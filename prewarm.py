"""Pre-warm a child agent: one-shot consent collection, delivered by email.

Instead of pausing mid-run at each OAuth interrupt, walk every auth-required
tool in the configured toolkits for a target user, union the required scopes
PER PROVIDER, and start one auth flow per provider. Gmail and Google
Calendar are both the google provider, so an entire toolkit set collapses
into a single consent link. The links go out in ONE email, sent through
Arcade's own Gmail.SendEmail tool executed as the agent service account.
Once the user grants, their child agent runs with zero interrupts.

Tradeoff worth narrating: mid-run authorize() is incremental least privilege
(each grant covers exactly one scope set; 25 tools here means 10 separate
consents). Pre-warm deliberately requests the scope UNION up front: one
consent per provider, broader grant. Convenience versus minimalism is a
policy choice, and this script is where it lives.

    python prewarm.py --user bob@example.com --email bob@example.com
    python prewarm.py --user bob@example.com --email bob@example.com --dry-run
    python prewarm.py --user bob@example.com --email bob@example.com --wait

--dry-run prints the email instead of sending. --wait blocks after sending
until every pending grant completes, then confirms the agent is warm.

The service account is itself just an Arcade user_id with a Gmail grant;
its token also lives in Arcade and never enters this process.
"""

import argparse
import os
from dataclasses import dataclass

from arcadepy import Arcade

from agent.arcade_toolkit import wait_for_grant
from agent.config import ARCADE_API_KEY, DEFAULT_TOOLKITS

# The identity that sends consent emails. Set ARCADE_SA_USER in the env
# or .env; orchestrator.py can override per run with --sa-user.
SERVICE_ACCOUNT = os.environ.get("ARCADE_SA_USER", "")
SEND_TOOL = "Gmail.SendEmail"


@dataclass
class ProviderConsent:
    provider: str
    tools: list[str]
    scopes: list[str]
    auth_response: object | None = None  # pending flow, None when already granted

    @property
    def granted(self) -> bool:
        return self.auth_response is None


def collect_consents(client: Arcade, toolkits: list[str], user_id: str) -> list[ProviderConsent]:
    """Union required scopes per provider, then start ONE auth flow each.

    auth.start is idempotent: an already-covered (user, provider, scopes)
    union comes back completed; otherwise it returns a single URL that covers
    every tool for that provider at once.
    """
    scopes: dict[str, set[str]] = {}
    tools: dict[str, list[str]] = {}
    for toolkit in toolkits:
        for tool in client.tools.list(toolkit=toolkit):
            req = getattr(tool, "requirements", None)
            auth_req = req and getattr(req, "authorization", None)
            if not auth_req:
                continue
            provider = auth_req.provider_id
            oauth2 = getattr(auth_req, "oauth2", None)
            scopes.setdefault(provider, set()).update(getattr(oauth2, "scopes", None) or [])
            tools.setdefault(provider, []).append(f"{tool.toolkit.name}.{tool.name}")

    consents = []
    for provider, scope_union in scopes.items():
        if scope_union:
            auth = client.auth.start(
                user_id=user_id, provider=provider, scopes=sorted(scope_union)
            )
        else:
            # Provider declares no scopes (e.g. GitHub): every tool shares one
            # grant, so authorize on a representative tool is the accurate
            # gate. auth.start with an empty scope list reports a phantom
            # pending flow even when the grant exists.
            auth = client.tools.authorize(
                tool_name=tools[provider][0], user_id=user_id
            )
        consents.append(
            ProviderConsent(
                provider=provider,
                tools=tools[provider],
                scopes=sorted(scope_union),
                auth_response=None if auth.status == "completed" else auth,
            )
        )
    return consents


def send_via_service_account(
    client: Arcade, recipient: str, subject: str, body: str, sa: str = SERVICE_ACCOUNT
):
    """Send through Arcade as the service account, bootstrapping its own
    grant via the operator console if needed (the sender cannot email its
    own consent link)."""
    svc = client.tools.authorize(tool_name=SEND_TOOL, user_id=sa)
    if svc.status != "completed":
        print(f"Service account needs a one-time grant first:\n  {svc.url}")
        wait_for_grant(client, svc)
    return client.tools.execute(
        tool_name=SEND_TOOL,
        input={
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "content_type": "html",
        },
        user_id=sa,
    )


