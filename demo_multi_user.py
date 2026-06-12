"""Multi-user isolation demo: one agent, two users, isolated grants.

Runs the same question through the same agent as two different user_ids.
Each user authorizes their own GitHub account; results differ per user and
neither the agent nor the model ever touches a token.

    python demo_multi_user.py --user-a alice@example.com --user-b bob@example.com
"""

import argparse

from arcadepy import Arcade

from agent.arcade_toolkit import ArcadeToolset
from agent.config import ARCADE_API_KEY
from agent.loop import ArcadeAgent

import datetime

QUESTION = (
    f"Today is {datetime.date.today():%B %d %Y}. Fetch today's calendar events "
    "and send an email to myself with a summary."
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-a", required=True)
    parser.add_argument("--user-b", required=True)
    args = parser.parse_args()

    toolset = ArcadeToolset(Arcade(api_key=ARCADE_API_KEY()), ["gmail","googlecalendar"])
    agent = ArcadeAgent(toolset)

    for user_id in (args.user_a, args.user_b):
        print(f"\n=== Acting as {user_id} ===")
        agent.run_turn([{"role": "user", "content": QUESTION}], user_id)

    print(
        "\nSame agent, same code path, two identities. Grants are keyed by "
        "user_id inside Arcade; tokens were injected at execution and never "
        "entered the model context."
    )


if __name__ == "__main__":
    main()
