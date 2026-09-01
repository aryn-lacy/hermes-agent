"""
A2A client tools — let the Hermes agent talk to *other* agents as a peer.

Tools (registered in the ``a2a`` toolset):
  - a2a_discover(url)         -> fetch + summarize a peer's Agent Card
  - a2a_call(agent, message)  -> send a task to a peer, return its reply
  - a2a_list()                -> list configured peers + persisted conversations
  - a2a_history(context_id)   -> recall a persisted A2A conversation
  - a2a_orchestrate(...)      -> fan-out task to multiple peers by capability

Peers are resolved from config.yaml under ``a2a_agents``::

    a2a_agents:
      researcher:
        url: "http://localhost:9999"
        auth: { type: bearer, token: "sk-..." }
        timeout: 120
        capabilities: [web_search, research]

Transport is stdlib urllib (no a2a-sdk dependency). The wire format is the A2A
v1.0 JSON-RPC ``message/send`` method; replies from v0.3 peers still parse.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional, TypedDict

from . import protocol, security

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 120
_ORCHESTRATE_MAX_WORKERS = 6  # max parallel peers for fan-out


# HTTP statuses where the peer effectively does not serve the streaming
# endpoint (card advertised streaming, endpoint disagrees) -> fall back.
_STREAM_FALLBACK_HTTP_CODES = frozenset({404, 405, 501})

# SSE per-read idle cap. Server keepalives arrive every ~5s; 30s of silence
# means the stream is starved, not merely slow.
_STREAM_READ_TIMEOUT_S = 30.0


class _A2aTransportError(ValueError):
    """Transport-level failure of the A2A streaming path.

    Distinct from application-level JSON-RPC errors so _send_task can fall
    back to message/send for transport problems (endpoint missing, stream
    died, truncated response) WITHOUT resubmitting a task the peer already
    processed and rejected at the application level.

    ``frames_received`` records whether the dead stream had produced any
    frames before the failure. A zero-frame failure is a provably clean
    first dispatch (message/send fallback is safe). A frames-received
    failure is an INDETERMINATE outcome: it falls back only when the peer
    config asserts idempotency (same contract as 524 retry); otherwise the
    caller must not resubmit.

    ``seen_ctx`` is the last contextId the stream established;
    ``frame_count`` is how many frames arrived before the stream died.
    """

    def __init__(self, *args: object, frames_received: bool = False,
                 seen_ctx: str = "", frame_count: int = 0) -> None:
        super().__init__(*args)
        self.frames_received = frames_received
        self.seen_ctx = seen_ctx
        self.frame_count = frame_count


# --------------------------------------------------------------------------
# Peer resolution
# --------------------------------------------------------------------------

def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _resolve_peer(agent: str) -> Optional[dict]:
    """Resolve a peer name to {url, auth, timeout, capabilities}, or treat ``agent`` as a URL."""
    if agent.startswith("http://") or agent.startswith("https://"):
        return {"url": agent, "auth": {}, "timeout": _DEFAULT_TIMEOUT, "capabilities": []}
    cfg = _load_config()
    peers = cfg.get("a2a_agents") or {}
    entry = peers.get(agent)
    if not entry:
        return None
    return {
        "url": entry.get("url", ""),
        "auth": entry.get("auth", {}) or {},
        "headers": entry.get("headers", {}) or {},
        "timeout": int(entry.get("timeout", _DEFAULT_TIMEOUT)),
        "capabilities": entry.get("capabilities", []) or [],
        "tenant": entry.get("tenant", ""),
        "idempotency": bool(entry.get("idempotency", False)),
        "allowed_rpc_origins": entry.get("allowed_rpc_origins") or [],
    }


def _auth_header(auth: dict) -> dict:
    if auth and auth.get("type") == "bearer" and auth.get("token"):
        return {"Authorization": f"Bearer {auth['token']}"}
    return {}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

# Redirects are a credential-exfiltration vector: urllib's default opener
# follows 3xx responses and forwards the full header map (Authorization,
# proxy service tokens) to whatever host the redirect points at. Peers are
# semi-trusted (card-controlled), so every hop must stay inside the origin
# policy or the send dies.
class _NoCredentialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail-closed redirect policy for credential-bearing requests.

    A redirect hop is followed with the full header map only when its target
    is same-origin with the ORIGINAL request URL or is one of the operator's
    pinned allowed origins — the same policy that gated the initial URL.
    Any other hop is refused (HTTPError), never followed. urllib's built-in
    cross-host Authorization stripping is partial (scheme/port changes,
    custom headers) — we enforce it uniformly here.
    """

    def __init__(self, allowed_origins: tuple[str, ...] = ()):
        self.allowed_origins = allowed_origins
        super().__init__()

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original = req.full_url
        if _url_same_origin(newurl, original) or any(
                _url_same_origin(newurl, o) for o in self.allowed_origins):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"A2A redirect to cross-origin {newurl} refused (not same-origin, not in allowed_rpc_origins)",
            headers, fp)


def _open_url_no_redirect_leak(req: urllib.request.Request, timeout: int,
                               allowed_origins: tuple[str, ...] = ()) -> Any:
    """urlopen with fail-closed cross-origin redirect handling."""
    opener = urllib.request.build_opener(_NoCredentialRedirectHandler(allowed_origins))
    return opener.open(req, timeout=timeout)


