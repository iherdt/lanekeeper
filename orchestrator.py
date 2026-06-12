"""Long-running orchestrator: user-bound child agents, spawned now or later.

Launch it once and it stays up. Initial lanes come from the CLI; more can be
spawned or scheduled from the control prompt while earlier lanes are still
running. Child output streams into the terminal, color-prefixed per user.

    python orchestrator.py --sa-user agent-service@example.com \
        --user alice@example.com --task "Summarize my unread email" \
        --user bob@example.com   --task "Fetch today's calendar events" \
        --schedule bob@example.com=+10m

Launch chain, end to end from one command:

  Phase 1  Service account bootstrap. The SA (--sa-user, the email sender)
           goes through the ORIGINAL console consent flow: URLs print right
           here in the CLI and the orchestrator blocks until the operator
           grants. This is the only consent that ever touches the console.
  Phase 2  Child lanes. One thread per --user; each lane passes the consent
           gate (ensure_prewarmed): warm users run immediately, cold users
           get ONE consent email per missing provider, sent by the now-warm
           SA, and their lane waits, then auto-resumes into the task.
  Phase 3  Control prompt. The orchestrator runs until you quit:

             adduser  <user_id> [email]            onboard + prewarm, no task
             spawn    <user_id> <task...>          run a child now
             schedule <when> <user_id> <task...>   run a child once, later
             every    <when> <user_id> <task...>   recurring child runs
             cancel   <user_id|all>                cancel scheduled jobs
             status | help | quit

           <when> is +30s / +10m / +2h or HH:MM (next occurrence; HH:MM
           with `every` means daily).

Tasks pair with users by position; a single --task (or none) is shared.
Consent emails route by KEY (--email <user_id>=<address>); unrouted users
default to their user_id when it looks like an email address. --schedule is
also keyed (<user_id>=<when>): that user's initial lane starts later
instead of immediately.

Why threads, not asyncio: both SDKs here are synchronous, a blocking OAuth
wait parks only its own thread, and a demo rewards primitives you can
narrate in one sentence. Scheduling is threading.Timer for the same reason.
Production upgrades: a process per tenant for hard memory isolation, a real
job queue for fan-out, cron or Temporal for durable schedules.
"""

import argparse
import datetime
import threading
from pathlib import Path

from arcadepy import Arcade

from agent.child import ChildAgent
from agent.config import ARCADE_API_KEY, DEFAULT_TOOLKITS, MEMORY_CAP_TOKENS, SA_TOOLKITS
from agent.memory import MemoryManager
from prewarm import SERVICE_ACCOUNT, ensure_prewarmed

DEFAULT_TASK = (
    f"Today is {datetime.date.today():%B %d %Y}. Fetch today's calendar "
    "events and send an email to myself with a summary."
)

COLORS = ["\033[36m", "\033[33m", "\033[35m", "\033[32m", "\033[34m", "\033[31m"]
GRAY = "\033[90m"
RESET = "\033[0m"

_print_lock = threading.Lock()


def make_emitter(label: str, color: str):
    """Per-child logger: every line prefixed with the user it acts as."""

    def emit(text) -> None:
        with _print_lock:
            for line in str(text).splitlines() or [""]:
                print(f"{color}[{label}]{RESET} {line}")

    return emit


def parse_when(spec: str) -> float:
    """'+30s' / '+10m' / '+2h' relative, or 'HH:MM' next occurrence.
    Returns delay in seconds."""
    if spec.startswith("+"):
        unit, mult = spec[-1], {"s": 1, "m": 60, "h": 3600}
        if unit in mult:
            return float(spec[1:-1]) * mult[unit]
        return float(spec[1:])  # bare seconds
    hh, mm = spec.split(":")
    now = datetime.datetime.now()
    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


