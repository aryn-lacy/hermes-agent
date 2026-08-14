"""Provision managed runtime tools into <install>/.hermes-runtime/.

THE one dep engine: `hermes update` (post-update MACHINE_STEPS), the
installers (`python -m installation.provisioner`, after the pinned-uv
bootstrap), and the desktop payload staging all run this same code.

Per tool: read the EXACT pin for this target (url + sha256) → download →
verify the digest BEFORE extracting → stage into the tool's directory →
verify by RUNNING the binary → record the fact. A tool that cannot be
verified is not recorded: readers see it as unprovisioned and fall back
to system PATH, and the next run retries.

Tools are visited in the pin table's dependency order, so a tool that
declares ``extends`` is staged after what it extends — npm is unpacked by
running the node it extends. The same edge, read the other way, is the
PATH order recorded into the facts file for both language readers.

There is no salvage and no "reuse whatever is lying around". A tool is
either the exact pinned artifact, verified by digest, or it is absent.
Adopting an unverified tree from a previous install would defeat the
point of pinning digests at all.

Progress streams as installer stage-JSON lines when --json is on, so the
GUI install driver renders provisioning natively.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from installation.paths import get_install_root, get_runtime_dir
from installation.tree import Sealed, runtime_tree
from installation.registry import (
    PinnedFile,
    RuntimeFact,
    current_target,
    install_order,
    load_facts,
    load_pins,
    path_order,
    pinned_file,
    save_facts,
)

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "hermes-agent-provisioner"}


def _is_windows() -> bool:
    return sys.platform.startswith("win")


# ─── download + verify + extract ────────────────────────────────────────────


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=600
    ) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_verified(pin: PinnedFile, into: Path) -> Path:
    """Download a pinned artifact and prove it is the pinned bytes.

    The digest check happens BEFORE anything is unpacked or executed: a
    mismatched archive is deleted, never extracted. This is the only
    thing standing between a compromised CDN and a user's machine.
    """
    archive = into / pin.filename
    _download(pin.url, archive)

    actual = _sha256(archive)
    if actual != pin.sha256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(
            f"sha256 mismatch for {pin.filename}: "
            f"pinned {pin.sha256}, downloaded {actual}"
        )
    return archive


def _extract(archive: Path, dest: Path) -> None:
    """Extract tar.gz/tar.xz/zip into a freshly emptied *dest*."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith((".tar.gz", ".tgz", ".tar.xz")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
    elif name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                # extract() RETURNS the path it actually wrote, with the
                # entry name already sanitized (".." stripped, absolute
                # paths made relative). Chmod that, never info.filename:
                # an entry named "../../victim" chmods a file OUTSIDE the
                # destination, which is an arbitrary chmod +x for anyone
                # who can serve us an archive.
                written = Path(zf.extract(info, dest))
                mode = info.external_attr >> 16
                if mode & 0o111 and written.is_file():
                    written.chmod(mode & 0o777)
    else:
        raise ValueError(f"unsupported archive: {archive.name}")


# Directory names that are part of a tool's OWN layout. An archive whose
# single top-level entry is one of these is already unwrapped — hoisting
# it would destroy the layout (a lone `bin/` became `gh` and `gh/bin/gh`
# vanished).
_LAYOUT_DIRS = frozenset({"bin", "cmd", "lib", "libexec", "share", "etc", "usr"})


