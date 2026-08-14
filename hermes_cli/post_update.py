"""Post-update maintenance steps shared by ``hermes update`` and boot bootstrap.

Each step operates on user state (config.yaml, skills, state.db) or machine
state (cua-driver), never on the install tree. Every step is idempotent and
self-gating: running it twice, or from two installs that share one
HERMES_HOME, converges. The caller (boot_bootstrap, update_cmd) decides WHEN
steps run; this module owns WHAT they do.

Steps declare a scope:

* ``home``    — mutates the active HERMES_HOME (per profile).
* ``machine`` — machine-global state shared by every profile.

The scopes must match the record that gates them in ``boot_bootstrap``
(home record vs machine record). See
.hermes/plans/2026-08-10_163500-boot-time-post-update-bootstrap.md.
"""
from __future__ import annotations

import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# config migration (backup / migrate / verify / restore)
# ---------------------------------------------------------------------------

def _backup_path(path: Path, stamp: str) -> Path:
    base = path.with_name(f"{path.name}.bak-{stamp}")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.bak-{stamp}.{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not choose a backup path for {path}")


def _backup_existing(paths: Iterable[Path]) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: dict = {}
    for path in paths:
        if not path.is_file():
            continue
        dest = _backup_path(path, stamp)
        shutil.copy2(path, dest)
        backups[path] = dest
    return backups


def _restore_backups(backups: dict) -> list:
    restored = []
    for original, backup in backups.items():
        if not backup.is_file():
            continue
        shutil.copy2(backup, original)
        restored.append(original)
    return restored


def step_migrate_config() -> dict:
    """Migrate config.yaml to the current schema, non-interactively.

    Same shape as scripts/docker_config_migrate.py: back up config + .env,
    migrate, verify the version advanced, restore the backups on failure.
    No-op when the on-disk version is current (the 99% case).
    """
    from hermes_cli.config import (
        check_config_version,
        get_config_path,
        get_env_path,
        migrate_config,
    )
    from hermes_cli.config_migrations import (
        SUPPORT_FLOOR_VERSION,
        support_floor_message,
    )

    current_ver, latest_ver = check_config_version()
    if current_ver >= latest_ver:
        return {"ok": True, "skipped": "up-to-date"}
    if current_ver < SUPPORT_FLOOR_VERSION:
        # migrate_config() refuses sub-floor configs and leaves the file
        # untouched; warn instead of failing the boot.
        logger.warning("config migration skipped: %s", support_floor_message())
        return {"ok": True, "skipped": "below-support-floor"}

    backups = _backup_existing((get_config_path(), get_env_path()))
    try:
        migrate_config(interactive=False, quiet=True)
    except Exception:
        _restore_backups(backups)
        raise
    post_ver, _ = check_config_version()
    if post_ver < latest_ver:
        restored = _restore_backups(backups)
        raise RuntimeError(
            f"migration did not advance config version to {latest_ver} "
            f"(still {post_ver}); restored: "
            + (", ".join(str(p) for p in restored) if restored else "none")
        )
    return {"ok": True, "migrated": f"{current_ver}->{latest_ver}"}


# ---------------------------------------------------------------------------
# skills sync (this home only — profiles self-serve on their own boot)
# ---------------------------------------------------------------------------

def step_sync_skills() -> dict:
    """Sync bundled skills into the active home. Content-diffed, respects
    user modifications and deletions; converges on repeat runs."""
    from tools.skills_sync import sync_skills

    result = sync_skills(quiet=True) or {}
    return {
        "ok": True,
        "copied": len(result.get("copied") or []),
        "updated": len(result.get("updated") or []),
    }


# ---------------------------------------------------------------------------
# state.db integrity guard (#68474 — check-only variant)
# ---------------------------------------------------------------------------

def step_state_db_guard() -> dict:
    """Verify the active home's state.db is intact.

    Boot bootstrap has no pre-update snapshot to restore from (that pairing
    lives in ``hermes update``), so this is detection: a corrupt db is
    surfaced loudly in the log instead of the user silently losing session
    search. Read-only, idempotent.
    """
    from hermes_constants import get_hermes_home
    from hermes_cli.backup import verify_sqlite_integrity

    state_path = get_hermes_home() / "state.db"
    if not state_path.exists():
        return {"ok": True, "skipped": "no-state-db"}
    result = verify_sqlite_integrity(state_path, check_header=True, run_pragma=True)
    if result.get("valid"):
        return {"ok": True}
    message = result.get("message", "unknown error")
    logger.error(
        "state.db failed integrity check after a code update: %s — "
        "restore a backup with `hermes backup` tooling or contact support",
        message,
    )
    return {"ok": False, "error": message}


# ---------------------------------------------------------------------------
# cua-driver refresh (machine scope)
# ---------------------------------------------------------------------------

def step_cua_driver_refresh() -> dict:
    """Refresh the Computer Use driver when a newer release is CONFIRMED.

    Config-gated (``updates.refresh_cua_driver``) and no-op unless the
    binary is already installed. ``require_confirmed_update`` keeps an
    indeterminate check (offline, rate-limited) from costing the
    multi-minute upstream installer.
    """
    refresh = True
    try:
        from hermes_cli.config import load_config

        update_cfg = (load_config() or {}).get("updates", {})
        if isinstance(update_cfg, dict):
            refresh = bool(update_cfg.get("refresh_cua_driver", True))
    except Exception as exc:
        logger.debug("Could not read updates.refresh_cua_driver: %s", exc)

    if not refresh:
        return {"ok": True, "skipped": "config-disabled"}
    if sys.platform not in ("darwin", "win32", "linux") or not shutil.which("cua-driver"):
        return {"ok": True, "skipped": "not-installed"}

    from hermes_cli.tools_config import install_cua_driver

    ok = install_cua_driver(
        upgrade=True,
        require_confirmed_update=True,
        show_installer_progress=False,
    )
    return {"ok": bool(ok)}


def step_provision_runtimes() -> dict:
    """Provision managed runtime tools (node, npm, uv, git, gh, ripgrep) into
    the install-scoped runtime dir from runtime-pins.json. THE dep engine
    for updates AND fresh installs (the installers run
    ``python -m installation.provisioner`` directly); see
    installation/provisioner.py."""
    from installation.provisioner import step_provision_runtimes as _run

    return _run()


# ---------------------------------------------------------------------------
# step registries — boot_bootstrap gates each list with the matching record
# ---------------------------------------------------------------------------

HOME_STEPS: tuple = (
    ("migrate_config", step_migrate_config),
    ("sync_skills", step_sync_skills),
    ("state_db_guard", step_state_db_guard),
)

# Machine steps may be slow (network installers); boot bootstrap runs them
# AFTER writing the machine record, detached from boot readiness.
MACHINE_STEPS: tuple = (
    ("cua_driver_refresh", step_cua_driver_refresh),
    ("provision_runtimes", step_provision_runtimes),
)


def run_steps(steps: Iterable) -> dict:
    """Run steps in order; one failure never stops the rest.

    Returns ``{name: result_dict}``. A raising step records
    ``{"ok": False, "error": str}`` — the caller still writes its record so
    a broken step cannot retrigger the slow path on every boot.
    """
    results: dict = {}
    for name, func in steps:
        try:
            results[name] = func()
        except Exception as exc:
            logger.warning("post-update step %s failed: %s", name, exc)
            results[name] = {"ok": False, "error": str(exc)}
    return results


def main(argv: list | None = None) -> int:
    """``python -m hermes_cli.post_update`` — run in a FRESH interpreter
    so every step imports post-pull code (no reload lists).

    Two modes:

    * default / ``--scope``: the boot-bootstrap step registries.
    * ``--update-phase``: the full ``hermes update`` post-update phase
      (``update_cmd._run_update_phase_inline``) — config prompt/migration,
      skills sync, state.db guard, notices, self-heals, cua refresh, and
      the gateway fleet restart. ``hermes update`` spawns this with
      inherited stdio; the desktop's streamed-update consumer forwards
      our lines unchanged.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="hermes_cli.post_update")
    parser.add_argument("--scope", choices=("home", "machine", "all"), default="all")
    parser.add_argument("--update-phase", action="store_true")
    parser.add_argument("--gateway-mode", action="store_true")
    parser.add_argument("--assume-yes", action="store_true")
    parser.add_argument("--pre-update-snapshot-id", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.update_phase:
        # This process was just born, so update_cmd and everything it
        # imports come from the pulled tree. The in-function reload
        # band-aids (_reload_config_modules) turn into no-ops here —
        # the "fresh" modules ARE the loaded modules.
        from hermes_cli.update_cmd import _run_update_phase_inline

        return _run_update_phase_inline(
            gateway_mode=args.gateway_mode,
            assume_yes=args.assume_yes,
            pre_update_snapshot_id=args.pre_update_snapshot_id,
            windows_gateway_resume=None,
        )

    selected: list = []
    if args.scope in ("home", "all"):
        selected.extend(HOME_STEPS)
    if args.scope in ("machine", "all"):
        selected.extend(MACHINE_STEPS)
    results = run_steps(selected)
    failed = [name for name, res in results.items() if not res.get("ok")]
    for name, res in results.items():
        state = "ok" if res.get("ok") else f"FAILED ({res.get('error')})"
        skipped = res.get("skipped")
        print(f"  post-update {name}: {f'skipped ({skipped})' if skipped else state}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