class Orchestrator:
    """Spawns user-scoped children now or on a timer, streams their output,
    tracks lane state. Holds NO user identity of its own and never calls a
    tool; identity lives only in the children."""

    def __init__(self, toolkits: list[str], sa_user: str, memory: MemoryManager):
        self.toolkits = toolkits
        self.sa_user = sa_user
        self.memory = memory
        self.lanes: list[dict] = []      # {user, task, thread, result}
        self.scheduled: list[dict] = []  # {user, task, fire_at, timer}
        self._spawn_count = 0

    def resolve_email(self, user_id: str, email_map: dict) -> str | None:
        return email_map.get(user_id) or (user_id if "@" in user_id else None)

    def spawn(
        self, user_id: str, task: str, email: str | None, use_memory: bool = True
    ) -> None:
        color = COLORS[self._spawn_count % len(COLORS)]
        self._spawn_count += 1
        emit = make_emitter(user_id, color)
        lane = {"user": user_id, "task": task, "result": None}

        def run() -> None:
            try:
                child = ChildAgent(
                    user_id, self.toolkits, emit=emit,
                    memory=self.memory.for_user(user_id) if use_memory else None,
                )
                emit(
                    f"child up: {len(child.toolset.anthropic_tools)} tools, isolated history, "
                    f"memory {'on' if use_memory else 'off'}"
                )
                # Consent gate: cold users get a consent email from the SA
                # and this lane waits; warm users pass straight through.
                ensure_prewarmed(
                    child.toolset.client, self.toolkits, user_id, email,
                    emit=emit, sa=self.sa_user,
                )
                lane["result"] = child.run(task)
                emit("lane done.")
            except Exception as exc:
                lane["result"] = f"FAILED: {exc}"
                emit(f"child failed: {exc}")

        thread = threading.Thread(target=run, name=f"child-{user_id}", daemon=True)
        lane["thread"] = thread
        self.lanes.append(lane)
        thread.start()

    def adduser(self, user_id: str, email: str | None) -> None:
        """Onboard a user identity without running a task: run the consent
        gate in the background so later spawns start with zero interrupts."""
        color = COLORS[self._spawn_count % len(COLORS)]
        self._spawn_count += 1
        emit = make_emitter(user_id, color)
        lane = {"user": user_id, "task": "(prewarm only)", "result": None}

        def warm() -> None:
            try:
                ensure_prewarmed(
                    Arcade(api_key=ARCADE_API_KEY()), self.toolkits, user_id,
                    email, emit=emit, sa=self.sa_user,
                )
                lane["result"] = "warm"
            except Exception as exc:
                lane["result"] = f"FAILED: {exc}"
                emit(f"prewarm failed: {exc}")

        thread = threading.Thread(target=warm, name=f"prewarm-{user_id}", daemon=True)
        lane["thread"] = thread
        self.lanes.append(lane)
        thread.start()

    def schedule(
        self, when_spec: str, user_id: str, task: str, email: str | None,
        recurring: bool = False, use_memory: bool = True,
    ) -> None:
        delay = parse_when(when_spec)
        fire_at = datetime.datetime.now() + datetime.timedelta(seconds=delay)
        job = {"user": user_id, "task": task, "fire_at": fire_at,
               "recurring": when_spec if recurring else None}

        def fire() -> None:
            label = "recurring" if recurring else "scheduled"
            print(f"{GRAY}[scheduler]{RESET} firing {label} child for {user_id}")
            self.spawn(user_id, task, email, use_memory=use_memory)
            if recurring and job in self.scheduled:
                # Re-arm: HH:MM specs roll to the next occurrence (daily),
                # +N specs repeat on a fixed interval.
                next_delay = parse_when(when_spec)
                job["fire_at"] = datetime.datetime.now() + datetime.timedelta(seconds=next_delay)
                timer = threading.Timer(next_delay, fire)
                timer.daemon = True
                job["timer"] = timer
                timer.start()
            elif job in self.scheduled:
                self.scheduled.remove(job)

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        job["timer"] = timer
        self.scheduled.append(job)
        timer.start()
        kind = f"every {when_spec}" if recurring else f"at {fire_at:%H:%M:%S}"
        print(f"{GRAY}[scheduler]{RESET} child for {user_id} scheduled {kind} (first in {int(delay)}s)")

    def cancel(self, target: str) -> None:
        """Cancel scheduled jobs by user_id, or all of them."""
        hits = [j for j in self.scheduled if target == "all" or j["user"] == target]
        for job in hits:
            job["timer"].cancel()
            self.scheduled.remove(job)
        print(f"cancelled {len(hits)} job(s)")

    def status(self) -> None:
        with _print_lock:
            print(f"lanes ({len(self.lanes)}):")
            for lane in self.lanes:
                state = "running" if lane["thread"].is_alive() else "done"
                tail = ""
                if lane["result"] and state == "done":
                    tail = f' -> {str(lane["result"])[:80]}'
                print(f'  {lane["user"]} [{lane["task"][:40]}]: {state}{tail}')
            print(f"scheduled ({len(self.scheduled)}):")
            for job in self.scheduled:
                when = f'every {job["recurring"]}, next' if job["recurring"] else "at"
                print(f'  {job["user"]} {when} {job["fire_at"]:%H:%M:%S}: {job["task"][:60]}')


