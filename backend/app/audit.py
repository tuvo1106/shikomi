"""Structured audit logging for security-relevant auth events.

Emits one record per event on the `app.audit` logger — the trail you'd ship to a
SIEM/log aggregator to spot brute force, account takeover, or abuse (failed
logins, lockouts, password resets, refresh-token reuse). The events are the raw
material; alerting on them is an ops concern.

One hard rule: **never log secrets** — no passwords, no tokens, no hashes. Emails,
user ids, and IPs are fine (identifying *who/where* is the whole point of an
audit log). Callers pass only safe context.
"""
import logging

logger = logging.getLogger("app.audit")


def audit(event: str, **fields) -> None:
    """Record one auth event.

    Args:
        event: a dotted event name, e.g. ``auth.login.failure``.
        fields: safe key/value context (email, user_id, ip, …); None values are
            dropped. Passed via `extra` so a structured (JSON) handler in prod can
            emit them as first-class fields, while the default formatter appends a
            readable ``k=v`` tail.
    """
    safe = {k: v for k, v in fields.items() if v is not None}
    kv = " ".join(f"{k}={v}" for k, v in safe.items())
    logger.info("audit %s %s", event, kv, extra={"audit_event": event, "audit_fields": safe})
