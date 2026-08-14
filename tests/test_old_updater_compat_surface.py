"""An `hermes update` already in the wild must survive meeting this tree.

`hermes update` swaps the checkout under its own feet. The Python process
keeps running the code it started with, but the files underneath it are
the ones we just pulled, so anything it loads from disk after that point
comes from the NEW tree. Those names are a contract with every released
updater: delete one and the users on that release get a traceback
halfway through an update, on a checkout that is already half-new — the
state least likely to be recoverable and least likely to be trusted.

`managed_uv._reload_hermes_constants` is the scar tissue proving this is
not hypothetical: an updater hit ``cannot import name 'venv_python_path'
from 'hermes_constants'`` while the file on disk plainly contained the
name.

The surface below is DERIVED, not invented: run

    python scripts/audit-old-updater-imports.py

which walks every commit on origin/main that ever touched the update
flow, follows the call graph out of the update entrypoints, and collects
three kinds of load — lazy ``import``, ``importlib.reload`` (which
re-executes the new file in the old process), and ``getattr`` on a
module fetched from ``sys.modules``.

To DELETE something listed here: prove no shipped updater reaches it,
then remove it from both the code and this list in the same commit. A
name kept alive costs one thin forwarding function. A name removed too
early costs somebody's install.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "audit-old-updater-imports.py"


# Frozen 2026-08-14 from the audit script over 69 shipped commits.
# module -> the names an old updater can load out of the new tree.
# "<module>" means the module itself is imported or reloaded, so the
# FILE has to keep existing and keep importing cleanly.
FROZEN_COMPAT_SURFACE: dict[str, tuple[str, ...]] = {
    "agent": ("curator",),
    "gateway.status": ("_get_process_start_time", "_pid_exists", "terminate_pid"),
    "hermes_cli": ("gateway_windows", "main"),
    "hermes_cli._scan_venv_blockers": ("_is_pausable_gateway",),
    "hermes_cli.backup": (
        "_quick_snapshot_root",
        "create_pre_update_backup",
        "create_quick_snapshot",
        "restore_cron_jobs_if_emptied",
        "verify_sqlite_integrity",
    ),
    "hermes_cli.config": (
        "<module>",
        "check_config_version",
        "get_hermes_home",
        "get_missing_config_fields",
        "get_missing_env_vars",
        "load_config",
        "migrate_config",
    ),
    "hermes_cli.config_defaults": ("<module>",),
    "hermes_cli.config_migrations": ("<module>",),
    "hermes_cli.gateway": (
        "_capture_gateway_argv",
        "_ensure_user_systemd_env",
        "_find_legacy_hermes_units",
        "_get_restart_drain_timeout",
        "_get_restart_exit_wait_budget",
        "_get_service_pids",
        "_graceful_restart_via_sigusr1",
        "_prepare_profile_gateway_update_restart",
        "_wait_for_gateway_exit",
        "find_gateway_pids",
        "find_profile_gateway_processes",
        "get_launchd_label",
        "get_launchd_plist_path",
        "has_legacy_hermes_units",
        "is_macos",
        "launch_detached_gateway_restart_by_cmdline",
        "launch_detached_profile_gateway_restart",
        "launchd_restart",
        "supports_systemd_services",
    ),
    "hermes_cli.main": ("_detect_venv_python_processes",),
    "hermes_cli.managed_uv": ("ensure_uv", "resolve_uv", "update_managed_uv"),
    "hermes_cli.memory_setup": ("_install_dependencies",),
    "hermes_cli.model_catalog": ("seed_cache_from_checkout",),
    "hermes_cli.profiles": (
        "backfill_profile_envs",
        "list_profiles",
        "seed_profile_skills",
    ),
    "hermes_cli.psutil_android": ("PSUTIL_URL", "prepare_patched_psutil_sdist"),
    "hermes_cli.sizefmt": ("format_bytes",),
    "hermes_cli.tools_config": ("install_cua_driver",),
    "hermes_constants": (
        "<module>",
        "FIRST_PARTY_MODULE_ROOTS",
        "display_hermes_home",
        "get_default_hermes_root",
        "get_hermes_home",
        "get_process_hermes_home",
        "is_wsl",
        "venv_python_path",
        "with_hermes_node_path",
    ),
    "hermes_state": ("SessionDB",),
    "plugins.memory.honcho.cli": ("sync_honcho_profiles_quiet",),
    "tools": ("lazy_deps",),
    "tools.browser_tool": ("warm_agent_browser_npx_cache",),
    "tools.environments.local": ("<module>",),
    "tools.lazy_deps": ("<module>",),
    "tools.skills_sync": ("sync_skills",),
}

# Names an old updater imports that we deliberately DO NOT keep, with the
# reason. Each entry is a decision, not an oversight.
ACCEPTED_BREAKS: dict[tuple[str, str], str] = {
    ("hermes_constants", "DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT"): (
        "Moved to gateway/restart.py. The importing updaters read it inside "
        "a try/except that falls back to a literal default, so the import "
        "failing degrades the drain timeout rather than breaking the update."
    ),
}


def _module_file(module: str) -> Path | None:
    rel = Path(module.replace(".", "/"))
    for candidate in (REPO_ROOT / f"{rel}.py", REPO_ROOT / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _defines(path: Path, symbol: str) -> bool:
    """Is *symbol* reachable as an attribute of the module at *path*?

    Parsed, not imported: importing would RUN the module, and the
    question is what an updater finds on disk.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == symbol:
                    return True
    return False


