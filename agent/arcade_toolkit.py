"""Arcade integration: tool discovery, per-user authorization, execution.

Design notes:
- Tool definitions come from Arcade pre-formatted; Anthropic tool names cannot
  contain dots, so "Gmail.ListEmails" is exposed to the model as
  "Gmail_ListEmails" and mapped back to the qualified name on execution.
- user_id travels with every authorize/execute call. Tokens live in Arcade,
  keyed by (user, provider). The model never sees a credential.
- Authorization is an interrupt, not an error: AuthorizationRequired carries
  the URL the human must visit, and the loop resumes after the grant.
"""

import time
from dataclasses import dataclass, field

from arcadepy import Arcade


def wait_for_grant(client: Arcade, auth_response, emit=print):
    """wait_for_completion with reconnect: a multi-minute long poll dies on
    transient connection drops, but the grant state lives server-side, so
    reconnect and keep waiting instead of crashing the lane."""
    attempt = 0
    while True:
        try:
            return client.auth.wait_for_completion(auth_response)
        except Exception as exc:
            attempt += 1
            if attempt > 120:
                raise
            emit(f"  (wait reconnect after {exc.__class__.__name__}, attempt {attempt})")
            time.sleep(min(2 * attempt, 15))


@dataclass
class AuthorizationRequired(Exception):
    tool_name: str
    user_id: str
    auth_response: object  # arcadepy AuthorizationResponse; .url and .status

    def __str__(self) -> str:
        return (
            f"{self.tool_name} needs authorization for user {self.user_id}: "
            f"{getattr(self.auth_response, 'url', '<no url>')}"
        )


@dataclass
class ArcadeToolset:
    client: Arcade
    toolkits: list[str]
    # sanitized model-facing name -> Arcade fully qualified name
    name_map: dict[str, str] = field(default_factory=dict)
    # qualified name -> bool (does the tool require user authorization)
    requires_auth: dict[str, bool] = field(default_factory=dict)
    anthropic_tools: list[dict] = field(default_factory=list)
    # (user_id, qualified_name) pairs already authorized this session
    _authorized: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self._discover()

    def _discover(self) -> None:
        for toolkit in self.toolkits:
            # Raw definitions: qualified names + auth requirements
            for tool in self.client.tools.list(toolkit=toolkit):
                qualified = f"{tool.toolkit.name}.{tool.name}"
                sanitized = qualified.replace(".", "_")
                self.name_map[sanitized] = qualified
                req = getattr(tool, "requirements", None)
                self.requires_auth[qualified] = bool(
                    req and getattr(req, "authorization", None)
                )
            # Formatted definitions: JSON schemas (OpenAI shape, converted below)
            for formatted in self.client.tools.formatted.list(
                format="openai", toolkit=toolkit
            ):
                fn = formatted["function"] if isinstance(formatted, dict) else formatted.function
                name = fn["name"] if isinstance(fn, dict) else fn.name
                description = (fn.get("description") if isinstance(fn, dict) else fn.description) or name
                parameters = (fn.get("parameters") if isinstance(fn, dict) else fn.parameters) or {
                    "type": "object",
                    "properties": {},
                }
                if name not in self.name_map:
                    # OpenAI-format names already use underscores; trust them
                    self.name_map[name] = name.replace("_", ".", 1)
                self.anthropic_tools.append(
                    {
                        "name": name,
                        "description": description[:1024],
                        "input_schema": parameters,
                    }
                )

    def ensure_authorized(self, qualified: str, user_id: str) -> None:
        if not self.requires_auth.get(qualified, False):
            return
        if (user_id, qualified) in self._authorized:
            return
        auth = self.client.tools.authorize(tool_name=qualified, user_id=user_id)
        if auth.status != "completed":
            raise AuthorizationRequired(qualified, user_id, auth)
        self._authorized.add((user_id, qualified))

    def wait_for_authorization(self, pending: AuthorizationRequired) -> None:
        """Block until the human grants access, then mark the tool usable."""
        wait_for_grant(self.client, pending.auth_response)
        self._authorized.add((pending.user_id, pending.tool_name))

    def execute(self, sanitized_name: str, tool_input: dict, user_id: str) -> str:
        qualified = self.name_map.get(sanitized_name, sanitized_name)
        self.ensure_authorized(qualified, user_id)
        response = self.client.tools.execute(
            tool_name=qualified,
            input=tool_input or {},
            user_id=user_id,
        )
        if getattr(response, "success", True) and response.output is not None:
            value = getattr(response.output, "value", None)
            if value is not None:
                return str(value)
            error = getattr(response.output, "error", None)
            if error is not None:
                return f"Tool error: {error}"
        return str(response)
