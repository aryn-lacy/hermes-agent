# Why this fork exists

This is the **Aryn Lacy flavor of [Hermes Agent](https://github.com/NousResearch/hermes-agent)** — a working fork, not a redesign. It exists because some bugs that bite this deployment daily sat unfixed upstream, and a real agent can't wait on a merge window to stay alive.

**Ground rules:**

- Upstream-first. Every fix here is either an open PR, a candidate PR, or a genuine fork-only need (macOS-specific, deployment-specific). Nothing here is a feature divergence.
- Upstream is merged continuously — a scheduled sync job on another machine merges `NousResearch/hermes-agent` into this fork's `main` daily, so this fork never drifts more than a day or so.
- Fixes that upstream later absorbs get retired from this list. Everything below was verified against upstream `main` as of **2026-10-08**.

## Still broken upstream — carried here

### A2A (agent-to-agent) platform stack — the big one

A dozen patches rebuilding the A2A platform into something that survives a real deployment. Live in `plugins/platforms/a2a/` and `tests/`.

- **SSE streaming client** — `message/stream` support on the refactored transport (surgical port of upstream PR [#86369](https://github.com/NousResearch/hermes-agent/pull/86369), still unmerged).
- **524-indeterminate retry semantics** — upstream's Cloudflare-fronted endpoints return 524 on long tasks; this fork adopted the indeterminate-retry contract with an idempotent retry ladder instead of naive re-POSTs.
- **Idempotent task dedup + cross-restart ReplyStore** — task IDs are deterministic; replies persist across gateway restarts so retries can't double-dispatch work.
- **Per-peer configuration** — custom headers, a `Hermes-A2A` User-Agent, and per-peer `max_turns` with turn-budget metadata and a transport-failure refund on the new adapter flow.
- **Fail-closed security posture** — cross-origin redirects fail closed, origin-level allowlists, credentials only sent to the configured origin, RPC origin binding.

Verified fork-only upstream on 2026-10-08: `retry_524`, `ReplyStore`, per-peer headers, the UA string, SSE event plumbing, and origin allowlists all have zero hits in upstream `main`.

### Gateway E2EE send guard

`_gateway_process_running` in `tools/send_message_senders.py`. When Hermes itself runs inside the gateway process, sending through the same gateway can deadlock the Matrix E2EE path — this guard refuses the send instead of wedging the room. Dropped once by an upstream decomposition refactor and restored here; still absent upstream (verified 2026-10-08).

### Darwin (macOS) Matrix E2EE self-heal

`plugins/platforms/matrix/adapter_darwin_e2ee.py`, dispatched from a tri-state hook in `adapter.py` (Linux path unchanged, byte-identical). Upstream gates the `[matrix]` platform extra to Linux, so macOS updates migrate the runtime into a fresh managed venv with no `pip` and silently lose Matrix for ~20 minutes per update. This patch detects the missing-deps state and self-heals it. Merged 2026-10-08 (PR #3); still macOS-only pain upstream.

### Matrix structural sync-auth classification

Matrix sync loops that die on transient Cloudflare/WAF errors get misclassified as "unknown token" failures, killing a healthy session permanently. This fork classifies auth errors structurally (permanent vs transient) and the sync loop survives what used to be fatal. Still fork-only (verified 2026-10-08).

### Google Meet: click "Join anyway"

Meet sometimes offers an interstitial confirmation; unattended joins would hang on it. The plugin clicks it when offered. Trivial, but unattended bots can't click for themselves.

## Fixed here first, since landed upstream

These used to be fork-only patches. Upstream now carries equivalents, so this fork no longer claims them — listed so patch archaeology doesn't misattribute them:

- **Kanban stop-guard** (`kanban_request_review` as a terminal tool in `agent/kanban_stop.py`) — absorbed upstream.
- **Cron ticker gateway-liveness gate** (`_gateway_owns_cron` in `hermes_cli/web_server.py`) — absorbed upstream.
- **Cron external-worker boot ordering** (PM dependency boot before `cron.jobs`, committed-generation selection, dependency-generation lease at worker entry) — landed upstream under the same titles (`308072df`, `9ec6b94e`, `94f83d97`).
- **SSH ControlMaster socket-safe temp root** — the macOS 104-byte `sun_path` limit broke every SSH tool call under deep `$TMPDIR`s; upstream shipped its own deterministic-socket fix.
- **Web build TS1484 regression** (`type`-only imports for the `SessionsPage_sources` types) — fixed here within hours of the break; upstream independently auto-fixed it.

## Housekeeping

- `contributors/` maps commit emails so fork contributors show up correctly in upstream-derived reports.
- The daily upstream-merge automation runs on a separate host against this repo's GitHub `main`; the local live checkout only fast-forwards to it after container validation.

## Sync & validation workflow

1. Measure the gap against this fork's GitHub `main`, never a stale local checkout.
2. Inventory fork-only patches by patch-id (`git cherry`), not commit ranges.
3. Dry-run the merge (`git merge-tree`), then merge in a scratch clone — never the live tree.
4. Verify every patch survived by signature grep in the merged tree.
5. Container-validate (Docker/podman build + one-shot chat round-trip) before any live swap.
6. After the swap, check every platform adapter actually connected — signature-verified patches don't prove the platform extras came up.
