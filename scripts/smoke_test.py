"""Pre-flight check: keys resolve, Arcade reachable, tools discoverable.

    python scripts/smoke_test.py

No LLM call, no tool execution, no auth flow. Safe to run anytime.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arcadepy import Arcade  # noqa: E402

from agent.arcade_toolkit import ArcadeToolset  # noqa: E402
from agent.config import ANTHROPIC_API_KEY, ARCADE_API_KEY, DEFAULT_TOOLKITS, MODEL  # noqa: E402


def main() -> None:
    ARCADE_API_KEY()
    print("[ok] ARCADE_API_KEY resolved")
    ANTHROPIC_API_KEY()
    print("[ok] ANTHROPIC_API_KEY resolved")

    toolset = ArcadeToolset(Arcade(api_key=ARCADE_API_KEY()), DEFAULT_TOOLKITS)
    print(f"[ok] Arcade reachable; {len(toolset.anthropic_tools)} tools across {DEFAULT_TOOLKITS}")

    auth_tools = sum(1 for v in toolset.requires_auth.values() if v)
    print(f"[ok] {auth_tools} tools require user authorization, {len(toolset.requires_auth) - auth_tools} are open")

    sample = sorted(toolset.name_map.items())[:5]
    for sanitized, qualified in sample:
        print(f"     {sanitized}  ->  {qualified}")

    print(f"[ok] model: {MODEL}")
    print("\nSmoke test passed. Run: python -m agent.cli --user <you>")


if __name__ == "__main__":
    main()