def _http_get_json(url: str, headers: dict, timeout: int,
                   allowed_origins: tuple[str, ...] = ()) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Hermes-A2A/1.0", **headers}, method="GET")
    with _open_url_no_redirect_leak(req, timeout, allowed_origins) as resp:  # noqa: S310 (configured peers)
        return json.loads(resp.read().decode("utf-8"))


# Retry budget for Cloudflare 524 (origin timeout) on blocking POST, when the
# peer has opted in. A 524 means the proxy gave up on the response, NOT that
# the origin failed — the peer may have already executed the task. Retrying a
# mutating send on a 524 is only safe when the peer deduplicates requests
# (the retry re-sends the identical task id and message), which the operator
# asserts per peer via `idempotency: true` in the peer config.
# Default: no retry — the indeterminate outcome propagates to the caller.
_POST_MAX_RETRIES = 3


def _http_post_json(url: str, body: dict, headers: dict, timeout: int,
                    retry_524: bool = False,
                    allowed_origins: tuple[str, ...] = ()) -> dict:
    data = json.dumps(body).encode("utf-8")
    # Custom peer headers are operator-controlled but Content-Type and
    # A2A-Version are protocol-owned and must not be clobbered; a config typo
    # would otherwise cause peer rejection or protocol-version mismatches.
    # User-Agent stays overridable (some proxies filter user agents).
    hdrs = {
        "User-Agent": "Hermes-A2A/1.0",
        **headers,
        "Content-Type": "application/json",
        "A2A-Version": protocol.PROTOCOL_VERSION,
    }
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")

    attempts = _POST_MAX_RETRIES if retry_524 else 1
    for attempt in range(attempts):
        try:
            with _open_url_no_redirect_leak(req, timeout, allowed_origins) as resp:  # noqa: S310 (configured peers)
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if (retry_524 and e.code == 524 and attempt < attempts - 1):
                # Exponential backoff: 1s, 2s, 4s. Safe as a blocking sleep:
                # plugin tools run on worker threads (orchestrate uses a
                # ThreadPoolExecutor; handlers never touch the gateway's
                # event loop).
                logger.warning(
                    "A2A: 524 from %s (origin may have completed the task); "
                    "peer opted into idempotent retry, backing off %.0fs",
                    url, 2 ** attempt)
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("A2A retry loop exited without a result")  # pragma: no cover


def _card_url(base_url: str) -> str:
    # A2A v1.0 canonical discovery path. v0.2 used agent.json; servers may
    # still serve that as a legacy alias, but clients should prefer this.
    return base_url.rstrip("/") + "/.well-known/agent-card.json"


def _legacy_card_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/.well-known/agent.json"


def _fetch_card(base_url: str, headers: dict, timeout: int,
                allowed_origins: tuple[str, ...] = ()) -> dict:
    try:
        return _http_get_json(_card_url(base_url), headers, timeout, allowed_origins)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return _http_get_json(_legacy_card_url(base_url), headers, timeout, allowed_origins)


def _select_jsonrpc_interface(card: Optional[dict]) -> Optional[dict]:
    if isinstance(card, dict):
        for iface in card.get("supportedInterfaces", []) or []:
            if isinstance(iface, dict) and iface.get("protocolBinding") == "JSONRPC" and iface.get("url"):
                return iface
    return None


def _rpc_url(base_url: str, card: Optional[dict]) -> str:
    """Prefer the card's JSONRPC interface (v1.0 supportedInterfaces), then the
    card's legacy top-level url, then the configured base."""
    iface = _select_jsonrpc_interface(card)
    if iface:
        return str(iface["url"])
    if isinstance(card, dict) and isinstance(card.get("url"), str) and card["url"]:
        return card["url"]
    return base_url.rstrip("/")


def _interface_tenant(card: Optional[dict], peer: dict) -> str:
    iface = _select_jsonrpc_interface(card)
    if iface and iface.get("tenant"):
        return str(iface["tenant"])
    return str(peer.get("tenant") or "")


def _url_origin(url: str) -> tuple[str, str]:
    """(scheme, host:port) of a URL, lowercased; port defaulted per scheme."""
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    # parsed.port is None when absent; explicit :0 is a real (if unroutable)
    # port and must not be silently defaulted.
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    return parsed.scheme.lower(), f"{host}:{port}"


def _url_same_origin(candidate: str, configured: str) -> bool:
    """True when candidate and configured share scheme + host + port."""
    try:
        return _url_origin(candidate) == _url_origin(configured)
    except ValueError:
        return False


def _allowed_rpc_origins(peer: dict) -> list[str]:
    """Operator-pinned cross-origin RPC URLs exempt from the origin check.

    Entries are compared by ORIGIN (scheme + host + port), so an entry pins
    the whole service, not one exact path: an allowlist entry
    ``https://rpc.internal.example.com`` matches a card advertising
    ``https://rpc.internal.example.com/anything``.
    """
    raw = peer.get("allowed_rpc_origins") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(u).rstrip("/") for u in raw if str(u).strip()]


