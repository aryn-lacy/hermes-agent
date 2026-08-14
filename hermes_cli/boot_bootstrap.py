"""Boot-time post-update bootstrap.

Every install kind (git checkout, desktop bundled payload, docker, nix)
compares two per-install facts at boot:

* current identity — the commit this install IS: ``install-stamp.json``
  for sealed trees, ``.git/HEAD`` for checkouts. Reading it is a couple of
  file reads, no subprocess.
* last-known identity — the commit this install last bootstrapped, recorded
  under ``install-bootstrap/`` keyed by the canonical install root.

Equal → nothing happens (the fast path, ~2 ms). Different → run the
idempotent post-update steps from ``hermes_cli.post_update`` under a
single-flight lock, then record the new identity.

Two records, one per step scope:

* home record — ``get_hermes_home()/install-bootstrap/<key>.json``.
  Gates home-scoped steps. HERMES_HOME moves per profile, so each profile
  bootstraps its own state once per code change.
* machine record — ``<base home>/install-bootstrap/<key>.machine.json``,
  anchored to the DEFAULT home (HOME-anchored, not HERMES_HOME-anchored —
  the ``_get_profiles_root()`` convention). Every profile resolves the same
  file, so machine-global steps run once per machine per code change and
  the record's lock serializes concurrent profile boots.

The records are an optimization, never the correctness layer: every step is
idempotent and self-gating, so a deleted record costs one redundant slow
path, nothing more.

Design: .hermes/plans/2026-08-10_163500-boot-time-post-update-bootstrap.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RECORD_SCHEMA_VERSION = 1
RECORD_DIR_NAME = "install-bootstrap"
LOCK_STALE_SECONDS = 600


# ---------------------------------------------------------------------------
# current identity
# ---------------------------------------------------------------------------

def read_git_head(root: Path) -> str | None:
    """The commit SHA of the checkout at ``root``, from files alone.

    Worktree-aware: ``.git`` may be a FILE containing ``gitdir: <path>``
    (linked worktrees). Symbolic HEAD is dereferenced through the loose ref,
    then ``packed-refs``. Returns None when anything is missing or garbled.
    """
    try:
        git_path = Path(root) / ".git"
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8", errors="replace").strip()
            if not pointer.startswith("gitdir:"):
                return None
            git_dir = Path(pointer[len("gitdir:"):].strip())
            if not git_dir.is_absolute():
                git_dir = (Path(root) / git_dir).resolve()
        elif git_path.is_dir():
            git_dir = git_path
        else:
            return None

        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if not head.startswith("ref:"):
            # Detached HEAD: the line is the SHA itself.
            return head if len(head) >= 7 else None

        ref = head[len("ref:"):].strip()

        # Worktree gitdirs delegate shared refs to the parent repo via
        # ``commondir`` (usually "../.."). HEAD itself stays per-worktree,
        # but branch refs and packed-refs live in the common dir.
        common = git_dir
        commondir_file = git_dir / "commondir"
        if commondir_file.is_file():
            common_pointer = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
            common = (git_dir / common_pointer).resolve()

        for base in (git_dir, common):
            loose = base / ref
            if loose.is_file():
                sha = loose.read_text(encoding="utf-8", errors="replace").strip()
                return sha if len(sha) >= 7 else None

        for base in (common, git_dir):
            packed = base / "packed-refs"
            if not packed.is_file():
                continue
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
        return None
    except OSError:
        return None


def current_install_identity(project_root: Path) -> str | None:
    """What code this install is: stamp commit for sealed trees, git HEAD
    for checkouts, None for broken trees (never bootstrap, never write)."""
    from installation.tree import read_build_info

    root = Path(project_root)
    if (root / ".git").exists():
        return read_git_head(root)
    commit = read_build_info(root).get("commit")
    if isinstance(commit, str) and len(commit) >= 7:
        return commit
    # A tagless/commitless stamp is a broken artifact; the tag alone is
    # accepted as a weaker identity (bundled artifacts always carry one).
    tag = read_build_info(root).get("tag")
    return tag if isinstance(tag, str) and tag else None


# ---------------------------------------------------------------------------
# last-known records
# ---------------------------------------------------------------------------

def _install_key(project_root: Path) -> str:
    try:
        canonical = str(Path(project_root).resolve())
    except OSError:
        canonical = str(project_root)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def record_path(project_root: Path, scope: str) -> Path:
    """Where the last-known record for ``project_root`` lives.

    ``home`` scope follows the active HERMES_HOME (per profile). ``machine``
    scope anchors to the default home so every profile shares one record.
    """
    if scope == "home":
        from hermes_constants import get_hermes_home

        base = get_hermes_home()
        suffix = ".json"
    elif scope == "machine":
        from hermes_cli.profiles import _get_default_hermes_home

        base = _get_default_hermes_home()
        suffix = ".machine.json"
    else:
        raise ValueError(f"unknown record scope: {scope!r}")
    return base / RECORD_DIR_NAME / f"{_install_key(project_root)}{suffix}"


def read_last_known(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_record(path: Path, identity: str, results: dict) -> None:
    payload = {
        "schemaVersion": RECORD_SCHEMA_VERSION,
        "identity": identity,
        "bootstrappedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "results": results,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_record(project_root: Path, scope: str, identity: str, results: dict | None = None) -> None:
    """Record ``identity`` as bootstrapped. Also used by ``hermes update``
    after it runs the steps itself, so the next boot skips."""
    _write_record(record_path(project_root, scope), identity, results or {})


def needs_bootstrap(project_root: Path, scope: str) -> str | None:
    """The new identity when this install changed since its last bootstrap,
    else None. None identity (broken tree) never bootstraps."""
    identity = current_install_identity(project_root)
    if not identity:
        return None
    known = read_last_known(record_path(project_root, scope))
    if known.get("identity") == identity:
        return None
    return identity


# ---------------------------------------------------------------------------
# single-flight lock
# ---------------------------------------------------------------------------

class _RecordLock:
    """O_CREAT|O_EXCL existence-as-mutex next to a record file.

    Losers skip (boot never waits on another process's bootstrap; the steps
    are idempotent, so a botched winner only costs redundant work later).
    A stale lock — older than LOCK_STALE_SECONDS — is broken and re-tried
    once: a crashed winner died before its record write, so re-running is
    correct.
    """

    def __init__(self, record: Path):
        self.path = record.with_name(record.name + ".lock")
        self.acquired = False

    def _try_create(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(fd, json.dumps({"pid": os.getpid(), "startedAt": time.time()}).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def _is_stale(self) -> bool:
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
            started = float(body.get("startedAt", 0))
        except (OSError, ValueError):
            # Unreadable lock: age it by mtime instead.
            try:
                started = self.path.stat().st_mtime
            except OSError:
                return False
        return (time.time() - started) > LOCK_STALE_SECONDS

    def acquire(self) -> bool:
        if self._try_create():
            self.acquired = True
            return True
        if self._is_stale():
            try:
                self.path.unlink()
            except OSError:
                return False
            if self._try_create():
                self.acquired = True
                return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self.acquired = False


# ---------------------------------------------------------------------------
# the boot entry point
# ---------------------------------------------------------------------------

def run_boot_bootstrap(project_root: Path) -> dict:
    """Run due home- and machine-scoped steps for this install. Returns a
    summary dict (for tests/logs); use maybe_run_boot_bootstrap at call
    sites."""
    from hermes_cli import post_update

    summary: dict = {"home": "skipped", "machine": "skipped"}

    for scope, steps, deferred in (
        ("home", post_update.HOME_STEPS, False),
        ("machine", post_update.MACHINE_STEPS, True),
    ):
        identity = needs_bootstrap(project_root, scope)
        if not identity:
            continue
        record = record_path(project_root, scope)
        lock = _RecordLock(record)
        if not lock.acquire():
            summary[scope] = "lost-race"
            continue
        try:
            # Double-check under the lock: the previous holder may have
            # finished between our read and our acquire.
            if read_last_known(record).get("identity") == identity:
                summary[scope] = "done-by-other"
                continue
            logger.info(
                "post-update bootstrap (%s scope): code changed to %s, running steps",
                scope, identity[:12],
            )
            if deferred:
                # Slow machine steps (network installers) must not block
                # boot readiness: record first, then run detached. A crash
                # mid-step leaves the record written — intended: the record
                # gates "did we trigger for this identity", and the steps
                # re-gate themselves (confirmed-update checks) next change.
                _write_record(record, identity, {"deferred": True})
                import threading

                threading.Thread(
                    target=post_update.run_steps,
                    args=(steps,),
                    name=f"hermes-bootstrap-{scope}",
                    daemon=True,
                ).start()
                summary[scope] = "deferred"
            else:
                results = post_update.run_steps(steps)
                _write_record(record, identity, results)
                summary[scope] = results
        finally:
            lock.release()
    return summary


def maybe_run_boot_bootstrap(project_root: Path) -> None:
    """The one call boot paths use. Never raises: a bootstrap problem must
    not stop the gateway/serve/CLI from starting."""
    try:
        run_boot_bootstrap(Path(project_root))
    except Exception as exc:
        logger.warning("boot bootstrap failed (continuing boot): %s", exc)
