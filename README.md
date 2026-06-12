# Lanekeeper

**Multi-user agent orchestration with per-user isolation and one-shot consent onboarding.**

Lanekeeper runs one AI agent per user, each in its own isolated lane. A resident orchestrator spawns user-bound child agents on demand or on a schedule, gates every lane behind per-user OAuth consent, and delivers consent requests as a single clickable email instead of interrupting mid-task. Built on [Arcade](https://arcade.dev) for tool calling and authorization and the [Anthropic SDK](https://docs.anthropic.com) for the model layer, with a deliberately thin hand-rolled loop so every seam stays visible and debuggable.

```
   Phase 1: service account bootstrap (console consent) ── warms the email sender
                          │
                          v
        Orchestrator ── long-running host, holds NO user identity
        control prompt: adduser / spawn / schedule / every / cancel / status
        scheduler (threading.Timer) ──┐ fires lanes now or later
                  ┌───────────────────┴─────────┐
                  v                             v
        ChildAgent(alice)             ChildAgent(bob)           ← one thread each,
        own Arcade client             own Arcade client           run independently
        own toolset + history         own toolset + history
                  │                             │
       consent gate: cold? the service account emails consent links, lane waits
                  │                             │
                  v                             v
user task ──> Claude (Anthropic SDK, tool defs from Arcade)
                   │ tool_use block
                   v
            ArcadeToolset.execute(tool, input, user_id)
                   │
        ┌── auth needed? ── yes ──> authorize() -> human visits URL -> resume
        │                           (only THIS child's lane pauses)
        v
            Arcade Engine executes against the provider
            (token injected by Arcade; the model never sees a credential)
```

## Why

An agent that acts on behalf of many users has two hard problems that frameworks tend to hide:

1. **Identity isolation.** Two users in the same process must never share grants, history, or tokens. Lanekeeper enforces this twice: in-process (each child owns its Arcade client, toolset, and message history; siblings share only a stdout lock) and server-side (Arcade keys grants by user, provider, and scope set). The `user_id` is fixed at child construction and cannot drift mid-conversation.
2. **Consent UX.** Mid-task OAuth interrupts are fine for a developer at a terminal and terrible for everyone else. Lanekeeper turns authorization into an onboarding step: it unions the required scopes per provider, starts one auth flow per provider, and emails the user a single message with one authorize button per provider. Grant once, and every scheduled run after that needs zero interaction.

## Quickstart

```bash
git clone https://github.com/iherdt/lanekeeper && cd lanekeeper
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your keys

python scripts/smoke_test.py    # keys + Arcade connectivity, no LLM call
```

`.env`:

```
ARCADE_API_KEY=arc_...
ANTHROPIC_API_KEY=sk-ant-...
ARCADE_SA_USER=agent-service@example.com   # identity that sends consent emails
ARCADE_TOOLKITS=gmail,googlecalendar       # toolkits each CHILD agent gets
ARCADE_SA_TOOLKITS=gmail                   # toolkits the SERVICE ACCOUNT gets (email only)
```

The toolkit list is the consent surface: the gate unions scopes across everything listed, not just what a task happens to use. Trim it to what your tasks need; that is least privilege at the fleet level, before scopes even enter the picture.

## Running

```bash
python orchestrator.py --sa-user agent-service@example.com \
  --user alice@example.com --task "Summarize my unread email" \
  --user bob@example.com   --task "Fetch today's calendar events" \
  --schedule bob@example.com=+10m
```

- **Phase 1** boots the service account through console consent (the only consent that ever touches the terminal; it just needs to send email).
- **Phase 2** starts the child lanes. Warm users run immediately. Cold users get one consent email and their lane waits, then auto-resumes into the task. A cold user stalls only their own lane.
- **Phase 3** keeps the orchestrator resident at a control prompt:

| Command | Effect |
|---|---|
| `adduser <user_id> [email]` | Onboard + pre-warm a user with no task, so later spawns have zero interrupts |
| `spawn <user_id> <task...>` | Run a user-bound child now |
| `schedule <when> <user_id> <task...>` | Run a child once, later |
| `every <when> <user_id> <task...>` | Recurring runs (`+10m` interval, `08:00` daily) |
| `cancel <user_id\|all>` | Cancel scheduled jobs |
| `memory <user_id>` | Show what the system remembers about a user |
| `pin <user_id> <fact...>` | Pin a never-evicted fact to a user's memory |
| `--no-memory` prefix on spawn/schedule/every | Run a child that neither reads nor writes memory |
| `status` / `help` / `quit` | Lanes + scheduled jobs / command list / exit |

`<when>` is `+30s` / `+10m` / `+2h` or `HH:MM` (next occurrence; daily under `every`).

### Example: a daily calendar digest per user

```
every 08:00 alice@example.com Fetch today's calendar events, summarize them, and email me the summary. Don't ask, just send.
every 08:05 bob@example.com Fetch today's calendar events, summarize them, and email me the summary. Don't ask, just send.
```

Each child runs every morning in its own lane, scoped to its own identity, emailing its own owner.

### Other entry points

```bash
python -m agent.cli --user alice@example.com        # single-user interactive REPL
python prewarm.py --user bob@example.com --email bob@example.com --wait   # standalone onboarding
python demo_multi_user.py --user-a alice@example.com --user-b bob@example.com  # sequential isolation proof
```

## The consent model

Arcade models authorization as an interrupt, not an error: `AuthorizationRequired` carries the URL a human must visit, and the loop resumes after the grant. Lanekeeper supports both consent strategies and is explicit about the tradeoff:

- **Incremental (mid-run)**: each grant covers exactly one scope set, minimal privilege, but interrupts the task. In live testing, 25 Google tools produced 10 separate consent links this way.
- **Pre-warm (onboarding)**: union the required scopes per provider and start ONE flow per provider. Gmail and Google Calendar are both the `google` provider, so an entire toolkit set collapses into a single consent link. Broader grant, zero mid-task interrupts.

Convenience versus minimalism is a policy choice, and `prewarm.py` is where that policy lives.

## Per-user memory

Children are ephemeral: one task, then the thread exits and the object is garbage. Continuity lives in a third per-user resource alongside the OAuth grant: a capped memory file (`memory/<user>.json`) the orchestrator owns and a child only sees through a user-scoped view. The 08:00 digest child is a brand-new object every morning, yet it knows the user said "skip recurring standups" three days ago.

Each user's memory has three tiers:

| Tier | Contents | Policy |
|---|---|---|
| `pinned` | Standing instructions ("always send as HTML") | Never evicted |
| `summary` | Rolling compaction of older history | Rewritten by a small-model merge call |
| `turns` | Recent tail of verbatim (task, outcome) pairs | Oldest evicted first |

The budget (`MEMORY_CAP_TOKENS`, default 4000) is enforced on write: over cap, the oldest half of the tail is merged into the summary with one small-model call (summarize-before-drop, so information degrades gracefully instead of disappearing); if still over, hard eviction trims oldest turns, then the summary. A failed compaction call falls back to pure eviction: memory must never block a lane.

Memory is injected as a system-prompt suffix, never as replayed message history (raw replay would orphan `tool_use` ids and bloat context). Per-user locks serialize concurrent lanes for the same user; cross-user isolation is by construction, one file per user, same move as the child itself.

## Feature demos

A guided tour, in the order that builds the story. All of them assume the orchestrator is up:

```bash
python orchestrator.py --sa-user agent-service@example.com
```

### 1. Lane isolation and the live auth interrupt

```
spawn alice@example.com List my unread email subjects
spawn bob@example.com   List my unread email subjects
```

If alice is warm and bob is cold, alice streams results immediately while bob's lane pauses: the SA emails bob a consent link and only bob's thread waits. Grant it and bob's lane resumes mid-flight while alice may still be running. One pause never blocks another lane, because authorization is per-user inside Arcade.

### 2. One-shot consent onboarding (pre-warm)

```
adduser carol@example.com
```

No task runs. The consent gate unions every required scope per provider, sends carol ONE email with one authorize button per provider, and waits in the background. After carol grants, every future spawn or scheduled run for her starts with zero interrupts. Compare against demo 1, where consent interrupted a live task: same mechanism, different policy.

### 3. Scheduling: deferred and recurring

```
schedule +2m bob@example.com Fetch today's calendar events and email me a summary. Don't ask, just send.
every 08:00 alice@example.com Fetch today's calendar events, summarize them, and email me the summary. Don't ask, just send.
status
cancel bob@example.com
```

`schedule` fires once; `every` re-arms after each run (`+10m` repeats on the interval, `08:00` rolls daily). `status` shows next fire times; `cancel` takes a user or `all`. Schedules are in-process timers and die with the process; `quit` warns about pending ones.

### 4. Memory: plant and recall across ephemeral children

Children are destroyed after every task, so this is the demo that proves continuity lives in the memory file, not the agent:

```
spawn alice@example.com Fetch today's calendar and summarize it. Remember for all future runs: format every summary as exactly three bullets, most important first.
memory alice@example.com
spawn alice@example.com Fetch today's calendar and summarize it.
```

The second spawn is a brand-new child with an empty message history, yet the summary comes back as exactly three bullets. Binary outcome, no interpretation needed: it either remembered or it did not.

### 5. Pinned facts

```
pin alice@example.com Always start your reply with the line "Lane: alice"
spawn alice@example.com What is on my calendar tomorrow?
```

The marker line appears in the output. Pinned facts inject on every run and survive every compaction and eviction; they are the tier for standing instructions, where the `turns` tail in demo 4 is best-effort recency.

### 6. Compaction and eviction under a tiny cap

Restart with a deliberately small budget so the policy is observable:

```bash
MEMORY_CAP_TOKENS=300 python orchestrator.py --sa-user agent-service@example.com
```

```
spawn alice@example.com What day is it today?
spawn alice@example.com What is 2 + 2?
spawn alice@example.com Name one fact about the moon.
spawn alice@example.com What day is it today?
memory alice@example.com
```

Watch `memory` output between spawns: verbatim turns disappear from the tail and an `Earlier history (compacted): ...` line grows in their place. That is summarize-before-drop firing live (one small-model call per compaction). Kill the network or the key and the same demo still stays under cap: compaction failure falls back to pure eviction.

### 7. Runs without memory

```
spawn --no-memory alice@example.com Summarize my unread email
schedule --no-memory +5m alice@example.com Run the cleanup task
```

The `--no-memory` prefix runs a child that neither reads nor writes memory: no remembered context injected, no trace left behind. Launch-time equivalent for all initial lanes: `python orchestrator.py --no-memory ...`. Use it for one-off tasks that should not pollute a user's memory (bulk experiments, debugging) or for users who should never be profiled at all.

## Field notes (learned the hard way, against the live API)

1. **Auth flows with a new scope set supersede the existing grant.** A working grant went `pending` (and execution went 403) after a single `auth.start` probe with a different scope union. Scope-set identity is part of the grant's identity. Consequences: the consent gate always requests the identical per-provider union, never mix per-tool `authorize()` and union `auth.start` flows for the same user, and a "warmth probe" is not passive.
2. **Some providers declare zero scopes.** The whole GitHub toolkit reports an empty scope list, so a union-based gate degenerates. For scopeless providers the accurate gate is `tools.authorize` on one representative tool; `auth.start` with an empty scope list reports a phantom pending flow.
3. **Long polls drop.** `auth.wait_for_completion` can die with a connection error minutes into a wait. The grant state lives server-side, so `wait_for_grant` reconnects with backoff instead of crashing the lane.
4. **Tool names need translation.** Anthropic tool names cannot contain dots, so `Gmail.SendEmail` is exposed to the model as `Gmail_SendEmail` and mapped back at execution time. Small impedance mismatches like this are the real work of platform integration.

## Design decisions

- **Thin custom loop over a framework.** The auth interrupt stays a visible code path you can debug. Reach for LangGraph when you need durable state, retries, or multi-agent handoffs; it hides exactly the seam this project is about.
- **Threads and `threading.Timer`, not asyncio.** Both SDKs here are synchronous, and a blocking OAuth wait parks only its own thread. Primitives you can narrate in one sentence.
- **Durability is deliberately out of scope.** Schedules are in-process timers and die with the process (`quit` warns you). The production upgrade is cron, a job queue, or Temporal for schedules, and a process per tenant for hard memory isolation.

## Production hardening checklist

Token storage policy and scope minimization per tenant; per-user rate limiting and retry with backoff on provider 429s; an audit log line per tool execution keyed by user and tool; an eval harness over golden prompts; deferred tool loading + tool search once the catalog grows past a few hundred tools.

## Layout

```
orchestrator.py        long-running host: phases, scheduler, control prompt
prewarm.py             one-shot consent collection + HTML consent email
demo_multi_user.py     sequential two-user isolation proof
agent/
  loop.py              Claude tool-use loop with the auth interrupt
  arcade_toolkit.py    discovery, per-user authorization, execution, wait_for_grant
  child.py             user-scoped child agent (one user, one toolset, one history)
  memory.py            per-user capped memory: pinned / summary / recent tail
  cli.py               single-user interactive REPL
  config.py            .env / environment key resolution
scripts/smoke_test.py  pre-flight: keys, connectivity, tool discovery
```

## License

MIT