def _origin_allowed(candidate: str, peer: dict) -> bool:
    """True when candidate's origin is the configured origin or a pinned
    allowed origin (origin-level match, not exact string)."""
    try:
        cand = _url_origin(candidate)
    except ValueError:
        return False
    # Same origin as the configured base URL (any path) is always allowed —
    # a card may move the RPC within its own service.
    try:
        if _url_same_origin(candidate, peer.get("url", "")):
            return True
    except ValueError:
        pass
    for entry in _allowed_rpc_origins(peer):
        try:
            if _url_origin(entry) == cand:
                return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------------
# Shared send path (used by a2a_call and a2a_orchestrate)
# --------------------------------------------------------------------------

def _short_state(state: str) -> str:
    """TASK_STATE_COMPLETED -> completed (also passes through v0.3 states)."""
    return state.replace("TASK_STATE_", "").replace("_", "-").lower() if state else ""


def _send_task(agent_label: str, peer: dict, message: str, context_id: str) -> tuple[str, str, str]:
    """Send one message/send to a peer. Returns (reply_text, context_id, state).

    Raises urllib errors / ValueError for the caller to format. Handles
    outbound redaction, audit, persistence, and metrics.

    **Idempotency:** Uses a deterministic task_id derived from (context_id,
    message) so retries send the same ID, enabling the server to deduplicate.
    """
    base_url = peer.get("url", "")
    headers = {**_auth_header(peer.get("auth", {}) or {}), **(peer.get("headers", {}) or {})}
    timeout = int(peer.get("timeout", _DEFAULT_TIMEOUT))
    # Operator asserts this peer dedupes on message identity (Hermes peers
    # do), making 524 retries and stream-fallback resends safe. Without it, a
    # 524 (origin may have completed the task) is surfaced, never retried.
    idempotency = bool(peer.get("idempotency", False))
    auth = peer.get("auth", {}) or {}
    if auth and any(k.lower() == "authorization" for k in (peer.get("headers", {}) or {})):
        logger.warning(
            "A2A: peer '%s' custom headers override the derived Authorization "
            "header — deliberate proxy auth schemes only",
            agent_label)
    allowed = tuple(_allowed_rpc_origins(peer))

    # Best-effort card fetch (to learn the rpc URL); non-fatal on failure.
    # The card is fetched AT the configured origin with the full credential
    # map — same destination the operator pinned the credentials for (a
    # Cloudflare-Access-fronted peer otherwise 403s the card and streaming
    # discovery is lost). The egress bound is enforced on the RPC destination
    # after the fetch: a card-advertised cross-origin URL never receives them.
    card = None
    try:
        card = _fetch_card(base_url, headers, min(timeout, 30), allowed)
    except Exception:
        pass

    rpc_url = _rpc_url(base_url, card)
    if not _origin_allowed(rpc_url, peer):
        # The card advertised an RPC interface on a different origin than the
        # configured base URL. Sending there would forward operator secrets
        # (bearer tokens, proxy service tokens) to a card-controlled host.
        # Refuse: fall back to the configured origin, never follow the card.
        logger.warning(
            "A2A: peer '%s' card advertised cross-origin RPC URL %s; not in "
            "peer's allowed_rpc_origins — using configured origin %s instead",
            agent_label, rpc_url, base_url)
        rpc_url = base_url.rstrip("/")

    ctx = context_id or protocol.new_context_id()
    safe_message = security.redact_outbound(message)
    # Deterministic task_id for idempotent retries
    task_id = protocol.deterministic_task_id(ctx, safe_message)
    # v1.0: contextId lives inside the Message, not at the params top level.
    rpc_body = {
        "jsonrpc": "2.0",
        "id": task_id,
        "method": "SendMessage",
        "params": {
            "taskId": task_id,
            "message": protocol.text_message(protocol.ROLE_USER, safe_message, context_id=ctx),
        },
    }

    tenant = _interface_tenant(card, peer)
    if tenant:
        rpc_body["params"]["tenant"] = tenant

    security.audit("outbound", agent_label, rpc_body["id"], safe_message)
    protocol.persist_message(ctx, "user", safe_message, rpc_body["id"])
    protocol.metrics.outbound_total += 1

    # Streaming path: if the peer advertises streaming, SendStreamingMessage
    # keeps bytes flowing (SSE keepalives) so proxies with idle timeouts
    # (e.g. Cloudflare's ~100s) do not kill long-running turns. The vetted
    # rpc_url above is reused for both paths (never re-derived from the card).
    if isinstance(card, dict) and (card.get("capabilities") or {}).get("streaming"):
        try:
            return _send_task_stream(agent_label, rpc_url, rpc_body,
                                     headers, timeout, ctx, rpc_body["id"])
        except _A2aTransportError as exc:
            # Zero-frame transport failure: task provably never reached the
            # peer's engine -> message/send is a clean first dispatch.
            # Frames-received death: outcome INDETERMINATE (peer may have
            # executed). Fall back only when the operator asserted
            # idempotency for this peer (same contract as 524 retry);
            # otherwise return an explicit indeterminate result.
            frames_seen = getattr(exc, "frames_received", False)
            if frames_seen and not idempotency:
                logger.warning(
                    "A2A: streaming send for %s died after frames (%s); "
                    "outcome indeterminate — NOT falling back to message/send",
                    agent_label, exc)
                return _indeterminate_outcome(agent_label, exc, ctx, rpc_body["id"])
            if frames_seen:
                logger.warning(
                    "A2A: streaming send for %s died after frames (%s); "
                    "peer asserted idempotency — falling back to message/send",
                    agent_label, exc)
            else:
                logger.debug(
                    "A2A: streaming send failed for %s (%s); falling back to message/send",
                    agent_label, exc)
        except urllib.error.HTTPError as exc:
            if exc.code not in _STREAM_FALLBACK_HTTP_CODES:
                raise
            logger.debug(
                "A2A: streaming endpoint returned %s for %s; falling back to message/send",
                exc.code, agent_label)
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            # Connection-level failures (DNS, refused, reset, bad framing,
            # read timeout) before any frame -> clean fallback.
            logger.debug(
                "A2A: streaming connection failed for %s (%s); falling back to message/send",
                agent_label, exc)

    resp = _http_post_json(rpc_url, rpc_body, headers, timeout, retry_524=idempotency)
    if "error" in resp:
        err = resp["error"]
        raise ValueError(f"Peer '{agent_label}' returned an error: {err.get('message', err)}")

    result = resp.get("result", {})
    payload = protocol.unwrap_send_message_response(result)
    reply = _reply_text_from_result(payload)
    reply_ctx, state = ctx, ""
    if isinstance(payload, dict):
        reply_ctx = payload.get("contextId", ctx)
        state = (payload.get("status") or {}).get("state", "")
    protocol.persist_message(reply_ctx, "agent", reply, rpc_body["id"])
    protocol.metrics.inbound_total += 1
    return reply, reply_ctx, state


