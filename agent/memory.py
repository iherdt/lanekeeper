"""Per-user memory: isolated, capped, compacted, evictable.

Children stay ephemeral; continuity moves into a third per-user resource
alongside the OAuth grant: a capped memory file. The orchestrator owns the
MemoryManager; a child only ever receives a UserMemory view scoped to its
own user_id. Same isolation move as ChildAgent itself.

Two tiers per user: a rolling summary (the compacted tier) and a recent
tail of verbatim turns, plus pinned facts that are never evicted. The
budget is enforced on write, in order:

  1. Compaction: merge the oldest half of the tail into the summary with
     one small-model call (summarize-before-drop, information degrades
     gracefully instead of disappearing).
  2. Eviction: if still over cap, drop oldest turns, then trim the summary.

Compaction failure falls back to pure eviction. Memory must never block a
lane; degraded memory beats a dead child.

Storage: memory/<user-slug>.json, one file per user. The cap governs what
enters the prompt, not what you may keep elsewhere; pair with an unbounded
audit log if you need full history.
"""

import json
import re
import threading
from pathlib import Path

EMPTY = {"pinned": [], "summary": "", "turns": []}


def _tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Budgeting needs a stable,
    fast bound, not an exact count."""
    return max(1, len(text) // 4)


def _slug(user_id: str) -> str:
    return re.sub(r"[^a-z0-9.@+_-]", "_", user_id.lower())


class UserMemory:
    """One user's capped memory. All file access goes through the per-user
    lock so concurrent lanes for the same user (a spawn racing a recurring
    job) serialize their read-modify-write."""

    def __init__(self, path: Path, cap_tokens: int, lock: threading.Lock, compactor=None):
        self.path = path
        self.cap = cap_tokens
        self.lock = lock
        self.compactor = compactor  # (summary, evicted_turns) -> new summary

    # -- storage -----------------------------------------------------------

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return json.loads(json.dumps(EMPTY))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1))

    # -- read side ---------------------------------------------------------

    def render(self) -> str:
        """Prompt text injected as a system suffix. Distilled text, not raw
        message history: replaying prior messages would orphan tool_use ids
        and bloat context."""
        with self.lock:
            data = self._load()
        if not (data["pinned"] or data["summary"] or data["turns"]):
            return ""
        parts = ["What you remember about this user from previous runs:"]
        if data["pinned"]:
            parts.append("Standing instructions: " + " | ".join(data["pinned"]))
        if data["summary"]:
            parts.append("Earlier history (compacted): " + data["summary"])
        for turn in data["turns"]:
            parts.append(f"- [{turn['ts']}] task: {turn['task']} -> {turn['outcome']}")
        return "\n".join(parts)

    # -- write side --------------------------------------------------------

    def pin(self, fact: str) -> None:
        """Pinned facts survive every compaction and eviction."""
        with self.lock:
            data = self._load()
            if fact not in data["pinned"]:
                data["pinned"].append(fact)
            self._save(data)

    def remember(self, task: str, outcome: str, ts: str) -> None:
        with self.lock:
            data = self._load()
            data["turns"].append(
                {"ts": ts, "task": task[:300], "outcome": outcome[:600]}
            )
            self._enforce_cap(data)
            self._save(data)

    # -- budget ------------------------------------------------------------

    def _size(self, data: dict) -> int:
        return _tokens(
            " ".join(data["pinned"]) + data["summary"] + json.dumps(data["turns"])
        )

    def _enforce_cap(self, data: dict) -> None:
        if self._size(data) <= self.cap:
            return
        # 1) compact: merge the oldest half of the tail into the summary
        if self.compactor and len(data["turns"]) > 1:
            half = len(data["turns"]) // 2
            evicted, data["turns"] = data["turns"][:half], data["turns"][half:]
            try:
                # summary gets at most half the budget (~4 chars/token)
                data["summary"] = self.compactor(data["summary"], evicted)[: self.cap * 2]
            except Exception:
                pass  # fall through to eviction
        # 2) evict: oldest turns first, then trim the summary tail
        while data["turns"] and self._size(data) > self.cap:
            data["turns"].pop(0)
        while data["summary"] and self._size(data) > self.cap:
            data["summary"] = data["summary"][: max(40, int(len(data["summary"]) * 0.8))]
            if len(data["summary"]) <= 40:
                data["summary"] = ""


class MemoryManager:
    """Owned by the orchestrator. for_user() hands out views scoped to one
    user's file; cross-user isolation is by construction."""

    def __init__(self, root: Path, cap_tokens: int = 4000, compactor=None):
        self.root = Path(root)
        self.cap = cap_tokens
        self.compactor = compactor if compactor is not None else self._llm_compactor
        self._locks: dict[str, threading.Lock] = {}
        self._registry_lock = threading.Lock()

    def for_user(self, user_id: str) -> UserMemory:
        with self._registry_lock:
            lock = self._locks.setdefault(user_id, threading.Lock())
        return UserMemory(
            self.root / f"{_slug(user_id)}.json", self.cap, lock, self.compactor
        )

    def _llm_compactor(self, summary: str, evicted: list[dict]) -> str:
        """One small-model call per compaction. Memory maintenance should
        not cost frontier-model tokens."""
        import anthropic

        from .config import ANTHROPIC_API_KEY, MEMORY_MODEL

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY())
        events = "\n".join(
            f"[{t['ts']}] {t['task']} -> {t['outcome']}" for t in evicted
        )
        response = client.messages.create(
            model=MEMORY_MODEL,
            max_tokens=600,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Merge this existing memory summary and these new events "
                        "into one updated summary of durable facts and preferences, "
                        "under 200 words. Drop transient detail; keep anything that "
                        "would change how future tasks for this user should run.\n\n"
                        f"Existing summary:\n{summary or '(none)'}\n\n"
                        f"New events:\n{events}"
                    ),
                }
            ],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()
