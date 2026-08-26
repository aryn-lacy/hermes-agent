"""Focused coverage for per-peer headers, User-Agent, 524 retry policy,
and destination-bound secret forwarding (PR #86322).

Extracted from test_a2a_plugin.py when the 524 retry became opt-in
(`idempotency: true`) and credential forwarding became origin-bound, per
upstream review: keep this surface in its own module under the 2K-line
test-file invariant.
"""

from __future__ import annotations

import pytest
import urllib.error as urlerr

from plugins.platforms.a2a import protocol, tools


# ---------------------------------------------------------------------------
# Header propagation and precedence
# ---------------------------------------------------------------------------

class TestPerPeerHeaders:
    def test_resolve_peer_carries_headers(self, monkeypatch):
        cfg = {"a2a_agents": {"mac": {
            "url": "http://localhost:9999",
            "headers": {"CF-Access-Client-Id": "cf-id"},
        }}}
        monkeypatch.setattr(tools, "_load_config", lambda: cfg)
        peer = tools._resolve_peer("mac")
        assert peer is not None
        assert peer["headers"] == {"CF-Access-Client-Id": "cf-id"}

    def test_resolve_peer_defaults_headers_to_empty(self, monkeypatch):
        cfg = {"a2a_agents": {"mac": {"url": "http://localhost:9999"}}}
        monkeypatch.setattr(tools, "_load_config", lambda: cfg)
        peer = tools._resolve_peer("mac")
        assert peer is not None
        assert peer["headers"] == {}

    def test_call_sends_auth_and_custom_headers(self, monkeypatch):
        cfg = {"a2a_agents": {"mac": {
            "url": "http://localhost:9999",
            "auth": {"type": "bearer", "token": "tok-123"},
            "headers": {"CF-Access-Client-Id": "cf-id", "CF-Access-Client-Secret": "cf-sec"},
        }}}
        monkeypatch.setattr(tools, "_load_config", lambda: cfg)
        monkeypatch.setattr(tools, "_fetch_card", lambda *a, **k: None)
        captured = {}

        def fake_post(url, body, headers, timeout, retry_524=False):
            captured.update(headers)
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        tools.a2a_call({"agent": "mac", "message": "ping"})
        assert captured["Authorization"] == "Bearer tok-123"
        assert captured["CF-Access-Client-Id"] == "cf-id"
        assert captured["CF-Access-Client-Secret"] == "cf-sec"

    def test_custom_headers_take_precedence(self, monkeypatch):
        """Peer config headers win over the derived Authorization header on
        name collision (a proxy may require a different auth scheme)."""
        cfg = {"a2a_agents": {"mac": {
            "url": "http://localhost:9999",
            "auth": {"type": "bearer", "token": "tok-123"},
            "headers": {"Authorization": "***"},
        }}}
        monkeypatch.setattr(tools, "_load_config", lambda: cfg)
        monkeypatch.setattr(tools, "_fetch_card", lambda *a, **k: None)
        captured = {}

        def fake_post(url, body, headers, timeout, retry_524=False):
            captured.update(headers)
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        tools.a2a_call({"agent": "mac", "message": "ping"})
        assert captured["Authorization"] == "***"

    def test_orchestrate_fanout_sends_custom_headers(self, monkeypatch):
        cfg = {"a2a_agents": {
            "a": {"url": "http://localhost:9001", "capabilities": ["x"], "headers": {"X-Tenant": "t1"}},
            "b": {"url": "http://localhost:9002", "capabilities": ["x"], "headers": {"X-Tenant": "t2"}},
        }}
        monkeypatch.setattr(tools, "_load_config", lambda: cfg)
        monkeypatch.setattr(tools, "_fetch_card", lambda *a, **k: None)
        seen = []

        def fake_post(url, body, headers, timeout, retry_524=False):
            seen.append((url, headers.get("X-Tenant")))
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        tools.a2a_orchestrate({"capability": "x", "message": "m"})
        assert len(seen) == 2
        assert {t for _, t in seen} == {"t1", "t2"}


class TestClientHttpEdgeCases:
    def test_get_json_sends_user_agent_and_custom_headers(self, monkeypatch):
        seen = {}

        class _Resp:
            def read(self):
                return b'{"name": "peer"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            seen["headers"] = dict(req.headers)
            return _Resp()

        monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
        out = tools._http_get_json("http://peer/.well-known/agent-card.json",
                                   {"CF-Access-Client-Id": "cf-id"}, 30)
        assert out == {"name": "peer"}
        lowered = {k.lower(): v for k, v in seen["headers"].items()}
        assert lowered["user-agent"] == "Hermes-A2A/1.0"
        assert lowered["cf-access-client-id"] == "cf-id"


# ---------------------------------------------------------------------------
# 524 retry policy: default off, opt-in per peer via `idempotency: true`
# ---------------------------------------------------------------------------

