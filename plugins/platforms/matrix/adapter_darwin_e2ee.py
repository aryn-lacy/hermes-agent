"""Darwin E2EE self-heal for the Matrix adapter (fork-only).

Why (pyproject.toml): ``mautrix[encryption]`` pulls python-olm, which ships no
macOS wheels and bundles a libolm snapshot that fails to compile under
AppleClang 17+. On a Mac with Homebrew libolm installed, building the
extension against the brew dylib works — this module performs that build
from the pinned PyPI sdist and installs the missing anchor packages into
the running PM environment, so a fresh environment after ``hermes update``
recovers Matrix (including E2EE) without a manual fix.

Never runs on Linux; the upstream lazy-install path is untouched. The
pins below mirror pyproject.toml's ``[matrix]`` extra — keep them in sync.
"""

from __future__ import annotations

import hashlib
import logging
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from urllib import request as _urlrequest
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep in sync with pyproject.toml [project.optional-dependencies].matrix.
_PINNED_PACKAGES = {
    "mautrix": "mautrix[encryption]==0.21.1",
    "aiosqlite": "aiosqlite==0.22.1",
    "asyncpg": "asyncpg==0.31.0",
    "aiohttp_socks": "aiohttp-socks==0.11.0",
}
# Floor/aux pins: not import anchors, so they ride along on EVERY self-heal
# install (gating them on ``missing`` would never fire — they are never
# anchors). aiohttp restates pyproject's CVE floor: this lane resolves fresh
# outside PM's lock+quarantine, so the floor must be explicit. markdown
# carries the lock's pin. python-olm resolves from the sdist installed in
# the same self-heal, not from PyPI (no darwin wheels).
_FLOOR_PACKAGES = {
    "markdown": "markdown==3.10.2",
    "aiohttp": "aiohttp==3.14.3",
}

_SDIST_URL = "https://files.pythonhosted.org/packages/b8/eb/23ca73cbdc8c7466a774e515dfd917d9fbe747c1257059246fdc63093f04/python-olm-3.2.16.tar.gz"
_SDIST_SHA256 = "a1c47fce2505b7a16841e17694cbed4ed484519646ede96ee9e89545a49643c9"

# One self-heal at a time; uv pip installs into the live environment.
_SELFHEAL_LOCK = threading.Lock()

_HOMEBREW_CANDIDATES = ("/opt/homebrew", "/usr/local")


def sys_platform() -> str:
    return platform.system().lower()


def brew_libolm_prefix() -> Path | None:
    """The Homebrew prefix carrying libolm headers + dylib, or None."""
    for prefix in _HOMEBREW_CANDIDATES:
        root = Path(prefix)
        if (root / "include/olm/olm.h").is_file() and list((root / "lib").glob("libolm*.dylib")):
            return root
    return None


def _uv_binary() -> str | None:
    """The uv binary: PATH first, then the standard Homebrew locations
    (a launchd-started gateway may inherit a PATH without the brew prefix)."""
    found = shutil.which("uv")
    if found:
        return found
    for prefix in _HOMEBREW_CANDIDATES:
        candidate = Path(prefix) / "bin/uv"
        if candidate.is_file():
            return str(candidate)
    return None


def _target_python() -> Path | None:
    """The python of the environment this process runs from: PM's selected
    venv when discoverable, else the running interpreter's own prefix."""
    try:
        from pm.environments import selected_venv
        from pm.paths import repo_root

        python = selected_venv(repo_root()) / "bin" / "python"
        if python.is_file():
            return python
    except Exception:  # pragma: no cover — PM layout drift falls back to the running interpreter
        logger.debug("darwin E2EE self-heal: PM venv probe failed", exc_info=True)
    python = Path(sys.executable)
    return python if python.is_file() else None