def _flatten_single_dir(dest: Path) -> None:
    """Hoist a lone VERSIONED wrapper dir's contents up one level.

    Most projects nest everything under one dir named for the release
    (``gh_2.97.0_linux_amd64/``, ``node-v26.7.0-linux-x64/``), which would
    otherwise leak the version into every facts path and break on the
    next bump. Some archives unpack flat instead — same tool, different
    platform, in uv's case — so this keys off what is actually there.
    """
    # EVERY entry counts, dotfiles included. Skipping them made a
    # top-level ".config" invisible to this check, so an archive shaped
    # {".config", "wrapper/.config"} looked like a lone wrapper and the
    # move silently replaced the outer file.
    entries = list(dest.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return

    inner = entries[0]
    if inner.name.lower() in _LAYOUT_DIRS:
        return

    # Never overwrite. After the checks above the destination holds only
    # `inner`, so the sole way to collide is a child named like its own
    # parent ("gh/gh"); shutil.move's own error for that case names a
    # temp path and reads like a bug in us. Refuse the whole flatten
    # instead: the unflattened tree is merely ugly, a clobbered file is
    # data loss.
    collisions = [c.name for c in inner.iterdir() if (dest / c.name).exists()]
    if collisions:
        raise RuntimeError(
            f"cannot unwrap {inner.name}/: would overwrite {', '.join(sorted(collisions))}"
        )

    for child in inner.iterdir():
        shutil.move(str(child), dest / child.name)
    inner.rmdir()


def _probe_version(
    binary: Path, args: list[str] | None = None, env: dict[str, str] | None = None
) -> Optional[str]:
    """Run `<binary> --version` and return the first version-shaped token.

    None when the binary does not run — callers treat that as
    unprovisioned, never as fatal.
    """
    try:
        out = subprocess.run(
            [str(binary)] + (args or ["--version"]),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    import re as _re

    m = _re.search(r"\d+(?:\.\d+)+", out or "")
    return m.group(0) if m else None


def _probe_env(entry: dict, rt: Path) -> Optional[dict[str, str]]:
    """Environment for the run-the-binary check.

    Most tools are self-contained executables and need nothing. A tool
    that extends another is a script launched by it — npm's shim is
    ``#!/usr/bin/env node`` — so the probe has to see the runtime dir's
    own tools on PATH, or it reports "does not run" on any host without a
    system copy and the tool is never recorded.
    """
    if not entry.get("extends"):
        return None
    from installation.env import with_managed_runtimes

    return with_managed_runtimes(runtime_dir=rt)


# ─── per-tool layout + staging ──────────────────────────────────────────────


@dataclass
class ToolResult:
    tool: str
    action: str  # kept | downloaded | failed
    version: Optional[str] = None
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.action != "failed"


def _binary_rel(tool: str, target: str) -> str:
    """Where each tool's binary lands, relative to the runtime dir."""
    win = target.startswith("win32")
    ext = ".exe" if win else ""
    return {
        # The Windows node zip has node.exe at the root; POSIX has bin/node.
        "node": "node/node.exe" if win else "node/bin/node",
        # `npm -g --prefix` drops .cmd shims in the prefix root on Windows
        # and POSIX shims in bin/ (same split dep_ensure documents).
        "npm": "npm/npm.cmd" if win else "npm/bin/npm",
        "uv": f"uv/uv{ext}",
        # PortableGit exposes cmd/git.exe; dugite-native uses bin/git.
        "git": "git/cmd/git.exe" if win else "git/bin/git",
        "gh": f"gh/bin/gh{ext}",
        "ripgrep": f"ripgrep/rg{ext}",
    }[tool]


def _path_dirs(tool: str, target: str) -> Optional[list[str]]:
    """PATH dirs for tools whose surface is more than the binary's dir.

    PortableGit needs three: bash.exe and the coreutils live outside
    cmd/. Everything else is covered by the binary's own directory.
    """
    if tool == "git" and target.startswith("win32"):
        return ["git/cmd", "git/bin", "git/usr/bin"]
    return None


def _stage_archive(pin: PinnedFile, dest: Path, tmp: Path) -> None:
    """The common case: fetch, verify, extract, un-nest.

    Flattening is decided by what the archive actually CONTAINS, not by a
    per-tool list: several projects nest under a versioned top-level dir
    on one platform and unpack flat on another (uv's POSIX tarball nests,
    its Windows zip does not — a hardcoded list got that wrong).
    ``_flatten_single_dir`` no-ops unless there is exactly one top-level
    directory, so applying it unconditionally is safe.
    """
    archive = _fetch_verified(pin, tmp)
    _extract(archive, dest)
    _flatten_single_dir(dest)


def _stage_portable_git(pin: PinnedFile, dest: Path, tmp: Path) -> None:
    """PortableGit is a self-extracting 7z, not an archive we can read.

    It is the one asset that must be EXECUTED to unpack, so the digest
    check matters more here than anywhere else — ``_fetch_verified`` has
    already proven the bytes before this runs it.
    """
    sfx = _fetch_verified(pin, tmp)
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    sfx.chmod(0o755)
    proc = subprocess.run(
        [str(sfx), f"-o{dest}", "-y"], capture_output=True, timeout=900
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PortableGit self-extractor exited {proc.returncode}")


def _stage_npm(pin: PinnedFile, dest: Path, tmp: Path, rt: Path, target: str) -> None:
    """npm installs itself, using the node it extends.

    npm is not a relocatable archive: its own ``bin/npm`` resolves the cli
    from ``dirname(process.execPath)``, so a plain unpack on PATH finds
    the npm BUNDLED inside node and fails outright. Letting npm do a
    global install into a prefix produces the launchers each platform
    actually needs (POSIX symlinks in ``bin/``, ``.cmd``/``.ps1`` shims in
    the prefix root) instead of us hand-writing shims per OS.

    The bytes are still the pinned, digest-verified tarball —
    ``--offline`` guarantees the registry is never consulted, so this
    installs exactly what the pin table says and nothing else.
    """
    tarball = _fetch_verified(pin, tmp)
    node = rt / _binary_rel("node", target)
    if not node.is_file():
        raise RuntimeError("npm extends node, which is not provisioned")

    # node's BUNDLED npm performs the install; the pinned npm replaces it
    # on PATH afterwards. Driving npm-cli.js through node directly avoids
    # depending on any npm shim already being resolvable.
    bundled_cli = (
        node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if target.startswith("win32")
        else node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    )
    if not bundled_cli.is_file():
        raise RuntimeError(f"node ships no bundled npm at {bundled_cli}")

    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            str(node),
            str(bundled_cli),
            "install",
            "--global",
            "--prefix",
            str(dest),
            "--offline",
            "--no-audit",
            "--no-fund",
            str(tarball),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        # Keep the install off the user's ~/.npm: an install-scoped tool
        # writes install-scoped state.
        env={**os.environ, "npm_config_cache": str(rt / "cache" / "npm")},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"npm install exited {proc.returncode}: {proc.stderr[-400:]}")


def _stage(
    tool: str, pin: PinnedFile, dest: Path, tmp: Path, target: str, rt: Path
) -> None:
    """Unpack one tool into its runtime-dir home.

    Branching lives here and nowhere else: every tool arrives through the
    same fetch-and-verify, and differs only in how its artifact unpacks.
    """
    if tool == "git" and target.startswith("win32"):
        _stage_portable_git(pin, dest, tmp)
        return

    if tool == "npm":
        _stage_npm(pin, dest, tmp, rt, target)
        return

    _stage_archive(pin, dest, tmp)
    if tool == "git" and not target.startswith("win32"):
        # delete useless DLLs
        dlls_dir = dest / "libexec" / "git-core"

        for dll_file in dlls_dir.rglob("*.dll"):
            print(f"deleted unused git dll: {dll_file}")
            dll_file.unlink()


# ─── the provisioning loop ──────────────────────────────────────────────────


def _discard_scratch(scratch: Path) -> None:
    """Delete a provisioning scratch dir, and shrug when the OS says no.

    A scratch file we cannot delete is not a provisioning failure: by
    the time this runs the tool is already unpacked into the runtime
    dir, and the OS reclaims its own temp dir later. On Windows the
    deleter races whatever still holds the artifact open — the
    PortableGit self-extractor outlives its own exit, and Defender
    cannot be disabled on the windows-11-arm image, so it scans the
    downloaded .exe and holds it too. Both surface as WinError 5, which
    used to abort the whole tool AFTER it had been staged.
    """
    # ignore_errors, not onerror/onexc: the callback spelling changed in
    # 3.12 and the deprecated one is removed in 3.14, and nothing here
    # needs the per-file exception — only whether anything survived.
    shutil.rmtree(scratch, ignore_errors=True)
    if scratch.exists():
        logger.debug("scratch dir %s could not be removed — leaving it", scratch)


def _provision_one(
    tool: str,
    entry: dict,
    rt: Path,
    facts: dict[str, RuntimeFact],
    target: str,
    path_order: list[str] | None = None,
) -> ToolResult:
    """Bring ONE tool to the pinned state. Never raises."""
    rel = _binary_rel(tool, target)

    # Already exactly right? The pin is exact, so this is an equality
    # check, not a range check.
    fact = facts.get(tool)
    if fact is not None and fact.version == entry["version"] and (rt / rel).is_file():
        return ToolResult(tool, "kept", version=fact.version)

    try:
        pin = pinned_file(tool, target, pins={tool: entry})
    except KeyError as exc:
        return ToolResult(tool, "failed", detail=str(exc))

    try:
        td = Path(tempfile.mkdtemp(prefix="hermes-provision-"))
        try:
            _stage(tool, pin, rt / tool, Path(td), target, rt)
        finally:
            _discard_scratch(td)

        binary = rt / rel
        if not binary.is_file():
            return ToolResult(tool, "failed", detail=f"{rel} missing after staging")
        binary.chmod(binary.stat().st_mode | 0o755)

        # Verify by RUNNING it, not by trusting the archive: a cross-arch
        # or half-extracted binary fails here rather than at first use.
        if _probe_version(binary, env=_probe_env(entry, rt)) is None:
            return ToolResult(tool, "failed", detail="provisioned binary does not run")

        facts[tool] = RuntimeFact(
            version=pin.version, path=rel, path_dirs=_path_dirs(tool, target)
        )
        save_facts(facts, rt, path_order=path_order)
        return ToolResult(tool, "downloaded", version=pin.version)
    except Exception as exc:  # noqa: BLE001 — per-tool isolation is the contract
        logger.warning("provisioning %s failed: %s", tool, exc)
        return ToolResult(tool, "failed", detail=str(exc))


def provision_tool(
    tool: str,
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
) -> ToolResult:
    """Provision a single pinned tool.

    Used by the self-heal paths that need exactly one runtime (the
    managed-Node bootstrap) without paying for a full sweep.
    """
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir()
    rt.mkdir(parents=True, exist_ok=True)
    pins = load_pins(install_root)
    entry = pins.get(tool)
    if entry is None:
        return ToolResult(tool, "failed", detail=f"{tool} is not pinned")
    return _provision_one(
        tool,
        entry,
        rt,
        load_facts(rt),
        current_target(),
        path_order=path_order(pins),
    )


def provision_runtimes(
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
    emit: Callable[[dict], None] | None = None,
    only: list[str] | None = None,
) -> list[ToolResult]:
    """Bring every pinned tool to its pinned version.

    Never raises for a single tool — each failure is recorded and the
    rest proceed (a broken ripgrep download must not kill node).

    Tools are provisioned in the pin table's dependency order, so a tool
    that extends another is staged after it — npm is unpacked by running
    the node it extends, which has to exist first.

    Provisioning is always for THIS host. A tool is never recorded until
    the staged binary has answered a version probe here, so a pin that
    downloads but cannot run is a failure rather than a fact.
    """
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir()
    rt.mkdir(parents=True, exist_ok=True)
    target = current_target()
    pins = load_pins(install_root)
    facts = load_facts(rt)
    results: list[ToolResult] = []
    order = path_order(pins)

    for tool in install_order(pins):
        if only and tool not in only:
            continue
        result = _provision_one(
            tool,
            pins[tool],
            rt,
            facts,
            target,
            path_order=order,
        )
        results.append(result)
        if emit:
            emit(
                {
                    "type": "runtime-tool",
                    "tool": result.tool,
                    "action": result.action,
                    "version": result.version,
                    "detail": result.detail,
                }
            )

    return results


def stale_tools(
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
) -> dict[str, tuple[str, Optional[str]]]:
    """Pinned tools whose installed state does not match the pin table.

    Maps tool → (pinned version, installed version or None). Empty means
    every pin is satisfied. This is the same equality check
    ``_provision_one`` makes before deciding to re-download — exact pins
    make it an equality check, not a range check.
    """
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir()
    target = current_target()
    facts = load_facts(rt)
    drift: dict[str, tuple[str, Optional[str]]] = {}

    for tool, entry in load_pins(install_root).items():
        fact = facts.get(tool)
        installed = fact.version if fact is not None else None
        if fact is not None and not (rt / _binary_rel(tool, target)).is_file():
            # Recorded but vanished reads as unprovisioned everywhere
            # else; say so here too rather than reporting it as current.
            installed = None
        if installed != entry["version"]:
            drift[tool] = (entry["version"], installed)
    return drift


class StaleManagedRuntimes(RuntimeError):
    """A sealed install's runtime tools disagree with its pin table."""


def require_current_runtimes(
    project_root: Path | None = None,
    runtime_dir: Path | None = None,
    install_root: Path | None = None,
) -> None:
    """Fail fast when a SEALED install ships out-of-date runtime tools.

    A git checkout provisions on demand: drift there is a normal state
    that the next `hermes update` (or the self-heal path) resolves, and
    raising would break the very run that fixes it.

    A sealed tree cannot self-heal. Its steward — Nix, Docker, the
    desktop bundle — builds the runtime tools as part of the artifact, so
    drift means the artifact was assembled against a different pin table
    than the code it ships. Every consequence of that is worse and more
    confusing than stopping here: tools silently missing from PATH,
    or a version the code does not expect. The steward has to rebuild.
    """
    root = project_root if project_root is not None else get_install_root()
    tree = runtime_tree(root)
    if not isinstance(tree, Sealed):
        return

    drift = stale_tools(runtime_dir=runtime_dir, install_root=install_root)
    if not drift:
        return

    lines = [
        f"  {tool}: pinned {pinned}, installed {installed or 'nothing'}"
        for tool, (pinned, installed) in sorted(drift.items())
    ]
    raise StaleManagedRuntimes(
        f"This Hermes is a sealed install managed by {tree.steward!r}, and its "
        "managed runtime tools do not match runtime-pins.json:\n"
        + "\n".join(lines)
        + "\n\nThe artifact was built against a different pin table than the code "
        "it ships. Rebuild it with its steward — a sealed tree cannot provision "
        "these itself."
    )


def step_provision_runtimes() -> dict:
    """post_update MACHINE_STEPS entry."""
    results = provision_runtimes()
    failed = [r for r in results if not r.ok]
    return {
        "ok": not failed,
        "tools": {r.tool: r.action for r in results},
        **(
            {"error": "; ".join(f"{r.tool}: {r.detail}" for r in failed)}
            if failed
            else {}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """``python -m installation.provisioner`` — provision into a dir.

    The desktop payload staging shells out to this rather than carrying a
    second implementation of download-and-verify in JavaScript.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m installation.provisioner")
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="Where to install (default: this install's .hermes-runtime).",
    )
    parser.add_argument(
        "--target",
        help="Assert this host IS this pin target, e.g. darwin-arm64. "
        "Provisioning is always for this host; the flag lets a caller "
        "state the target it believes it is on instead of inferring it. "
        "A mismatch exits 2.",
    )
    parser.add_argument(
        "--only", action="append", help="Provision just this tool (repeatable)."
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON lines.")
    ns = parser.parse_args(argv)

    # An asserted target that is not this host means the caller is wrong about
    # the machine it is on. Staging pins for another platform would write
    # binaries that cannot be probed here, so refuse rather than record facts
    # no one verified.
    host = current_target()
    if ns.target and ns.target != host:
        print(
            f"runtime_provisioner: --target {ns.target} is not this host ({host})",
            file=sys.stderr,
        )
        return 2

    def emit(event: dict) -> None:
        if ns.json:
            print(json.dumps(event), flush=True)
        else:
            version = f" {event['version']}" if event.get("version") else ""
            detail = f" — {event['detail']}" if event.get("detail") else ""
            print(f"  {event['tool']}: {event['action']}{version}{detail}", flush=True)

    results = provision_runtimes(
        runtime_dir=ns.runtime_dir, emit=emit, only=ns.only
    )
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
