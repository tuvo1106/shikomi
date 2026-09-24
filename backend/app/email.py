"""Outbound email with a pluggable backend.

`console` (default) logs the message and appends it to an in-memory `outbox`,
which dev and tests inspect. `smtp` sends via the SMTP_* settings. Sending is
best-effort — `send_best_effort` retries a few times, then logs and swallows SMTP
failures — so a mail hiccup never breaks the flow that triggered it
(registration/reset).

Mail is sent by the arq worker (`worker/accounts.py:send_account_email`), not the
API: requests only enqueue, so SMTP latency can't show in a response's timing.

`send_email` is a **blocking** call (`smtplib` has no async API) and its caller
is an async job — so `send_best_effort` (the only entry point) runs it in a
thread via `asyncio.to_thread` rather than calling it directly, which would
otherwise block the whole event loop — the worker's other concurrent jobs —
for as long as the SMTP conversation takes. `_send_smtp` also passes an
explicit socket `timeout` (`settings.smtp_timeout_seconds`), since without one
a mail server that never responds (a dropped connection with no RST, a
firewall silently eating packets) hangs `smtplib.SMTP` forever — the thread
hop keeps that hang off the event loop, but a stuck thread would otherwise
still tie up the request awaiting it indefinitely.
"""
import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import get_settings

logger = logging.getLogger("app.email")
settings = get_settings()

# Indirection so a test can stub the backoff without patching `asyncio.sleep`
# process-wide (which would also short-circuit unrelated coroutines).
_sleep = asyncio.sleep


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str


# In-memory record of what the console backend "sent" (dev + tests).
outbox: list[SentEmail] = []


def _send_console(msg: SentEmail) -> None:
    outbox.append(msg)
    # Print to stdout (visible in the server log) so links are usable in dev —
    # uvicorn's logging config filters app-logger INFO, so a bare logger won't show.
    print(
        f"\n{'=' * 60}\n[email] To: {msg.to}\n[email] Subject: {msg.subject}\n\n"
        f"{msg.body}\n{'=' * 60}\n",
        flush=True,
    )
    logger.info("email sent to=%s subject=%s", msg.to, msg.subject)


def _send_smtp(msg: SentEmail) -> None:
    email = EmailMessage()
    email["From"] = settings.email_from
    email["To"] = msg.to
    email["Subject"] = msg.subject
    email.set_content(msg.body)
    with smtplib.SMTP(
        settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
    ) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(email)


def send_email(to: str, subject: str, body: str) -> None:
    """Dispatch a message to the configured backend.

    This can raise (an SMTP hiccup, auth error, or timeout). Callers that mail
    *after* committing user-visible state must treat it as best-effort — see
    `send_best_effort`, which every out-of-band flow uses.
    """
    msg = SentEmail(to=to, subject=subject, body=body)
    (_send_smtp if settings.email_backend == "smtp" else _send_console)(msg)


def _is_transient(exc: Exception) -> bool:
    """Whether retrying a failed send could plausibly succeed.

    Transient: a network/timeout error, a dropped connection, or an SMTP 4xx reply
    (the server saying "try again later", e.g. greylisting). Permanent: a 5xx reply
    (unknown recipient, rejected sender), an auth failure, any other SMTP error, or
    a non-network bug — repeating those can't help and only ties up a worker slot
    (and, for a bad recipient, is a cheap way to burn it).
    """
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return all(400 <= code < 500 for code, _ in exc.recipients.values())
    if isinstance(exc, smtplib.SMTPResponseException):  # incl. auth (535) and 5xx
        return 400 <= exc.smtp_code < 500
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return True
    if isinstance(exc, smtplib.SMTPException):
        return False
    return isinstance(exc, OSError)  # socket errors, timeouts, refused connections


async def send_best_effort(to: str, subject: str, body: str) -> None:
    """Send an email off-thread: retried a few times, then logged and swallowed.

    The contract for out-of-band flows (verification, password reset): the
    user-visible state — the account row, the reset token — is already committed
    before we mail, so a send failure must not surface. Concretely it must not

    * turn a successful registration into a 500 (the account exists; the client
      would wrongly think it failed), nor
    * make the password-reset *real-email* path 500 while the unknown-email path
      returns 202 early — that status difference is an enumeration oracle for the
      very flow built to prevent it.

    Only the SMTP hop is retried (`email_send_attempts` tries, exponential backoff
    from `email_retry_delay_seconds`), and only for *transient* failures
    (`_is_transient`) — never the token minting that precedes it, so a flaky mail
    server can't leave a trail of extra live links. Each failed attempt
    is logged (not silent), so a genuinely misconfigured mailer is still
    diagnosable. `send_email` runs via `asyncio.to_thread` (see the module
    docstring) so the blocking SMTP conversation — bounded by
    `settings.smtp_timeout_seconds` — doesn't stall the event loop.
    """
    attempts = max(1, settings.email_send_attempts)
    for attempt in range(1, attempts + 1):
        try:
            await asyncio.to_thread(send_email, to, subject, body)
            return
        except Exception as exc:  # noqa: BLE001 — best-effort by contract (see docstring)
            logger.exception("email send failed to=%s subject=%s attempt=%d/%d",
                             to, subject, attempt, attempts)
            if not _is_transient(exc):
                return  # permanent (bad recipient, bad credentials, a bug): don't retry
            if attempt < attempts:
                await _sleep(settings.email_retry_delay_seconds * 2 ** (attempt - 1))
