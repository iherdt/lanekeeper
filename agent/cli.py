"""Interactive multi-user agent REPL.

Usage:
    python -m agent.cli --user you@example.com
    python -m agent.cli --user alice@example.com --toolkits github

In-session commands:
    /user <id>   switch acting user (separate history, separate grants)
    /tools       list discovered tools
    /quit
"""

import argparse

from arcadepy import Arcade

from .arcade_toolkit import ArcadeToolset
from .config import ARCADE_API_KEY, DEFAULT_TOOLKITS
from .loop import ArcadeAgent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="user_id Arcade keys grants by")
    parser.add_argument(
        "--toolkits", default=",".join(DEFAULT_TOOLKITS), help="comma-separated"
    )
    args = parser.parse_args()

    toolkits = [t.strip() for t in args.toolkits.split(",") if t.strip()]
    print(f"Discovering tools for toolkits: {toolkits} ...")
    toolset = ArcadeToolset(Arcade(api_key=ARCADE_API_KEY()), toolkits)
    print(f"{len(toolset.anthropic_tools)} tools loaded.")

    agent = ArcadeAgent(toolset)
    user_id = args.user
    histories: dict[str, list] = {user_id: []}
    print(f"Acting as: {user_id}\n")

    while True:
        try:
            line = input(f"[{user_id}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break
        if line.startswith("/user "):
            user_id = line.split(maxsplit=1)[1].strip()
            histories.setdefault(user_id, [])
            print(f"Now acting as: {user_id} (fresh grants, fresh history)")
            continue
        if line == "/tools":
            for name in sorted(toolset.name_map.values()):
                marker = "auth" if toolset.requires_auth.get(name) else "open"
                print(f"  [{marker}] {name}")
            continue

        history = histories[user_id]
        history.append({"role": "user", "content": line})
        agent.run_turn(history, user_id)
        print()


if __name__ == "__main__":
    main()
