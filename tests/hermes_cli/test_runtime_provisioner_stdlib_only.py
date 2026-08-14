"""The provisioner runs before there is a venv, so it must stay stdlib-only.

"Stdlib-only" is enforced at two levels because they fail differently:

- Nothing outside the standard library may be imported AT IMPORT TIME under
  a clean interpreter. This is the property that matters at runtime: the
  provisioner installs the tools the rest of Hermes needs, so it must import
  when no dependency is installed yet.
- The hermes modules it does import must themselves hold that property.
  ``runtime_registry`` (the pin/fact tables) and ``hermes_constants`` (where
  the runtime dir is) are pure stdlib apart from each other, so importing
  them keeps the guarantee. Anything NOT on that allowlist is a layering
  break: the provisioner is the bottom of the graph and must not grow a
  dependency on a module that could pull in a third-party package.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "hermes_cli" / "runtime_provisioner.py"

# The only first-party modules the provisioner may import, and why each one
# is safe: every module here is itself stdlib-only except for other entries
# on this list, so the whole closure imports with no site-packages.
ALLOWED_FIRST_PARTY = {
    "hermes_constants",  # where the install root and runtime dir are
    "hermes_cli",  # runtime_registry / runtime_env / runtime_tree
}

# Roots that are ours but NOT allowed: importing any of these would put an
# installable dependency underneath the thing that does the installing.
FORBIDDEN_FIRST_PARTY = {
    "agent",
    "tools",
    "gateway",
    "cron",
    "plugins",
    "providers",
    "toolsets",
    "utils",
}


def _imported_roots() -> list[tuple[str, int]]:
    """Every imported root package, including imports inside functions.

    ``ast.walk`` rather than a top-level scan: a lazy import inside a
    function is still an import the module performs, and lazy imports are
    exactly how this rule gets broken by accident.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import can only reach a hermes module.
                found.append((f"<relative:{node.module}>", node.lineno))
            elif node.module:
                found.append((node.module.split(".")[0], node.lineno))
    return found


def test_provisioner_imports_no_third_party() -> None:
    """No PyPI package, at module level or lazily inside a function."""
    bad = [
        (module, line)
        for module, line in _imported_roots()
        if module not in sys.stdlib_module_names
        and module not in ALLOWED_FIRST_PARTY
        and not module.startswith("<relative:")
    ]
    assert not bad, (
        "runtime_provisioner installs the tools the rest of Hermes needs, so "
        "it must import before anything is installed. A third-party import "
        f"here is a bootstrap deadlock. Offending (module, line): {bad}"
    )


def test_provisioner_imports_no_upper_layer() -> None:
    """It is the bottom of the graph: nothing above it may be imported."""
    bad = [
        (module, line)
        for module, line in _imported_roots()
        if module in FORBIDDEN_FIRST_PARTY or module.startswith("<relative:")
    ]
    assert not bad, (
        "runtime_provisioner is the bottom of the dependency graph — other "
        "modules import FROM it, never the reverse. "
        f"Offending (module, line): {bad}"
    )


def test_the_allowlisted_modules_are_themselves_stdlib_only() -> None:
    """The allowlist is only safe while its members stay clean.

    Without this, allowlisting ``hermes_cli`` would silently permit any
    dependency at all — one ``import requests`` in runtime_registry and the
    provisioner can no longer import on a fresh machine.
    """
    closure = [
        REPO_ROOT / "hermes_constants.py",
        REPO_ROOT / "hermes_cli" / "runtime_registry.py",
        REPO_ROOT / "hermes_cli" / "runtime_env.py",
        REPO_ROOT / "hermes_cli" / "runtime_tree.py",
    ]
    offenders: list[tuple[str, str, int]] = []
    for path in closure:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            roots: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                roots = [(a.name.split(".")[0], node.lineno) for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots = [(node.module.split(".")[0], node.lineno)]
            for module, line in roots:
                if (
                    module not in sys.stdlib_module_names
                    and module not in ALLOWED_FIRST_PARTY
                    and module != "__future__"
                ):
                    offenders.append((path.name, module, line))
    assert not offenders, (
        "A module the provisioner depends on grew a non-stdlib import, which "
        "breaks the provisioner's ability to run before the venv exists: "
        f"{offenders}"
    )


def test_provisioner_imports_under_a_bare_interpreter() -> None:
    """Import it the way a pre-venv bootstrap does.

    ``-I`` is isolated mode: no site-packages, no PYTHONPATH, no user site.
    That is an empty venv without the cost of building one. Only the repo
    root goes on the path, so ``hermes_cli`` itself still resolves.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import hermes_cli.runtime_provisioner",
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "runtime_provisioner failed to import under an isolated interpreter, "
        "so it cannot run before the venv exists:\n" + result.stderr
    )
