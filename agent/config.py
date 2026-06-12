"""Key resolution: .env file, then shell env, then macOS launchctl (GUI env)."""

import os
import subprocess
from pathlib import Path


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _launchctl_getenv(name: str) -> str | None:
    try:
        out = subprocess.run(
            ["launchctl", "getenv", name], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        return out or None
    except Exception:
        return None


def get_key(name: str) -> str:
    value = os.environ.get(name) or _launchctl_getenv(name)
    if not value:
        raise SystemExit(
            f"{name} not found. Set it in the environment or in .env"
        )
    return value


# Load .env at import time so MODEL/DEFAULT_TOOLKITS below see it too
_load_dotenv()

ARCADE_API_KEY = lambda: get_key("ARCADE_API_KEY")  # noqa: E731
ANTHROPIC_API_KEY = lambda: get_key("ANTHROPIC_API_KEY")  # noqa: E731

MODEL = os.environ.get("AGENT_MODEL", "claude-opus-4-8")
DEFAULT_TOOLKITS = [
    t.strip()
    for t in os.environ.get("ARCADE_TOOLKITS", "github,gmail").split(",")
    if t.strip()
]
# The service account only sends consent emails (Gmail.SendEmail), so its
# bootstrap is scoped to gmail alone. Pre-warming it across the full child
# toolkit list would make it request github/calendar/etc it never uses.
SA_TOOLKITS = [
    t.strip()
    for t in os.environ.get("ARCADE_SA_TOOLKITS", "gmail").split(",")
    if t.strip()
]