# --------------------------------------------------------------------------
# Streaming send path (surgical port of PR #86369 onto fork main)
# --------------------------------------------------------------------------

def _http_post_sse(url: str, body: dict, headers: dict, timeout: int):
    """POST with Accept: text/event-stream and yield decoded SSE data payloads.

    Yields each ``data:`` frame's parsed JSON. Malformed data lines are
    skipped silently. Comment lines (keepalives) and event/id fields are
    consumed without yielding. Raises urllib errors / _A2aTransportError for
    the caller to format.

    Timeout semantics: the socket's per-read timeout is capped at
    ``_STREAM_READ_TIMEOUT_S`` (keepalive-starvation detection — server
    keepalives arrive every ~5s), and the *total* turn is bounded by a
    wall-clock deadline of ``timeout + _STREAM_READ_TIMEOUT_S``. Without the
    deadline, a peer that keeps sending keepalives could hold the stream
    open indefinitely, since a per-read timeout resets on every byte.
    """
    data = json.dumps(body).encode("utf-8")
    # Accept is protocol-owned for this streaming request, alongside
    # Content-Type/A2A-Version (same precedence policy as _http_post_json).
    hdrs = {
        "User-Agent": "Hermes-A2A/1.0",
        **headers,
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "A2A-Version": protocol.PROTOCOL_VERSION,
    }
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    deadline = time.monotonic() + timeout + _STREAM_READ_TIMEOUT_S
    with urllib.request.urlopen(req, timeout=min(timeout, _STREAM_READ_TIMEOUT_S)) as resp:  # noqa: S310 (configured peers)
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.startswith("text/event-stream"):
            # Peer ignored the stream request; body is a plain JSON-RPC response.
            # A 200 with a non-JSON body (HTML error page, empty body behind
            # a misbehaving proxy) is a transport problem, not an application
            # answer — wrap it so _send_task falls back to message/send
            # instead of hard-failing on an unhandled JSONDecodeError.
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                yield json.loads(raw)
            except (json.JSONDecodeError, RecursionError):
                raise _A2aTransportError(
                    f"non-SSE response was not valid JSON-RPC "
                    f"(Content-Type: {ctype!r}, first bytes: {raw[:64]!r})"
                ) from None
            return
        for raw in resp:
            if time.monotonic() > deadline:
                raise _A2aTransportError(
                    f"stream exceeded total deadline of {timeout}s")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue  # keepalive comments, event/id fields
            payload = line[len("data:"):].strip()
            try:
                yield json.loads(payload)
            except (json.JSONDecodeError, RecursionError):
                continue  # malformed/hostile frame — skip, do not abort


