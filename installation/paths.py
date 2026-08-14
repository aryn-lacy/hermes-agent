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
    "get_install_root",
    "get_runtime_dir",
    "reset_install_root_override",
    "set_install_root_override",
]

RUNTIME_DIR_NAME = ".hermes-runtime"

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
