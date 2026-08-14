"""Where THIS install lives, and where its native tools go.

The bottom of the installation layer: everything else here reads these two
paths, and nothing in this module reads anything of ours. That is what lets
the provisioner import before a venv exists.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from pathlib import Path

__all__ = [
    "RUNTIME_DIR_NAME",
    "TOOL_STORE_DIR_NAME",
    "get_install_root",
    "get_runtime_dir",
    "get_tool_store",
    "reset_install_root_override",
    "resolve_bases",
    "set_install_root_override",
]

RUNTIME_DIR_NAME = ".hermes-runtime"
TOOL_STORE_DIR_NAME = "tools"

# A module-private sentinel: ``None`` is a meaningful override value here
# (it means "restore the default derivation"), so absence needs its own.
_UNSET = object()

_INSTALL_ROOT_OVERRIDE: ContextVar[str | object] = ContextVar(
    "_INSTALL_ROOT_OVERRIDE", default=_UNSET
)


def set_install_root_override(path: str | Path | None) -> Token:
    """Override the install root for this context (desktop resourcesPath,
    tests). Pass ``None`` to explicitly restore the default derivation."""
    value: str | object = _UNSET if path is None else str(path)
    return _INSTALL_ROOT_OVERRIDE.set(value)


def reset_install_root_override(token: Token) -> None:
    _INSTALL_ROOT_OVERRIDE.reset(token)


def get_install_root() -> Path:
    """Return the root directory of THIS install of Hermes.

    Resolution order:
      1. ``HERMES_INSTALL_ROOT`` env var — set by the desktop app
         (resources payload) and by tests. An env var rather than only a
         ContextVar because child processes (post-update phase, tool
         subprocesses) must inherit it across the process boundary.
      2. Context override (``set_install_root_override``) — in-process
         callers that cannot mutate the environment.
      3. The directory ABOVE this package — for a source checkout that is
         the repo root, since ``installation/`` sits at top level.

    pip/wheel layouts are unsupported by design (setup.py blocks wheel
    builds outside Nix), so rung 3 is always a real, writable checkout —
    or the caller set rung 1/2.
    """
    env_root = os.environ.get("HERMES_INSTALL_ROOT", "")
    if env_root:
        return Path(env_root)
    override = _INSTALL_ROOT_OVERRIDE.get()
    if override is not _UNSET:
        return Path(str(override))
    return Path(__file__).resolve().parent.parent


def get_runtime_dir(install_root: Path | None = None) -> Path:
    """Return the install-scoped runtime directory ``<root>/.hermes-runtime``.

    Holds managed binaries (node, npm, uv, git, gh, ripgrep), install-keyed
    caches, and the ``runtimes.json`` facts manifest. Callers must treat
    the location as opaque and go through the runtime registry for tool
    lookup — no path literals.

    ``HERMES_RUNTIME_DIR`` overrides it for packagers that BUILD the
    runtime dir instead of provisioning it: the Nix package assembles one
    from the pin table at build time and points here, because its install
    root is an immutable store path that no provisioner can write to. An
    explicit *install_root* still wins — a caller naming a root means
    that root.
    """
    if install_root is None:
        override = os.environ.get("HERMES_RUNTIME_DIR", "").strip()
        if override:
            return Path(override)
    root = install_root if install_root is not None else get_install_root()
    return root / RUNTIME_DIR_NAME


def get_tool_store() -> Path:
    """Return the machine-wide store that holds managed tool BYTES.

    ``~/.hermes/tools/<tool>-<version>-<target>/`` — one entry per
    (tool, version, target) tuple, which is exactly the tuple the pin
    table keys on. Two installs that agree on a pin therefore share the
    entry by construction, and two that disagree get one entry each.

    Bytes and FACTS live apart on purpose. The facts file stays
    install-scoped in ``get_runtime_dir()`` because which tools an
    install uses is its own business; the bytes are identical wherever
    they came from, so copying them per install only costs disk. A
    checkout-nested copy cost ~495MB per worktree, and worktrees are the
    normal unit of work here.

    There are no symlinks between the two: ``runtimes.json`` names a
    store-relative path, so the facts file IS the indirection layer.

    ``HERMES_RUNTIME_DIR`` wins, and points bytes and facts back at ONE
    self-contained directory. That is what a packager builds: the Nix
    bundle and the desktop payload assemble a runtime dir at build time
    and cannot use a store they do not own.
    """
    override = os.environ.get("HERMES_RUNTIME_DIR", "").strip()
    if override:
        return Path(override)
    # Local import: hermes_constants imports THIS module, so an
    # import-time dependency would be circular. By the time anyone calls
    # this, both modules are loaded. (installation/tree.py does the same
    # for the same reason.)
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / TOOL_STORE_DIR_NAME


def resolve_bases(
    runtime_dir: Path | None = None, store_dir: Path | None = None
) -> tuple[Path, Path]:
    """Resolve the (facts dir, bytes dir) pair every reader needs.

    ONE rule, shared by the registry, the environment assembler and the
    provisioner, so no two of them can disagree about where a tool is:

    * an explicit *store_dir* always wins;
    * a caller that names a *runtime_dir* and no store means THAT
      directory for both — a self-contained runtime dir, which is what
      the Nix bundle, the desktop payload and the tests all pass;
    * with neither, facts come from this install's runtime dir and bytes
      from the shared store.
    """
    rt = runtime_dir if runtime_dir is not None else get_runtime_dir()
    if store_dir is not None:
        return rt, store_dir
    if runtime_dir is not None:
        return rt, rt
    return rt, get_tool_store()