def _send_task_stream(agent_label: str, rpc_url: str, rpc_body: dict, headers: dict,
                       timeout: int, ctx: str, task_id: str) -> tuple[str, str, str]:
    """Send one SendStreamingMessage and collect the terminal StreamResponse.

    Frames are JSON-RPC-wrapped StreamResponse objects (A2A v1.0 §9.4); the
    stream closes on the terminal state. Returns (reply_text, context_id, state).

    Raises _A2aTransportError when the stream cannot produce a result
    (closed before any result frame, or closed without a terminal state).
    A zero-frame failure is safe to retry via message/send; a frames-received
    failure carries ``frames_received=True`` (plus ``seen_ctx`` and
    ``frame_count``) so the caller can treat it as an indeterminate outcome
    rather than resubmit. Mid-stream connection deaths (wall-clock deadline,
    read timeout, reset) are re-qualified the same way: the reader raises
    them without bookkeeping, so the loop re-tags them against the frames it
    actually consumed before the failure. Raises ValueError for
    application-level JSON-RPC errors — the peer processed and rejected the
    task, so retrying would duplicate side effects.
    """
    rpc_body = dict(rpc_body, method="SendStreamingMessage")
    result = None
    saw_terminal = False
    artifact_parts: list = []
    task_id_out = task_id
    # Peers may establish the context in an early frame and omit it from
    # later ones (spec-legal: the Task carries contextId, individual events
    # need not repeat it). Track the latest non-empty value so history lands
    # under the peer's own addressing, and never let an absent-contextId
    # frame fall back to "" and clobber it.
    seen_ctx = ctx
    frame_count = 0
    try:
        for frame in _http_post_sse(rpc_url, rpc_body, headers, timeout):
            if not isinstance(frame, dict):
                continue
            frame_count += 1
            if frame.get("error"):
                # Application-level rejection on a healthy stream: return it
                # to the caller, never fall back (would resubmit the task).
                raise ValueError(f"Peer '{agent_label}' returned an error: "
                                 f"{frame['error'].get('message', frame['error'])}")
            candidate = frame.get("result")
            if not isinstance(candidate, dict):
                continue
            # StreamResponse is member-discriminated (A2A v1.0): task snapshots,
            # statusUpdate events, artifactUpdate events, bare messages.
            if isinstance(candidate.get("artifactUpdate"), dict):
                art = candidate["artifactUpdate"].get("artifact") or {}
                if art.get("parts"):
                    artifact_parts.extend(art["parts"])
                continue
            if isinstance(candidate.get("statusUpdate"), dict):
                upd = candidate["statusUpdate"]
                cand = {"status": upd.get("status") or {},
                        "contextId": upd.get("contextId", ""),
                        "taskId": upd.get("taskId", "")}
            elif isinstance(candidate.get("task"), dict):
                cand = candidate["task"]
            elif isinstance(candidate.get("message"), dict):
                cand = {"status": {"state": "", "message": candidate["message"]},
                        "contextId": candidate["message"].get("contextId", "")}
            else:
                cand = candidate
            result = cand
            if cand.get("taskId"):
                task_id_out = cand["taskId"]  # peer-assigned id keys the history
            frame_ctx = str(cand.get("contextId") or "")
            if frame_ctx:
                seen_ctx = frame_ctx
            state = (cand.get("status") or {}).get("state", "")
            if state in protocol.TERMINAL_STATES:
                saw_terminal = True
                break
    except _A2aTransportError as exc:
        # The SSE reader raises without frame bookkeeping (wall-clock
        # deadline exceeded, non-SSE/non-JSON body guard). Re-qualify it
        # here: once frames have arrived the death is an indeterminate
        # outcome, not a clean zero-frame failure, so the caller must not
        # resubmit via message/send. A genuinely zero-frame failure keeps
        # frames_received=False and still falls back.
        if not exc.frames_received:
            exc.frames_received = frame_count > 0
            exc.seen_ctx = seen_ctx
            exc.frame_count = frame_count
        raise
    except urllib.error.HTTPError:
        # HTTP status errors keep their existing semantics (404/405/501 ->
        # fallback, others propagate) and are handled by _send_task, which
        # already distinguishes them from generic transport errors.
        raise
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
        # Mid-stream connection death (DNS/refused/reset/bad framing/read
        # timeout) after _http_post_sse may have already yielded frames.
        # Zero-frame stays a clean first dispatch; frames-received is an
        # indeterminate outcome the caller must not resubmit.
        raise _A2aTransportError(
            f"stream failed mid-read: {exc}",
            frames_received=(frame_count > 0), seen_ctx=seen_ctx,
            frame_count=frame_count) from exc
    if result is None:
        raise _A2aTransportError(
            f"Peer '{agent_label}' stream closed without a result",
            frames_received=(frame_count > 0), seen_ctx=seen_ctx,
            frame_count=frame_count)
    if not saw_terminal:
        # Truncated stream (peer died after WORKING): indistinguishable from
        # success if we returned here, so fail loud. The task's outcome is
        # indeterminate — frames were received but no terminal state arrived.
        raise _A2aTransportError(
            f"Peer '{agent_label}' stream closed without a terminal state",
            frames_received=True, seen_ctx=seen_ctx, frame_count=frame_count)
    if artifact_parts:
        # Accumulate every artifact part (multi-artifact/chunked streams);
        # artifacts carry the final output ahead of status messages, matching
        # the message/send extraction order.
        result = dict(result, artifacts=[{"parts": artifact_parts}])
    reply = _reply_text_from_result(result)
    reply_ctx = result.get("contextId") or seen_ctx
    state = (result.get("status") or {}).get("state", "")
    protocol.persist_message(reply_ctx, "agent", reply, task_id_out)
    protocol.metrics.inbound_total += 1
    return reply, reply_ctx, state


def _indeterminate_outcome(agent_label: str, exc: _A2aTransportError,
                           ctx: str, task_id: str) -> tuple[str, str, str]:
    """Return an explicit indeterminate result instead of resubmitting.

    The stream produced frames but died without a terminal state: the peer
    may have already executed the task, and it has no proven retry-safe
    idempotency contract, so a message/send fallback could run it twice.
    Surface the unknown outcome to the caller, keep the stream's contextId
    so follow-ups land in the same conversation, and persist it like a
    normal reply (non-terminal state).
    """
    frame_count = getattr(exc, "frame_count", 0) or 0
    reply_ctx = getattr(exc, "seen_ctx", "") or ctx
    reply = (
        f"Peer '{agent_label}' stream died mid-task after {frame_count} "
        f"frame{'s' if frame_count != 1 else ''} — outcome unknown; the task "
        f"may have already run. No automatic retry (a replay could run it "
        f"twice); use a2a_call again with a new message if intended."
    )
    protocol.persist_message(reply_ctx, "agent", reply, task_id)
    protocol.metrics.inbound_total += 1
    return reply, reply_ctx, ""