def _patched_sdist(workdir: Path, brew: Path) -> Path:
    """Download the pinned sdist, verify its hash, and rewrite olm_build.py
    to compile against the Homebrew libolm instead of the bundled snapshot
    (whose list.hh trips AppleClang 17+'s const-qualified increment error)."""
    archive = workdir / "python-olm-3.2.16.tar.gz"
    with _urlrequest.urlopen(_SDIST_URL, timeout=120) as response, archive.open("wb") as sink:
        sink.write(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != _SDIST_SHA256:
        raise RuntimeError(f"python-olm sdist hash mismatch: {digest}")

    source_dir = workdir / "python-olm-3.2.16"
    with tarfile.open(archive) as tar:
        # filter="data" needs 3.11.4+; older 3.11 patch releases raise
        # TypeError on the kwarg itself — extract without it there (same
        # archive every time: the pinned, hash-verified sdist).
        try:
            tar.extractall(workdir, filter="data")
        except TypeError:
            tar.extractall(workdir)

    build_script = source_dir / "olm_build.py"
    script = build_script.read_text(encoding="utf-8-sig")
    build_marker = "# Try to build with cmake first"
    set_source_marker = "ffibuilder.set_source("
    # Order sanity on the pristine text: compile_args, then the bundled
    # cmake/make block, then set_source — whose argument list carries
    # library_dirs after it.
    positions = {
        "compile_args": script.find("compile_args = ["),
        "cmake_block": script.find(build_marker),
        "set_source": script.find(set_source_marker),
        "library_dirs": script.find('library_dirs=[os.path.join("libolm", "build")],'),
    }
    if not (0 <= positions["compile_args"] < positions["cmake_block"]
            < positions["set_source"] < positions["library_dirs"]):
        raise RuntimeError("python-olm olm_build.py layout changed; self-heal patch does not apply")
    # The bundled-libolm cmake/make block sits between compile_args and
    # set_source; library_dirs lives INSIDE the set_source(...) call, after
    # it — patch each site in place rather than splicing one region. Each
    # value site must match EXACTLY ONCE and is rewritten via bounded
    # replaces (no regex substitution, so brew paths can never be read as
    # backreferences), and splice offsets are computed AFTER the rewrites:
    # they lengthen their lines, so offsets taken earlier slice stale
    # positions (2026-10-08 review: mid-string-literal cut -> SyntaxError;
    # the self-heal could never succeed on any host).
    compile_hits = re.findall(r'compile_args = \["-I[^"]*"\]', script)
    dirs_hits = re.findall(r'library_dirs=\[os\.path\.join\("libolm", "build"\)\],', script)
    if len(compile_hits) != 1 or len(dirs_hits) != 1:
        raise RuntimeError("python-olm olm_build.py layout changed; self-heal patch does not apply")
    script = script.replace(compile_hits[0], 'compile_args = ["-I' + str(brew / "include") + '"]', 1)
    script = script.replace(dirs_hits[0], 'library_dirs=["' + str(brew / "lib") + '"],', 1)
    start = script.index(build_marker)
    end = script.index(set_source_marker)
    script = script[:start] + "# Bundled libolm build removed; linking against Homebrew libolm.\n\n" + script[end:]
    # Belt and suspenders: a corrupted splice must fail HERE, loudly, not at
    # build time three steps later.
    try:
        compile(script, "olm_build.py", "exec")
    except SyntaxError as exc:
        raise RuntimeError(f"self-heal patch produced an invalid olm_build.py: {exc}") from exc
    build_script.write_text(script, encoding="utf-8")
    return source_dir


def handle_missing_matrix_deps(missing, importer, target_globals) -> bool | None:
    """Dispatch for a missing ``[matrix]`` group (fork hook called from the
    adapter's ``ensure_matrix_deps``).

    Returns True when the self-heal restored the packages and the types were
    rebound via *importer*, False when the self-heal ran and failed (caller
    reports the failure and gives up), and None on non-darwin platforms — the
    caller then takes upstream's ``pm.extras.ensure_and_bind`` path.
    """
    if sys_platform() != "darwin":
        return None
    if not darwin_e2ee_selfheal(missing):
        logger.warning(
            "Matrix: required packages not installed and darwin self-heal "
            "could not restore them. Run `hermes pm install`, then restart Hermes."
        )
        return False
    # Same rebind semantics as ensure_and_bind: the adapter's module-level
    # import ran before mautrix existed, so its import-safe stubs must be
    # replaced with the real types for this process.
    try:
        target_globals.update(importer())
    except ImportError:
        logger.warning("Matrix: import after darwin self-heal failed:", exc_info=True)
        return False
    return True


def darwin_e2ee_selfheal(missing: tuple[str, ...]) -> bool:
    """Rebuild python-olm against Homebrew libolm and install the missing
    ``matrix`` anchor packages into the running environment. Best-effort:
    returns False (with a logged reason) when the host lacks the brew
    library or uv — the caller then falls through to the upstream failure
    path. Returns True when the packages now import."""
    if sys_platform() != "darwin":
        return False
    with _SELFHEAL_LOCK:
        return _selfheal_locked(missing)


def _selfheal_locked(missing: tuple[str, ...]) -> bool:
    brew = brew_libolm_prefix()
    if brew is None:
        logger.warning(
            "Matrix: darwin E2EE self-heal skipped — Homebrew libolm not found "
            "(tried %s). Install it with `brew install libolm` and restart Hermes.",
            ", ".join(_HOMEBREW_CANDIDATES))
        return False
    uv = _uv_binary()
    if uv is None:
        logger.warning("Matrix: darwin E2EE self-heal skipped — uv not found on PATH or in Homebrew.")
        return False
    python = _target_python()
    if python is None:
        logger.warning("Matrix: darwin E2EE self-heal skipped — no target python for the install.")
        return False

    specs = [_PINNED_PACKAGES[anchor] for anchor in missing if anchor in _PINNED_PACKAGES]
    specs += list(_FLOOR_PACKAGES.values())
    if not specs:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-olm-build-") as tmp:
            source_dir = _patched_sdist(Path(tmp), brew)
            # ONE resolve for the whole closure. This lane runs outside PM's
            # lock + exclude-newer quarantine, so every root must be pinned:
            # the transitive closure floats within mautrix's own declared
            # constraints, but aiohttp (the CVE-floor package) is rooted at
            # pyproject's pin so the resolver cannot move it. python-olm
            # enters the resolve as the patched sdist — its local metadata
            # satisfies mautrix[encryption]'s python-olm requirement, so uv
            # never reaches for PyPI, where darwin has no wheels and the
            # bundled libolm would hit the same AppleClang failure this
            # self-heal exists to route around.
            subprocess.run(
                [uv, "pip", "install", "--python", str(python),
                 str(source_dir), *specs],
                capture_output=True, text=True, timeout=600, check=True)
    except (OSError, subprocess.SubprocessError, RuntimeError, tarfile.TarError) as exc:
        logger.warning("Matrix: darwin E2EE self-heal failed: %s", exc)
        return False

    failed = [anchor for anchor in missing
              if not _anchor_imports(anchor)]
    if failed:
        logger.warning("Matrix: darwin E2EE self-heal installed packages but %s still do not import", failed)
        return False
    logger.info("Matrix: darwin E2EE self-heal restored %s", ", ".join(missing))
    return True


def _anchor_imports(anchor: str) -> bool:
    """Fresh-import check for a matrix anchor in a clean interpreter, so a
    stale sys.modules entry cannot mask a broken install."""
    probe = subprocess.run(
        [sys.executable, "-c", f"import {anchor}"],
        capture_output=True, timeout=60)
    return probe.returncode == 0