_CASES = [
    (module, symbol)
    for module, symbols in sorted(FROZEN_COMPAT_SURFACE.items())
    for symbol in symbols
]


@pytest.mark.parametrize(
    "module,symbol", _CASES, ids=[f"{m}.{s}" for m, s in _CASES]
)
def test_the_name_an_old_updater_imports_still_exists(module: str, symbol: str):
    path = _module_file(module)
    assert path is not None, (
        f"{module} is gone. A shipped `hermes update` imports it AFTER "
        f"replacing the checkout, so removing it breaks that update "
        f"mid-flight. Keep a shim, or prove no released updater reaches it "
        f"and drop it from FROZEN_COMPAT_SURFACE in the same commit."
    )

    if symbol == "<module>":
        return  # the file existing IS the requirement

    # `from hermes_cli import gateway_windows` names a submodule.
    submodule = REPO_ROOT / Path(module.replace(".", "/")) / symbol
    if submodule.with_suffix(".py").is_file() or (submodule / "__init__.py").is_file():
        return

    assert _defines(path, symbol), (
        f"{module}.{symbol} is gone from {path.relative_to(REPO_ROOT)}. A "
        f"shipped `hermes update` imports this name after replacing the "
        f"checkout: the update would die with an ImportError on a half-new "
        f"tree. Keep a forwarding shim, or prove no released updater reaches "
        f"it and drop it from FROZEN_COMPAT_SURFACE in the same commit."
    )


def test_accepted_breaks_are_still_actually_broken():
    """An accepted break that healed should leave the exception list.

    Otherwise the list grows into a graveyard of stale excuses and the
    next reader cannot tell which entries still describe reality.
    """
    healed = []
    for (module, symbol), _reason in ACCEPTED_BREAKS.items():
        path = _module_file(module)
        if path is not None and _defines(path, symbol):
            healed.append(f"{module}.{symbol}")
    assert not healed, (
        f"These are listed in ACCEPTED_BREAKS but exist again: {healed}. "
        f"Move them into FROZEN_COMPAT_SURFACE."
    )


def test_the_frozen_surface_matches_what_the_audit_derives():
    """The list is generated; regenerate it rather than editing by hand.

    Without this the frozen list silently rots: a new release adds a lazy
    import, nobody re-runs the script, and the next deletion sails past a
    green suite. Skipped when the git history is unavailable (a shallow
    CI clone, an exported tarball) — there is nothing to compare against
    there, and failing would only punish the checkout shape.
    """
    if not AUDIT_SCRIPT.is_file():
        pytest.skip("audit script not present")

    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/main"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip("origin/main not available in this checkout")

    import json
    import sys

    proc = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode not in (0, 1):
        pytest.skip(f"audit script could not run: {proc.stderr[-300:]}")

    derived_raw = json.loads(proc.stdout)["required"]
    derived: dict[str, set[str]] = {}
    for entry in derived_raw:
        derived.setdefault(entry["module"], set()).add(entry["symbol"] or "<module>")

    frozen = {m: set(s) for m, s in FROZEN_COMPAT_SURFACE.items()}
    for module, symbol in ACCEPTED_BREAKS:
        frozen.setdefault(module, set()).add(symbol)

    new_names = {
        f"{module}.{symbol}"
        for module, symbols in derived.items()
        for symbol in symbols - frozen.get(module, set())
    }

    assert not new_names, (
        f"The update flow gained {len(new_names)} post-swap requirement(s) "
        f"that are not frozen yet: {sorted(new_names)}. Re-run "
        f"`python scripts/audit-old-updater-imports.py` and update "
        f"FROZEN_COMPAT_SURFACE."
    )