def ensure_prewarmed(
    client: Arcade, toolkits: list[str], user_id: str, email: str | None, emit=print,
    sa: str = SERVICE_ACCOUNT,
) -> None:
    """The orchestrator's consent gate: detect missing grants, deliver the
    consent email, block until the user approves. Warm users pass straight
    through. Called inside each child's lane, so a cold user only stalls
    their own thread."""
    consents = collect_consents(client, toolkits, user_id)
    pending = [c for c in consents if not c.granted]
    if not pending:
        emit("warm: all provider grants in place")
        return
    emit(f"cold: missing grants for {', '.join(c.provider for c in pending)}")
    if email:
        subject, body = compose_email(user_id, pending)
        send_via_service_account(client, email, subject, body, sa=sa)
        emit(f"consent email sent to {email} from {sa}; waiting for approval...")
    else:
        for c in pending:
            emit(f"consent needed for {c.provider}: {c.auth_response.url}")
        emit("waiting for approval...")
    for consent in pending:
        wait_for_grant(client, consent.auth_response)
        emit(f"granted: {consent.provider}")
    emit("pre-warm complete; running with zero interrupts")


def compose_email(user_id: str, pending: list[ProviderConsent]) -> tuple[str, str]:
    """HTML consent email: one clickable authorize button per provider."""
    subject = "Action needed: authorize your agent"
    button_style = (
        "display:inline-block;padding:10px 22px;background:#4f46e5;color:#ffffff;"
        "border-radius:6px;text-decoration:none;font-weight:600"
    )
    rows = []
    for consent in pending:
        rows.append(
            '<p style="margin:18px 0 4px 0">'
            f'<a href="{consent.auth_response.url}" style="{button_style}">'
            f"Authorize {consent.provider.capitalize()}</a></p>"
            '<p style="margin:0;color:#6b7280;font-size:13px">'
            f"covers {len(consent.tools)} tools"
            + (f", {len(consent.scopes)} scopes" if consent.scopes else "")
            + "</p>"
        )
    body = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'max-width:560px;color:#111827;font-size:15px;line-height:1.5">'
        "<p>Hi,</p>"
        f"<p>Your agent (acting as <b>{user_id}</b>) needs your consent before "
        "it can work on your behalf. Every grant is scoped to you and revocable "
        "at any time. Your credentials are stored by Arcade and are never "
        "visible to the agent or the model.</p>"
        + "".join(rows)
        + '<p style="margin-top:24px">Nothing runs until you approve. '
        "Reply to this email with any questions.</p>"
        "<p>Agent service</p>"
        "</div>"
    )
    return subject, body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True, help="user_id to pre-warm")
    parser.add_argument("--email", required=True, help="where to send the consent links")
    parser.add_argument("--toolkits", default=None, help="comma-separated, overrides .env")
    parser.add_argument("--dry-run", action="store_true", help="print the email, do not send")
    parser.add_argument("--wait", action="store_true", help="block until all grants complete")
    args = parser.parse_args()

    toolkits = (
        [t.strip() for t in args.toolkits.split(",") if t.strip()]
        if args.toolkits
        else DEFAULT_TOOLKITS
    )
    client = Arcade(api_key=ARCADE_API_KEY())

    print(f"Collecting consents for {args.user} across {toolkits}...")
    consents = collect_consents(client, toolkits, args.user)
    pending = [c for c in consents if not c.granted]
    for c in consents:
        status = "granted" if c.granted else "PENDING"
        print(f"  {c.provider}: {len(c.tools)} tools, {len(c.scopes)} scopes -> {status}")

    if not pending:
        print("Agent is already warm. Nothing to send.")
        return

    subject, body = compose_email(args.user, pending)

    if args.dry_run:
        print(f"\n--- DRY RUN: email to {args.email} ---\nSubject: {subject}\n\n{body}")
        return

    response = send_via_service_account(client, args.email, subject, body)
    ok = getattr(response, "success", True)
    print(f"\nConsent email sent to {args.email} as {SERVICE_ACCOUNT}: {'ok' if ok else response}")

    if args.wait:
        print("Waiting for all grants...")
        for consent in pending:
            wait_for_grant(client, consent.auth_response)
            print(f"  granted: {consent.provider}")
        print(f"\n{args.user} is pre-warmed. Their child agent will run with zero interrupts.")


if __name__ == "__main__":
    main()
