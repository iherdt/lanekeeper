"""Thin Claude tool-use loop over Arcade tools.

Why a hand-rolled loop instead of LangGraph/CrewAI: the
auth interrupt should stay visible and debuggable. The loop is ~60 lines; the
authorization pause/resume is an explicit code path, not a framework callback.
Reach for LangGraph when you need durable state, retries, or multi-agent
handoffs in production.
"""

import anthropic

from .arcade_toolkit import ArcadeToolset, AuthorizationRequired
from .config import ANTHROPIC_API_KEY, MODEL

SYSTEM_PROMPT = (
    "You are a production assistant that acts on real SaaS accounts through "
    "Arcade tools. You act only on behalf of the user identified to you; you "
    "never have access to their credentials. You may be running unattended "
    "with nobody available to answer questions: when the task explicitly "
    "pre-authorizes an outward-facing action (sending email, posting), "
    "perform it without asking. Otherwise confirm before destructive or "
    "outward-facing actions. Be concise."
)


class ArcadeAgent:
    def __init__(self, toolset: ArcadeToolset, model: str = MODEL):
        self.client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY())
        self.toolset = toolset
        self.model = model

    def run_turn(
        self, messages: list, user_id: str, on_event=print, system_extra: str | None = None
    ) -> list:
        """Run one user turn to completion. Mutates and returns messages.

        system_extra appends per-user context (e.g. memory) to the stable
        system prompt without touching message history."""
        system = SYSTEM_PROMPT + ("\n\n" + system_extra if system_extra else "")
        while True:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                system=system,
                tools=self.toolset.anthropic_tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type == "text" and block.text.strip():
                    on_event(block.text)

            if response.stop_reason != "tool_use":
                return messages

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                on_event(f"  -> {block.name}({block.input}) as {user_id}")
                try:
                    result = self.toolset.execute(block.name, block.input, user_id)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result[:50_000],
                        }
                    )
                except AuthorizationRequired as pending:
                    # The demo moment: agent pauses, human grants, agent resumes.
                    on_event(f"\n  AUTH REQUIRED for {user_id}:\n  {pending.auth_response.url}\n")
                    on_event("  Waiting for grant...")
                    self.toolset.wait_for_authorization(pending)
                    on_event("  Granted. Resuming.")
                    result = self.toolset.execute(block.name, block.input, user_id)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result[:50_000],
                        }
                    )
                except Exception as exc:  # surface tool failures to the model
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {exc}",
                            "is_error": True,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
