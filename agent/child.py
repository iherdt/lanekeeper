"""User-scoped child agent: everything it owns belongs to exactly one user.

Each child constructs its own Arcade client, its own toolset, its own
Anthropic client, and its own message history. Siblings share nothing in
this process except stdout, which the orchestrator guards with a lock.
Isolation is therefore enforced twice: in-process by construction, and
inside Arcade where grants are keyed by (user, provider, scopes). The
user_id is fixed at construction and cannot drift mid-conversation.

Children are ephemeral: one task, then the thread exits and the object is
garbage. Continuity across runs lives in the optional UserMemory view
(capped, compacted, user-scoped) injected at construction, never in the
child itself.
"""

import datetime

from arcadepy import Arcade

from .arcade_toolkit import ArcadeToolset
from .config import ARCADE_API_KEY
from .loop import ArcadeAgent
from .memory import UserMemory


class ChildAgent:
    def __init__(
        self,
        user_id: str,
        toolkits: list[str],
        emit=print,
        memory: UserMemory | None = None,
    ):
        self.user_id = user_id
        self.emit = emit
        self.memory = memory
        self.toolset = ArcadeToolset(Arcade(api_key=ARCADE_API_KEY()), toolkits)
        self.agent = ArcadeAgent(self.toolset)
        self.messages: list = []

    def run(self, task: str) -> str:
        """Run one task to completion; returns the final assistant text."""
        remembered = self.memory.render() if self.memory else ""
        if remembered:
            self.emit(f"memory loaded ({len(remembered)} chars)")
        self.messages.append({"role": "user", "content": task})
        self.agent.run_turn(
            self.messages, self.user_id, on_event=self.emit,
            system_extra=remembered or None,
        )
        # run_turn always ends on an assistant message (stop_reason != tool_use)
        final_blocks = self.messages[-1]["content"]
        texts = [b.text for b in final_blocks if getattr(b, "type", None) == "text"]
        final = texts[-1].strip() if texts else ""
        if self.memory:
            self.memory.remember(
                task, final, ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            )
        return final
