"""`_client_ip` only trusts `X-Forwarded-For` when `trust_proxy` is set (§4.1).

The header is client-supplied: if we trusted it unconditionally, a direct client
could forge a fresh IP per request and sail past the per-IP auth cap. These tests
pin both branches so that protection can't silently regress.
"""
from types import SimpleNamespace

import app.rate_limit as rl


def _request(xff=None, peer="203.0.113.9"):
    """A minimal stand-in for Starlette's Request: just what `_client_ip` reads."""
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer))


def test_client_ip_ignores_forged_xff_when_untrusted(monkeypatch):
    monkeypatch.setattr(rl.settings, "trust_proxy", False)
    # A spoofed header must be ignored → the socket peer is the rate-limit key.
    ip = rl._client_ip(_request(xff="1.2.3.4", peer="203.0.113.9"))
    assert ip == "203.0.113.9"


def test_client_ip_uses_first_hop_when_trusted(monkeypatch):
    monkeypatch.setattr(rl.settings, "trust_proxy", True)
    # Behind a trusted proxy, the client is the first XFF hop (not later proxies).
    ip = rl._client_ip(_request(xff="1.2.3.4, 10.0.0.1", peer="10.0.0.2"))
    assert ip == "1.2.3.4"


def test_client_ip_falls_back_when_no_client(monkeypatch):
    monkeypatch.setattr(rl.settings, "trust_proxy", False)
    req = SimpleNamespace(headers={}, client=None)
    assert rl._client_ip(req) == "unknown"