def _reply_text_from_result(result: Any) -> str:
    result = protocol.unwrap_send_message_response(result)
    if not isinstance(result, dict):
        return str(result)
    # Artifacts first (final output), then status message (interim/clarify).
    for artifact in result.get("artifacts", []) or []:
        txt = protocol.extract_text(artifact)
        if txt:
            return txt
    status = result.get("status", {}) or {}
    msg = status.get("message")
    if msg:
        return protocol.extract_text(msg)
    # Bare message result (message/send may return a Message instead of a Task)
    return protocol.extract_text(result)


# --------------------------------------------------------------------------
# Tool handlers
# --------------------------------------------------------------------------

def a2a_discover(args: dict, **_: Any) -> str:
    """Fetch and summarize the Agent Card at ``url``."""
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: 'url' is required (e.g. http://localhost:9999)."
    try:
        card = _fetch_card(url, {}, _DEFAULT_TIMEOUT)
    except urllib.error.HTTPError as e:
        return f"Error: discovery failed — HTTP {e.code} from {url}."
    except Exception as e:
        return f"Error: could not reach {url} — {e}."

    name = card.get("name", "?")
    desc = card.get("description", "")
    caps = card.get("capabilities", {}) or {}
    skills = card.get("skills", []) or []
    auth = "yes" if card.get("security") else "no"
    ifaces = card.get("supportedInterfaces", []) or []
    proto = ", ".join(
        f"{i.get('protocolBinding', '?')} v{i.get('protocolVersion', '?')}"
        for i in ifaces if isinstance(i, dict)
    ) or f"v{card.get('protocolVersion', '?')} (pre-1.0 card)"
    lines = [
        f"Agent: {name}",
        f"Description: {desc}",
        f"URL: {_rpc_url(url, card)}",
        f"Protocol: {proto}",
        f"Streaming: {bool(caps.get('streaming'))}  Push: {bool(caps.get('pushNotifications'))}  Auth required: {auth}",
        f"Skills ({len(skills)}):",
    ]
    for s in skills[:20]:
        lines.append(f"  - {s.get('name', s.get('id', '?'))}: {s.get('description', '')}")
    return "\n".join(lines)


def a2a_call(args: dict, **_: Any) -> str:
    """Send a task to a peer agent and return its reply.

    ``agent`` is a configured peer name (from ``a2a_agents``) or a direct URL.
    ``context_id`` continues a prior exchange (multi-turn) when provided.
    """
    # Accept common aliases models reach for (observed live: 'agent_name').
    agent = str(args.get("agent") or args.get("agent_name") or args.get("name") or "").strip()
    message = str(args.get("message") or args.get("text") or args.get("task") or "").strip()
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not agent or not message:
        return "Error: both 'agent' and 'message' are required."

    peer = _resolve_peer(agent)
    if not peer or not peer.get("url"):
        return (
            f"Error: unknown agent '{agent}'. Configure it under 'a2a_agents' in "
            f"config.yaml or pass a full http(s):// URL."
        )

    try:
        reply, reply_ctx, state = _send_task(agent, peer, message, context_id)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"Error: peer '{agent}' rejected auth (HTTP {e.code}). Check the configured token."
        if e.code == 429:
            return f"Error: peer '{agent}' rate limited us (HTTP 429). Retry later."
        return f"Error: call to '{agent}' failed — HTTP {e.code}."
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Error: call to '{agent}' failed — {e}."

    header = f"[{agent} · context {reply_ctx}"
    if state:
        header += f" · {_short_state(state)}"
    header += "]"
    body = reply or "(no text reply)"
    if state == protocol.STATE_INPUT_REQUIRED:
        body += (
            "\n\n(The peer needs more input — answer by calling a2a_call again "
            f"with context_id '{reply_ctx}'.)"
        )
    return f"{header}\n{body}"


def a2a_list(args: dict | None = None, **_: Any) -> str:
    """List configured A2A peers and any persisted conversations."""
    cfg = _load_config()
    peers = cfg.get("a2a_agents") or {}
    lines = []
    if peers:
        lines.append(f"Configured peers ({len(peers)}):")
        for name, entry in peers.items():
            auth = (entry.get("auth") or {}).get("type", "none")
            caps = entry.get("capabilities", [])
            cap_str = f" caps: {', '.join(caps)}" if caps else ""
            lines.append(f"  - {name}: {entry.get('url', '?')} (auth: {auth}){cap_str}")
    else:
        lines.append("No peers configured. Add them under 'a2a_agents' in config.yaml.")

    convos = protocol.list_conversations()
    if convos:
        lines.append("")
        lines.append(f"Persisted conversations ({len(convos)}) — recall with a2a_history:")
        for c in convos[:25]:
            lines.append(f"  - {c}")

    # Show metrics snapshot
    m = protocol.metrics.snapshot()
    lines.append("")
    lines.append(f"Metrics: {m['inbound_total']} in / {m['outbound_total']} out, "
                 f"{m['tasks_completed']} completed, {m['tasks_failed']} failed, "
                 f"{m['streams_started']} streams, {m['push_sent']} push sent, "
                 f"{m['anti_loop_triggers']} anti-loop, {m['rate_limit_triggers']} rate-limited, "
                 f"avg {m['avg_latency_ms']}ms")

    return "\n".join(lines)


def a2a_history(args: dict, **_: Any) -> str:
    """Recall a persisted A2A conversation by context_id.

    This is how prior A2A exchanges survive compaction/restarts: every turn is
    written to ~/.hermes/a2a_conversations/<context>.jsonl and can be reloaded
    here.
    """
    context_id = str(args.get("context_id") or args.get("contextId") or "").strip()
    if not context_id:
        return "Error: 'context_id' is required (see a2a_list for known conversations)."
    try:
        limit = max(1, min(int(args.get("limit") or 50), 200))
    except (ValueError, TypeError):
        limit = 50
    messages = protocol.load_conversation(context_id, limit=limit)
    if not messages:
        return f"No persisted conversation for context '{context_id}'."
    lines = [f"Conversation {context_id} (last {len(messages)} messages):"]
    for m in messages:
        role = m.get("role", "?")
        text = (m.get("text") or "").strip()
        if len(text) > 1000:
            text = text[:1000] + " …[truncated]"
        lines.append(f"[{role}] {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# a2a_orchestrate: capability-based routing with fan-out
# --------------------------------------------------------------------------

def _match_peers_by_capability(capability: str) -> list[tuple[str, dict]]:
    """Find configured peers that advertise the given capability."""
    cfg = _load_config()
    peers = cfg.get("a2a_agents") or {}
    matches = []
    for name, entry in peers.items():
        caps = entry.get("capabilities", []) or []
        if capability in caps or capability == "*":
            matches.append((name, entry))
    return matches


def _call_peer_sync(agent_name: str, peer_entry: dict, message: str, context_id: str = "") -> tuple[str, str]:
    """Call a single peer synchronously. Returns (agent_name, reply_text)."""
    try:
        peer = {
            "url": peer_entry.get("url", ""),
            "auth": peer_entry.get("auth", {}) or {},
            "headers": peer_entry.get("headers", {}) or {},
            "tenant": peer_entry.get("tenant", ""),
            "timeout": int(peer_entry.get("timeout", _DEFAULT_TIMEOUT)),
            # Forwarded so fan-out peers keep their 524-retry opt-in and
            # cross-origin RPC allowlist (same semantics as a2a_call).
            "idempotency": bool(peer_entry.get("idempotency", False)),
            "allowed_rpc_origins": peer_entry.get("allowed_rpc_origins") or [],
        }
        reply, _ctx, _state = _send_task(agent_name, peer, message, context_id)
        return (agent_name, reply or "(no reply)")
    except Exception as e:
        return (agent_name, f"Error: {e}")


def a2a_orchestrate(args: dict, **_: Any) -> str:
    """Fan-out a task to multiple peer agents by capability.

    Modes:
      - ``all``: send to all peers matching the capability, return all replies.
      - ``first``: send to all matching peers, return the first successful reply.
      - ``best``: send to all, return the longest successful reply (a coarse
        detail heuristic — use ``all`` when you want to judge yourself).

    Configured peers advertise capabilities in config.yaml::

      a2a_agents:
        researcher:
          url: "http://localhost:9991"
          capabilities: [web_search, research]
        coder:
          url: "http://localhost:9992"
          capabilities: [code, debug]
    """
    capability = str(args.get("capability") or "").strip()
    message = str(args.get("message") or args.get("task") or "").strip()
    mode = str(args.get("mode") or "all").strip().lower()
    context_id = str(args.get("context_id") or "").strip()

    if not message:
        return "Error: 'message' is required."
    if not capability:
        return "Error: 'capability' is required (or use '*' for all peers)."

    matches = _match_peers_by_capability(capability)
    if not matches:
        return f"Error: no configured peers advertise capability '{capability}'."

    if mode not in ("all", "first", "best"):
        mode = "all"

    # Fan-out
    results: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(len(matches), _ORCHESTRATE_MAX_WORKERS)) as pool:
        futures = {
            pool.submit(_call_peer_sync, name, entry, message, context_id): name
            for name, entry in matches
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results.append(fut.result())
                if mode == "first" and not results[-1][1].startswith("Error:"):
                    # Got a good reply; cancel peers that haven't started yet.
                    for f in futures:
                        f.cancel()
                    break
            except Exception as e:
                results.append((name, f"Error: {e}"))

    # Sort results by peer name for deterministic output
    results.sort(key=lambda r: r[0])
    successes = [(name, reply) for name, reply in results if not reply.startswith("Error:")]

    def _all_failed() -> str:
        lines = ["All peers failed:"]
        for name, reply in results:
            lines.append(f"  {name}: {reply}")
        return "\n".join(lines)

    if mode == "best":
        if not successes:
            return _all_failed()
        best = max(successes, key=lambda r: len(r[1]))
        return f"[best: {best[0]}]\n{best[1]}"
    elif mode == "first":
        if not successes:
            return _all_failed()
        name, reply = successes[0]
        return f"[first: {name}]\n{reply}"
    else:  # mode == "all"
        lines = [f"Orchestrated '{capability}' to {len(matches)} peer(s):"]
        for name, reply in results:
            lines.append(f"\n--- {name} ---")
            lines.append(reply)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Tool schemas + registration
# --------------------------------------------------------------------------

_FunctionSchema = TypedDict("_FunctionSchema", {"name": str, "description": str, "parameters": dict[str, Any]}, total=False)
_ToolSchema = TypedDict("_ToolSchema", {"type": str, "function": _FunctionSchema}, total=False)
_SCHEMAS: dict[str, _ToolSchema] = {
    "a2a_discover": {
        "type": "function",
        "function": {
            "name": "a2a_discover",
            "description": (
                "Fetch and summarize another agent's A2A Agent Card from a URL "
                "(its name, description, capabilities, and skills). Use this to "
                "find out what a remote agent can do before calling it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Base URL of the remote A2A agent, e.g. http://localhost:9999"},
                },
                "required": ["url"],
            },
        },
    },
    "a2a_call": {
        "type": "function",
        "function": {
            "name": "a2a_call",
            "description": (
                "Send a natural-language task to a remote A2A agent and return "
                "its reply. The agent is a peer (any A2A-compliant framework), "
                "not a sub-agent you control. Pass 'context_id' from a previous "
                "reply to continue a multi-turn exchange."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "description": "Configured peer name (from a2a_agents) or a full http(s):// URL."},
                    "message": {"type": "string", "description": "The task / message to send the peer, in natural language."},
                    "context_id": {"type": "string", "description": "Optional: context id from a prior reply, to continue the conversation."},
                },
                "required": ["agent", "message"],
            },
        },
    },
    "a2a_list": {
        "type": "function",
        "function": {
            "name": "a2a_list",
            "description": "List configured A2A peer agents, persisted A2A conversations, and metrics.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "a2a_history": {
        "type": "function",
        "function": {
            "name": "a2a_history",
            "description": (
                "Recall a persisted A2A conversation transcript by context_id "
                "(survives restarts and context compaction). Use a2a_list to "
                "see known context ids."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "context_id": {"type": "string", "description": "Context id of the conversation to recall."},
                    "limit": {"type": "integer", "description": "Max messages to return (default 50, max 200)."},
                },
                "required": ["context_id"],
            },
        },
    },
    "a2a_orchestrate": {
        "type": "function",
        "function": {
            "name": "a2a_orchestrate",
            "description": (
                "Fan-out a task to multiple peer agents by capability. Peers are "
                "matched from config.yaml a2a_agents.*.capabilities. Modes: 'all' "
                "(return all replies), 'first' (first successful), 'best' (longest "
                "successful reply)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "description": "Capability to match (e.g. 'research', 'code') or '*' for all peers."},
                    "message": {"type": "string", "description": "The task to send to all matching peers."},
                    "mode": {"type": "string", "enum": ["all", "first", "best"], "description": "How to aggregate results. Default: 'all'."},
                    "context_id": {"type": "string", "description": "Optional: shared context id for all peers."},
                },
                "required": ["capability", "message"],
            },
        },
    },
}

