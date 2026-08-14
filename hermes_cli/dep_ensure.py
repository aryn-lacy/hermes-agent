"""Lazy dependency bootstrapper for non-Python runtime deps.

Detection and prompting live here in Python — not in install.sh — because:
  1. shutil.which() works on every platform; install.sh needs bash.
  2. Detection is instant; spawning bash for a "is node installed?" check is waste.
  3. Python controls the UX (rich prompts, non-interactive fallback, TTY detection).

install.sh is still the *installation* backend because it has 1900 lines of
battle-tested OS detection and package-manager logic (apt/brew/pacman/dnf/
zypper/Termux/…).  Reimplementing that in Python would be huge duplication.

Deps that degrade gracefully (ripgrep → grep fallback, ffmpeg → skip conversion)
don't need ensure_dependency wired in — only hard-fail sites do (TUI needs node,
browser tool needs agent-browser).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

from hermes_constants import agent_browser_runnable
from tools.environments.local import hermes_subprocess_env

_IS_WINDOWS = platform.system() == "Windows"


def _node_present() -> bool:
    """The pinned node is provisioned at install time; this reports on it."""
    from installation import registry

    return registry.tool_path("node") is not None


def _managed_camofox_present() -> bool:
    """The camofox browser server sidecar AND the browser it drives.

    Two artifacts, two mechanisms, both required before browsing works:
    the npm module (scripts/camofox-browser, integrity-pinned by its own
    package-lock.json) and the Camoufox browser binary (pinned per target
    with a sha256 in installation/runtime-pins.json).
    """
    from installation import registry

    if registry.tool_path("camoufox") is None:
        return False
    return (_camofox_sidecar_dir() / "node_modules" / ".package-lock.json").is_file()


def _camofox_sidecar_dir() -> Path:
    """Where the camofox npm sidecar lives (a checkout-relative path)."""
    return Path(__file__).resolve().parent.parent / "scripts" / "camofox-browser"


_DEP_CHECKS = {
    # The recorded fact rather than a bare which(): the managed tree is not on
    # PATH, so which() would report Node missing on an install that has one and
    # trigger a redundant re-install.
    "node": _node_present,
    "browser": lambda: (
        _managed_camofox_present()
        or agent_browser_runnable(shutil.which("agent-browser"))
        or _has_system_browser()
        or _has_hermes_agent_browser()
        or _has_npx_agent_browser()
    ),
    "ripgrep": lambda: shutil.which("rg") is not None,
    "ffmpeg": lambda: shutil.which("ffmpeg") is not None,
}

# Deps whose whole install is "stage this pinned tool". The provisioner
# downloads the exact pinned artifact, verifies its digest before
# extraction, brings up whatever it extends, and records it in the
# install's runtimes.json — so a second ensure_dependency() call is a
# no-op and `hermes update`'s sweep keeps it at the pin from then on.
# Deps NOT listed here still shell out to install.sh/install.ps1, which
# owns the OS package-manager work Python has no business restating.
_PINNED_DEPS = {
    "node": "node",
    "ripgrep": "ripgrep",
}

_DEP_DESCRIPTIONS = {
    "node": "Node.js (required for browser tools and TUI)",
    "browser": "Browser engine (Chromium, for web browsing tools)",
    "ripgrep": "ripgrep (fast file search)",
    "ffmpeg": "ffmpeg (TTS voice messages)",
}


def _has_system_browser() -> bool:
    if _IS_WINDOWS:
        names = ("chrome", "msedge", "chromium")
    else:
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
    for name in names:
        if shutil.which(name):
            return True
    return False


def _has_npx_agent_browser() -> bool:
    """agent-browser resolves lazily via npx on the default install (#43564),
    invisible to the PATH/managed-dir probes above. Mirror
    tools.browser_tool.check_browser_requirements's Termux carve-out so this
    check can't diverge from what browser tools actually find."""
    try:
        from tools.browser_tool import (
            _find_agent_browser,
            _is_npx_agent_browser_sentinel,
            _requires_real_termux_browser_install,
        )
        browser_cmd = _find_agent_browser(validate=False)
    except Exception:
        return False
    if not _is_npx_agent_browser_sentinel(browser_cmd):
        return False
    return not _requires_real_termux_browser_install(browser_cmd)


def _has_hermes_agent_browser() -> bool:
    from installation import env as runtime_env

    # The managed Node tree is install-scoped; managed_path_dirs owns
    # where it lives and the per-platform layout order (npm -g --prefix
    # drops .cmd shims in the prefix root on Windows, bin/ on POSIX).
    name = "agent-browser.cmd" if _IS_WINDOWS else "agent-browser"
    return any(
        (directory / name).is_file() for directory in runtime_env.managed_path_dirs()
    )


def _find_install_script(
    package_dir: Path | None = None,
    repo_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Locate the install script — bundled in wheel or in git checkout.

    On Windows, prefers install.ps1; on POSIX, prefers install.sh.
    Returns a (path, shell) tuple, or (None, None) if neither is found.
    """
    if package_dir is None:
        package_dir = Path(__file__).parent
    if repo_root is None:
        repo_root = package_dir.parent

    if _IS_WINDOWS:
        preferred = ("install.ps1", "powershell")
        fallback = ("install.sh", "bash")
    else:
        preferred = ("install.sh", "bash")
        fallback = ("install.ps1", "powershell")

    for script_name, shell in (preferred, fallback):
        bundled = package_dir / "scripts" / script_name
        if bundled.is_file():
            return bundled, shell
        repo = repo_root / "scripts" / script_name
        if repo.is_file():
            return repo, shell

    return None, None


def _install_browser(interactive: bool) -> bool:
    """Stage the pinned browser binary, then the npm sidecar that drives it.

    Order matters, and so does the env. The sidecar's postinstall would
    otherwise run ``npx camoufox-js fetch``, which picks a browser from
    the GitHub releases API at install time — an unpinned ~650MB download
    (plus a 66MB GeoIP database), chosen by a regex over whatever is
    newest. Provisioning the pinned binary FIRST and exporting
    ``CAMOUFOX_EXECUTABLE`` makes the postinstall skip the fetch
    (``externalExecutableFromEnv``) and makes the server launch that same
    binary at runtime (``lib/config.js``). One variable covers both.

    ``CAMOUFOX_INSTALL_DIR`` does NOT do this: the postinstall checks its
    own ``camoufoxCacheDir()`` for a version.json rather than that
    variable, and the variable is not in ``FETCH_CHILD_ENV_VARS`` either,
    so it is dropped before ``camoufox-js fetch`` runs (measured: the
    fetch downloaded the full 663MB browser + 66MB GeoIP database into
    the cache dir with INSTALL_DIR pointed at an already-staged copy).
    """
    from installation import registry
    from installation.provisioner import provision_tool

    result = provision_tool("camoufox")
    if not result.ok:
        if interactive:
            print(f"  Could not provision the Camoufox browser: {result.detail}")
        return False

    browser = registry.tool_path("camoufox")
    if browser is None:  # pragma: no cover — provision_tool just recorded it
        return False

    sidecar = _camofox_sidecar_dir()
    if not (sidecar / "package-lock.json").is_file():
        if interactive:
            print(f"  No camofox sidecar manifest at {sidecar}")
        return False

    npm = registry.tool_path("npm")
    if npm is None:
        if interactive:
            print("  The pinned npm is not provisioned; run `hermes update`.")
        return False

    run_env = hermes_subprocess_env(inherit_credentials=False)
    run_env["CAMOUFOX_EXECUTABLE"] = str(browser)
    # `npm ci` installs exactly the lockfile — every dependency carries an
    # integrity hash — and fails rather than resolving anything itself.
    proc = subprocess.run(
        [str(npm), "ci", "--no-audit", "--no-fund"],
        cwd=str(sidecar),
        env=run_env,
    )
    if proc.returncode != 0:
        if interactive:
            print(f"  camofox sidecar install failed (npm ci exited {proc.returncode})")
        return False
    return True


def ensure_dependency(
    dep: str,
    interactive: bool = True,
) -> bool:
    """Ensure a non-Python dependency is available. Returns True if available."""
    check = _DEP_CHECKS.get(dep)
    if check is None:
        # Unknown dep — don't silently forward to install script.
        return False
    if check():
        return True

    desc = _DEP_DESCRIPTIONS.get(dep, dep)
    if interactive and sys.stdin.isatty():
        try:
            reply = input(f"{desc} is not installed. Install now? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if reply not in ("", "y", "yes"):
            return False

    if dep == "browser":
        try:
            if not _install_browser(interactive):
                return False
        except Exception as exc:
            if interactive:
                print(f"  Could not install the browser: {exc}")
            return False
        return check()

    # A pinned tool needs no shell: the provisioner IS the installer, and
    # it is the same engine the installers and `hermes update` run, so the
    # tool arrives digest-verified and recorded rather than at whatever
    # version a package manager happened to offer.
    pinned = _PINNED_DEPS.get(dep)
    if pinned is not None:
        try:
            from installation.provisioner import provision_tool

            result = provision_tool(pinned)
        except Exception as exc:
            if interactive:
                print(f"  Could not provision {pinned}: {exc}")
            return False
        if not result.ok:
            if interactive:
                print(f"  Could not provision {pinned}: {result.detail}")
            return False
        return check()

    script, shell = _find_install_script()
    if script is None:
        if interactive:
            print(f"  {desc} is not installed and no install script was found.")
            print(f"  Install {dep} manually and try again.")
        return False

    if shell == "powershell":
        from hermes_constants import get_hermes_home
        ps_bin = shutil.which("powershell") or shutil.which("pwsh")
        if not ps_bin:
            if interactive:
                print("  PowerShell not found. Install PowerShell or run install.ps1 manually.")
            return False
        cmd = [
            ps_bin,
            "-ExecutionPolicy", "Bypass",
            "-File", str(script),
            "-Ensure", dep,
            "-HermesHome", str(get_hermes_home()),
        ]
    else:
        cmd = ["bash", str(script), "--ensure", dep]

    run_env = hermes_subprocess_env(inherit_credentials=False)
    run_env["IS_INTERACTIVE"] = "false"
    result = subprocess.run(
        cmd,
        env=run_env,
    )
    if result.returncode != 0:
        return False

    return check()