class Test524RetryPolicy:
    def test_524_not_retried_by_default(self, monkeypatch):
        """A 524 is indeterminate (origin may have completed the task).
        Without the peer's idempotency opt-in, exactly one attempt is made
        and the error surfaces to the caller."""
        attempts = {"n": 0}

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            raise urlerr.HTTPError(req.full_url, 524, "Origin Time-out", {}, None)

        monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urlerr.HTTPError) as ei:
            tools._http_post_json("http://peer/", {"x": 1}, {}, 30)
        assert ei.value.code == 524
        assert attempts["n"] == 1

    def test_524_retried_when_peer_opts_in(self, monkeypatch):
        """With retry_524=True the full backoff ladder runs before giving
        up on a persistently-524-ing peer."""
        monkeypatch.setattr(tools.time, "sleep", lambda s: None)
        attempts = {"n": 0}

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            raise urlerr.HTTPError(req.full_url, 524, "Origin Time-out", {}, None)

        monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urlerr.HTTPError):
            tools._http_post_json("http://peer/", {"x": 1}, {}, 30, retry_524=True)
        assert attempts["n"] == tools._POST_MAX_RETRIES

    def test_524_backoff_is_exponential_when_opted_in(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(tools.time, "sleep", lambda s: sleeps.append(s))
        attempts = {"n": 0}

        class _Resp:
            def read(self):
                return b'{"ok": true}'

        class _Ctx:
            def __enter__(self):
                return _Resp()

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            if attempts["n"] < tools._POST_MAX_RETRIES:
                raise urlerr.HTTPError(req.full_url, 524, "Origin Time-out", {}, None)
            return _Ctx()

        monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
        out = tools._http_post_json("http://peer/", {"x": 1}, {}, 30, retry_524=True)
        assert out == {"ok": True}
        assert attempts["n"] == tools._POST_MAX_RETRIES
        assert sleeps == [1, 2]

    def test_other_errors_never_retried(self, monkeypatch):
        attempts = {"n": 0}

        def fake_urlopen(req, timeout=None):
            attempts["n"] += 1
            raise urlerr.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

        monkeypatch.setattr(tools.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(urlerr.HTTPError):
            tools._http_post_json("http://peer/", {"x": 1}, {}, 30, retry_524=True)
        assert attempts["n"] == 1

    def test_call_passes_idempotency_flag_to_post(self, monkeypatch):
        """Peer config `idempotency: true` reaches the POST as retry_524."""
        cfg = {"a2a_agents": {"mac": {
            "url": "http://localhost:9999",
            "idempotency": True,
        }}}
        monkeypatch.setattr(tools, "_load_config", lambda: cfg)
        monkeypatch.setattr(tools, "_fetch_card", lambda *a, **k: None)
        captured = {}

        def fake_post(url, body, headers, timeout, retry_524=False):
            captured["retry_524"] = retry_524
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        tools.a2a_call({"agent": "mac", "message": "ping"})
        assert captured["retry_524"] is True


# ---------------------------------------------------------------------------
# Destination-bound secret forwarding
# ---------------------------------------------------------------------------

class TestRpcOriginBinding:
    def _peer(self, **extra):
        return {"url": "https://configured.example", **extra}

    def test_same_origin_card_url_used(self):
        assert tools._url_same_origin("https://configured.example/rpc",
                                      "https://configured.example")
        assert not tools._url_same_origin("https://other.example/rpc",
                                          "https://configured.example")

    def test_port_and_scheme_are_part_of_origin(self):
        assert not tools._url_same_origin("http://configured.example/rpc",
                                          "https://configured.example")
        assert not tools._url_same_origin("https://configured.example:8443/rpc",
                                          "https://configured.example")

    def test_cross_origin_rpc_rejected_without_allowlist(self, monkeypatch):
        """A card advertising a foreign RPC origin must NOT receive the
        credential-bearing header map: the send falls back to the
        configured origin."""
        card = {"supportedInterfaces": [{"protocolBinding": "JSONRPC",
                                         "url": "https://evil.example/rpc"}]}
        monkeypatch.setattr(tools, "_fetch_card", lambda *a, **k: card)
        posted = {}

        def fake_post(url, body, headers, timeout, retry_524=False):
            posted["url"] = url
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        import logging as _logging
        monkeypatch.setattr(tools, "logger", _logging.getLogger("test-a2a"))
        reply, ctx, state = tools._send_task(
            "peer-x", self._peer(auth={"type": "bearer", "token": "tok"}), "hi", "")
        assert posted["url"] == "https://configured.example"

    def test_cross_origin_rpc_allowed_via_allowlist(self, monkeypatch):
        card = {"supportedInterfaces": [{"protocolBinding": "JSONRPC",
                                         "url": "https://trusted.example/rpc"}]}
        monkeypatch.setattr(tools, "_fetch_card", lambda *a, **k: card)
        posted = {}

        def fake_post(url, body, headers, timeout, retry_524=False):
            posted["url"] = url
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        peer = self._peer(allowed_rpc_origins=["https://trusted.example/rpc"])
        tools._send_task("peer-x", peer, "hi", "")
        assert posted["url"] == "https://trusted.example/rpc"

    def test_card_fetch_carries_no_credentials(self, monkeypatch):
        """The card fetch itself must be bare: no bearer header, no custom
        proxy headers — the card endpoint is discovery data, not a secret
        destination."""
        fetched = {}

        def fake_fetch(url, headers, timeout):
            fetched["headers"] = headers
            return None

        monkeypatch.setattr(tools, "_fetch_card", fake_fetch)

        def fake_post(url, body, headers, timeout, retry_524=False):
            return protocol.jsonrpc_result(
                body["id"],
                protocol.build_task("t", "c1", protocol.STATE_COMPLETED, "ok"),
            )

        monkeypatch.setattr(tools, "_http_post_json", fake_post)
        peer = self._peer(auth={"type": "bearer", "token": "tok"},
                          headers={"CF-Access-Client-Secret": "sec"})
        tools._send_task("peer-x", peer, "hi", "")
        assert fetched["headers"] == {}