_HANDLERS = {
    "a2a_discover": a2a_discover,
    "a2a_call": a2a_call,
    "a2a_list": a2a_list,
    "a2a_history": a2a_history,
    "a2a_orchestrate": a2a_orchestrate,
}


def _a2a_tools_available() -> bool:
    """check_fn for the outbound client tools: serve them ONLY when the
    operator has opted into A2A somehow — peers configured under
    ``a2a_agents`` in config.yaml, or the inbound platform enabled
    (a peer-reachable Hermes plausibly dials back).

    Maintainer-directed (#95681): these registered unconditionally, so
    every session on every install paid ~561 tok/call for tools whose
    only possible output without config is 'no peers configured'. A2A is
    unrelated to Bot Mode (bots talk over gateway RPCs) — for most
    installs this toolset is foreign-agent plumbing they never enabled.
    Config adds mid-session surface at the next compaction (#97073).
    """
    cfg = {}
    try:
        cfg = _load_config()
        if cfg.get("a2a_agents"):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        import os as _os

        if _os.getenv("A2A_PORT"):
            return True
        platforms = cfg.get("platforms") or {}
        a2a_cfg = platforms.get("a2a") or {}
        if isinstance(a2a_cfg, dict) and a2a_cfg.get("enabled"):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def register_tools(ctx) -> None:
    """Register the client tools in the ``a2a`` toolset (config-gated)."""
    for name, schema in _SCHEMAS.items():
        # The registry stores schemas as-is and ``get_definitions()`` wraps
        # them in {"type": "function", "function": ...}. ``_SCHEMAS`` entries
        # are pre-wrapped for OpenAI compatibility, so unwrap here — passing
        # them wrapped would double-nest them and the model-facing tool defs
        # (and ``tool_describe``) would come back empty.
        # NOTE: upstream landed the same unwrap as cefeed4ca using hard
        # indexing; this variant keeps the defensive ``.get()`` fallbacks
        # and is a behavioral superset of it.
        flat = schema.get("function", schema)
        ctx.register_tool(
            name=name,
            toolset="a2a",
            schema=flat,
            handler=_HANDLERS[name],
            description=flat.get("description", ""),
            emoji="\U0001f9e9",  # puzzle piece
            check_fn=_a2a_tools_available,
        )