def control_loop(orchestrator: Orchestrator, email_map: dict) -> None:
    """Phase 3: keep running, accept spawn/schedule/status/quit."""
    help_text = (
        "Commands:\n"
        "  adduser  <user_id> [consent_email]            onboard + prewarm a user, no task\n"
        "  spawn    <user_id> <task...>                  run a child now\n"
        "  schedule <when> <user_id> <task...>           run a child once, later\n"
        "  every    <when> <user_id> <task...>           run a child on a recurring schedule\n"
        "  cancel   <user_id|all>                        cancel scheduled jobs\n"
        "  memory   <user_id>                            show a user's remembered context\n"
        "  prefix spawn/schedule/every with --no-memory  to run without reading or writing memory\n"
        "  pin      <user_id> <fact...>                  pin a never-evicted fact to memory\n"
        "  status | help | quit\n"
        "  <when> = +30s / +10m / +2h interval, or HH:MM (next occurrence; daily when recurring)"
    )
    print(f"\n=== Phase 3: orchestrator running ===\n{help_text}\n")
    while True:
        try:
            line = input().strip()
        except (EOFError, KeyboardInterrupt):
            line = "quit"
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        use_memory = True
        if rest.startswith("--no-memory "):
            use_memory, rest = False, rest[len("--no-memory "):].strip()
        try:
            if cmd == "quit":
                running = [l for l in orchestrator.lanes if l["thread"].is_alive()]
                if running or orchestrator.scheduled:
                    print(
                        f"exiting with {len(running)} running lane(s) and "
                        f"{len(orchestrator.scheduled)} scheduled job(s); they die with the process"
                    )
                return
            elif cmd == "help":
                print(help_text)
            elif cmd == "status":
                orchestrator.status()
            elif cmd == "adduser":
                user_id, _, email = rest.partition(" ")
                if not user_id:
                    print("usage: adduser <user_id> [consent_email]")
                    continue
                if email:
                    email_map[user_id] = email.strip()
                orchestrator.adduser(user_id, orchestrator.resolve_email(user_id, email_map))
            elif cmd == "spawn":
                user_id, _, task = rest.partition(" ")
                if not user_id or not task:
                    print("usage: spawn <user_id> <task...>")
                    continue
                orchestrator.spawn(
                    user_id, task, orchestrator.resolve_email(user_id, email_map),
                    use_memory=use_memory,
                )
            elif cmd in ("schedule", "every"):
                when, _, rest2 = rest.partition(" ")
                user_id, _, task = rest2.partition(" ")
                if not when or not user_id or not task:
                    print(f"usage: {cmd} <+30s|+10m|+2h|HH:MM> <user_id> <task...>")
                    continue
                orchestrator.schedule(
                    when, user_id, task,
                    orchestrator.resolve_email(user_id, email_map),
                    recurring=(cmd == "every"), use_memory=use_memory,
                )
            elif cmd == "cancel":
                if not rest:
                    print("usage: cancel <user_id|all>")
                    continue
                orchestrator.cancel(rest.strip())
            elif cmd == "memory":
                if not rest:
                    print("usage: memory <user_id>")
                    continue
                print(orchestrator.memory.for_user(rest.strip()).render() or "(no memory yet)")
            elif cmd == "pin":
                user_id, _, fact = rest.partition(" ")
                if not user_id or not fact:
                    print("usage: pin <user_id> <fact...>")
                    continue
                orchestrator.memory.for_user(user_id).pin(fact.strip())
                print(f"pinned for {user_id}")
            else:
                print(f"unknown command: {cmd} (try: help)")
        except Exception as exc:
            print(f"command failed: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sa-user", default=SERVICE_ACCOUNT, dest="sa_user",
        help="service account user_id that sends consent emails "
        "(default: ARCADE_SA_USER from the environment)",
    )
    parser.add_argument("--user", action="append", default=[], dest="users")
    parser.add_argument("--task", action="append", default=[], dest="tasks")
    parser.add_argument(
        "--email", action="append", default=[], dest="emails",
        help="consent email routing, keyed: --email <user_id>=<address>. "
        "Unrouted users default to their user_id when it looks like an "
        "email address.",
    )
    parser.add_argument(
        "--schedule", action="append", default=[], dest="schedules",
        help="delay a user's initial lane, keyed: --schedule <user_id>=<+10m|HH:MM>",
    )
    parser.add_argument(
        "--no-memory", action="store_true", dest="no_memory",
        help="run the initial lanes without reading or writing memory",
    )
    parser.add_argument("--toolkits", default=None, help="comma-separated, overrides .env")
    args = parser.parse_args()

    if not args.sa_user:
        parser.error("--sa-user is required (or set ARCADE_SA_USER in the environment)")
    if len(args.tasks) > 1 and len(args.tasks) != len(args.users):
        parser.error("give one --task total (shared) or one per --user")

    def parse_keyed(entries: list[str], flag: str) -> dict:
        mapping = {}
        for entry in entries:
            if "=" not in entry:
                parser.error(f"{flag} must be <user_id>=<value>, got: {entry}")
            key, _, value = entry.partition("=")
            if key not in args.users:
                parser.error(f"{flag} targets unknown user: {key}")
            mapping[key] = value
        return mapping

    email_map = parse_keyed(args.emails, "--email")
    schedule_map = parse_keyed(args.schedules, "--schedule")
    toolkits = (
        [t.strip() for t in args.toolkits.split(",") if t.strip()]
        if args.toolkits
        else DEFAULT_TOOLKITS
    )

    memory = MemoryManager(Path("memory"), cap_tokens=MEMORY_CAP_TOKENS)
    orchestrator = Orchestrator(toolkits, args.sa_user, memory)

    if args.users:
        print("Consent email routing:")
        for user_id in args.users:
            target = orchestrator.resolve_email(user_id, email_map)
            print(f"  {user_id} -> {target or 'console URLs (no email)'}")

    # Phase 1: service account bootstrap via the ORIGINAL console flow.
    # The SA must be warm before any lane can email consent links. Scope it
    # to SA_TOOLKITS (gmail only by default): the SA just sends email, so it
    # should not be asked for github/calendar/etc the children use.
    print(f"\n=== Phase 1: service account bootstrap ({args.sa_user}) over {SA_TOOLKITS} ===")
    ensure_prewarmed(
        Arcade(api_key=ARCADE_API_KEY()),
        SA_TOOLKITS,
        args.sa_user,
        email=None,  # None = console URLs, the original CLI consent flow
        emit=make_emitter(f"SA {args.sa_user}", GRAY),
        sa=args.sa_user,
    )

    print("\n=== Phase 2: spawning child agents ===")
    for i, user_id in enumerate(args.users):
        task = args.tasks[i] if len(args.tasks) == len(args.users) else (
            args.tasks[0] if args.tasks else DEFAULT_TASK
        )
        email = orchestrator.resolve_email(user_id, email_map)
        if user_id in schedule_map:
            orchestrator.schedule(
                schedule_map[user_id], user_id, task, email,
                use_memory=not args.no_memory,
            )
        else:
            orchestrator.spawn(user_id, task, email, use_memory=not args.no_memory)

    control_loop(orchestrator, email_map)


if __name__ == "__main__":
    main()
