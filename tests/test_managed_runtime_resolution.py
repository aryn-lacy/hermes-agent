"""Guard: Hermes-owned subprocesses must not resolve managed runtimes by bare PATH.

Hermes installs runtimes for itself — ``uv`` at ``$HERMES_HOME/bin/uv``, Node at
``$HERMES_HOME/node``. Neither directory is on the ambient PATH of an arbitrary
process, so ``shutil.which("uv")`` / ``shutil.which("node")`` in Hermes's own
code has two failure modes:

* the managed runtime is invisible, so the caller reports "not installed" or
  degrades to a slower tier on a machine that has exactly what it needed; and
* when a system copy also exists, the one Hermes does not own wins — which is
  how a generated systemd unit or launchd plist can bake a system Node in and
  keep resolving it across reboots.

The fix per call site is one of ``installation.nodejs.node_path()``,
``installation.env.managed_path_dirs()``, ``resolve_uv()``, or ``ensure_uv()``.
This test is
the ratchet that stops a new bare lookup from being added back.

Reading source is normally banned (see AGENTS.md). It is the right tool here and
only here: the property under test is "no call site anywhere in the tree spells
it this way", which is a statement about the whole codebase rather than about
one function's behavior, and there is no runtime seam that can observe a lookup
that was never written. Every entry in the allow-list below names a call site
whose behavior is separately covered by a real test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Runtimes Hermes provisions into HERMES_HOME and must therefore resolve
# through a managed-aware helper rather than PATH.
_MANAGED_COMMANDS = frozenset({"uv", "node", "npm", "npx"})

# Directories that are not Hermes-owned subprocess code: plugins ship their own
# resolution policy, tests assert against PATH deliberately, and skills/scripts
# run as standalone user-invoked programs.
_EXEMPT_DIRS = (
    "tests",
    "plugins",
    "skills",
    "optional-skills",
    "scripts",
    "website",
    "node_modules",
    ".git",
    ".venv",
    "venv",
    ".worktrees",
    # Staged desktop payloads are a COPY of the repo, so every source
    # file appears twice and the lint reports the copy's line numbers.
    # Gitignored build output, not source to review.
    "build",
)

# Call sites where a bare PATH lookup is the correct answer. Each entry is
# (path, command) -> why. Keep this list short and justified — the default
# answer for a new call site is a managed-aware helper, not a new exemption.
_ALLOWED: dict[tuple[str, str], str] = {
    ("tools/env_probe.py", "uv"): (
        "Reports the environment the MODEL sees in the terminal tool. The model "
        "can only run what is on that subshell's PATH, which local.py populates "
        "with the managed dirs — so PATH is the correct question to ask here."
    ),
    ("hermes_cli/update_cmd.py", "uv"): (
        "Termux fallback: a pkg-installed uv lands on PATH but not in the "
        "managed bin dir, and it is checked only after resolve_uv() misses."
    ),
    ("hermes_cli/gateway.py", "node"): (
        "Fallback rung of _append_node_dir_for_service(), after the managed "
        "dirs from managed_path_dirs() are already appended."
    ),
    ("hermes_cli/main.py", "node"): (
        "_ensure_tui_node()'s idempotence gate: the question really is 'is "
        "node already discoverable on PATH', before bootstrapping one."
    ),
    ("hermes_cli/main.py", "npm"): (
        "Same _ensure_tui_node() gate as node."
    ),
}


def _iter_which_calls(tree: ast.AST):
    """Yield (command, lineno) for every ``which("<cmd>")`` call in *tree*.

    AST rather than a regex so prose in docstrings and comments that mentions
    ``shutil.which("npm")`` is not mistaken for a call site.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None
        )
        if name != "which":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value in _MANAGED_COMMANDS:
            yield first.value, node.lineno


def _source_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        # Match ANY component, not just the first: staged payloads live at
        # apps/desktop/build/, and node_modules nests arbitrarily deep.
        if any(part in _EXEMPT_DIRS for part in rel.parts):
            continue
        files.append(path)
    return files


def _findings() -> list[tuple[str, str, int]]:
    """Return (relpath, command, lineno) for every bare managed lookup."""
    found: list[tuple[str, str, int]] = []
    for path in _source_files():
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "which(" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for command, lineno in _iter_which_calls(tree):
            found.append((rel, command, lineno))
    return found


def test_no_unreviewed_bare_managed_runtime_lookups():
    """Every bare which() for a managed runtime is a reviewed exemption."""
    unexpected = [
        (rel, cmd, lineno)
        for rel, cmd, lineno in _findings()
        if (rel, cmd) not in _ALLOWED
    ]

    assert not unexpected, (
        "Bare PATH lookup for a Hermes-managed runtime.\n\n"
        + "\n".join(f"  {rel}:{lineno}  which({cmd!r})" for rel, cmd, lineno in unexpected)
        + "\n\n$HERMES_HOME/bin (uv) and $HERMES_HOME/node are not on an "
        "arbitrary process's PATH, so this resolves a system copy — or nothing "
        "— on an install that has a managed one.\n"
        "Use instead:\n"
        "  uv       -> managed_uv.resolve_uv() (lookup) or ensure_uv() (may install)\n"
        "  node/npm -> installation.nodejs.node_path() / npm_path()\n"
        "  PATH env -> installation.env.managed_path_dirs()\n"
        "If PATH really is the right question, add the site to _ALLOWED with a "
        "reason."
    )


def test_allowlist_has_no_stale_entries():
    """A fixed call site must be dropped from the allow-list, not left to rot."""
    live = {(rel, cmd) for rel, cmd, _lineno in _findings()}
    stale = sorted(set(_ALLOWED) - live)

    assert not stale, (
        "Allow-list entries no longer match any source line — the call site was "
        "fixed or moved. Remove them:\n"
        + "\n".join(f"  {rel} ({cmd})" for rel, cmd in stale)
    )


@pytest.mark.parametrize(
    ("module", "helper"),
    [
        ("installation.nodejs", "node_path"),
        ("installation.nodejs", "npm_path"),
        ("installation.nodejs", "npx_path"),
        ("installation.nodejs", "run_npm"),
        ("installation.nodejs", "npm_install"),
        ("installation.env", "managed_path_dirs"),
        ("installation.env", "with_managed_runtimes"),
    ],
)
def test_managed_node_helpers_exist(module, helper):
    """The alternatives this guard points contributors at must be importable."""
    import importlib

    assert callable(getattr(importlib.import_module(module), helper))


def test_managed_uv_helpers_exist():
    from hermes_cli.managed_uv import ensure_uv, managed_uv_path, resolve_uv

    assert callable(resolve_uv)
    assert callable(ensure_uv)
    # Install-scoped layout (hermes-home lifetime split): <runtime dir>/uv/.
    # Asserted against get_runtime_dir() rather than the literal
    # ".hermes-runtime": a packager can point HERMES_RUNTIME_DIR at a built
    # tree (Nix does), and the invariant is that uv lives inside whatever
    # runtime dir this install resolves — not what that dir is called.
    from installation.paths import get_runtime_dir

    assert managed_uv_path().parent.name == "uv"
    assert managed_uv_path().parent.parent == get_runtime_dir()
