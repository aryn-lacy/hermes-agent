#!/usr/bin/env python3
"""What an OLD `hermes update` can still import from a NEW tree.

`hermes update` swaps the checkout under its own feet. The process keeps
running the code it started with, but the files underneath it are the
ones we just pulled. Anything it loads from disk after that point is a
contract with every released updater in the wild: delete one of those
names and the users on that release get a traceback halfway through an
update, on a tree that is already half-new.

`managed_uv._reload_hermes_constants` is the scar tissue proving this is
real: an updater hit ``cannot import name 'venv_python_path' from
'hermes_constants'`` while the file on disk plainly contained the name.

WHY THIS OVER-APPROXIMATES, ON PURPOSE
--------------------------------------
An earlier version of this script tried to find the exact swap statement
(the ``git merge --ff-only``) and count only what runs after it. That was
wrong twice over. It was fragile — ``ast.unparse`` normalises quotes, so
matching source text for ``"merge", "--ff-only"`` silently matched
nothing and the whole git path reported no swap at all. And it was wrong
in the DANGEROUS direction: every miss SHRINKS the frozen set, and a
symbol wrongly dropped from the set is a bricked update for whoever
reaches that branch.

So the rule is deliberately blunt: everything reachable from the update
entrypoints counts. A false positive costs one kept symbol. A false
negative costs somebody's install, mid-update, on a half-new tree.

A dynamic trace (driving real updates and watching imports) has the
opposite bias and is the wrong tool here for the same reason: one run
takes one path. It never enters the diverged-history reset, the Windows
rollback, the termux rung, or the ZIP fallback, so it reports a SMALLER
surface than reality.

THE DYNAMIC PATTERNS THAT MATTER (and why they are not missed)
--------------------------------------------------------------
Plain import analysis misses three things this flow really does, all of
which are resolved here because they are all spelled with literals:

* ``importlib.reload(m)`` — RE-EXECUTES the new file in the old process.
  This is the most dangerous load in the whole flow and it looks like
  nothing to an import walker. ``_UPDATE_RUNTIME_RELOAD_MODULES`` and
  ``_reload_config_modules`` reload ``hermes_constants``,
  ``hermes_cli.config`` and friends by name. Treated as a whole-module
  requirement.
* ``getattr(module, "name")`` — a symbol requirement with no import
  statement. ``managed_uv._windows_runtime_holders`` looks up
  ``_detect_venv_python_processes`` on ``hermes_cli.main`` this way, and
  silently refuses the update when it is absent.
* ``importlib.import_module(x)`` with a non-literal argument — cannot be
  resolved statically. Reported as UNRESOLVED rather than ignored.

Usage:
    python scripts/audit-old-updater-imports.py            # report
    python scripts/audit-old-updater-imports.py --json
    python scripts/audit-old-updater-imports.py --check    # CI gate
    python scripts/audit-old-updater-imports.py --explain hermes_constants
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The update flow has lived at both of these paths.
UPDATE_MODULE_CANDIDATES = (
    "hermes_cli/update_cmd.py",
    "hermes_cli/subcommands/update.py",
)

# Helpers the update flow calls into after the tree moves. Every function
# in these is treated as post-swap.
POST_SWAP_HELPER_MODULES = (
    "hermes_cli/post_update.py",
    "hermes_cli/managed_uv.py",
    "hermes_cli/update_lock.py",
)

# Only OUR packages matter: a third-party import is pinned by the
# dependency resolver, not by this repo's file layout.
FIRST_PARTY_ROOTS = frozenset(
    {
        "agent",
        "gateway",
        "hermes_cli",
        "hermes_constants",
        "hermes_state",
        "installation",
        "plugins",
        "tools",
    }
)

# Where an update begins. Everything reachable from here can run while
# the tree is being replaced.
UPDATE_ENTRYPOINTS = (
    "cmd_update",
    "_cmd_update_impl",
    "_update_via_zip",
    "_run_update_phase_inline",
    "cmd_update_eject",
)

_AnyFunc = ast.FunctionDef | ast.AsyncFunctionDef


def _first_party(module: str) -> bool:
    return module.split(".")[0] in FIRST_PARTY_ROOTS


@dataclass(frozen=True)
class Requirement:
    """One name the updater needs to find in the NEW tree."""

    module: str
    symbol: str | None
    kind: str  # import | reload | getattr
    function: str
    source_file: str

    def key(self) -> tuple[str, str]:
        return (self.module, self.symbol or "")


@dataclass
class Analysis:
    path: str
    requirements: list[Requirement] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    reachable: set[str] = field(default_factory=set)


def _function_table(tree: ast.AST) -> dict[str, _AnyFunc]:
    table: dict[str, _AnyFunc] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            table.setdefault(node.name, node)
    return table


def _called_names(node: ast.AST) -> set[str]:
    """Function names a piece of code can call.

    Covers the three shapes this codebase uses: ``foo()``,
    ``module.foo()``, and ``_m().foo()`` — update_cmd's lazy
    ``hermes_cli.main`` handle, which re-exports these same helpers.
    Attribute calls that are not ours simply find no match in the
    module's own function table.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _string_constants(tree: ast.AST) -> dict[str, list[str]]:
    """Module-level ``NAME = (...)`` / ``NAME = [...]`` string collections.

    ``_UPDATE_RUNTIME_RELOAD_MODULES`` is exactly this shape, and its
    contents are module names that get reloaded — i.e. re-executed from
    the new tree.
    """
    out: dict[str, list[str]] = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            continue
        values = [
            el.value
            for el in node.value.elts
            if isinstance(el, ast.Constant) and isinstance(el.value, str)
        ]
        if values:
            out[target.id] = values
    return out


def _requirements_in(
    func: _AnyFunc,
    source_file: str,
    constants: dict[str, list[str]],
) -> tuple[list[Requirement], list[str]]:
    """Every name *func* needs from the new tree, plus what we could not read."""
    reqs: list[Requirement] = []
    unresolved: list[str] = []

    def add(module: str, symbol: str | None, kind: str) -> None:
        if _first_party(module):
            reqs.append(Requirement(module, symbol, kind, func.name, source_file))

    for child in ast.walk(func):
        # ── plain lazy imports ─────────────────────────────────────────
        if isinstance(child, ast.ImportFrom):
            if not child.level and child.module:
                for alias in child.names:
                    add(child.module, alias.name, "import")
        elif isinstance(child, ast.Import):
            for alias in child.names:
                add(alias.name, None, "import")

        # ── dynamic loads ──────────────────────────────────────────────
        elif isinstance(child, ast.Call):
            fname = (
                child.func.attr
                if isinstance(child.func, ast.Attribute)
                else child.func.id
                if isinstance(child.func, ast.Name)
                else ""
            )

            if fname in ("reload", "import_module") and child.args:
                arg = child.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    # importlib.reload("x") is not legal, but
                    # import_module("x") is — same requirement either way.
                    add(arg.value, None, "reload")
                elif isinstance(arg, ast.Name) and arg.id in constants:
                    for module in constants[arg.id]:
                        add(module, None, "reload")
                else:
                    # A reload of a loop variable: find the collection the
                    # loop walks. `for m in (...)` / `for m in CONST`.
                    resolved = False
                    for loop in ast.walk(func):
                        if not isinstance(loop, ast.For):
                            continue
                        if not (
                            isinstance(loop.target, ast.Name)
                            and isinstance(arg, ast.Name)
                        ):
                            continue
                        names: list[str] = []
                        if isinstance(loop.iter, (ast.Tuple, ast.List)):
                            names = [
                                el.value
                                for el in loop.iter.elts
                                if isinstance(el, ast.Constant)
                                and isinstance(el.value, str)
                            ]
                        elif isinstance(loop.iter, ast.Name):
                            names = constants.get(loop.iter.id, [])
                        for module in names:
                            add(module, None, "reload")
                            resolved = True
                    if not resolved:
                        try:
                            text = ast.unparse(child)
                        except Exception:  # noqa: BLE001
                            text = f"{fname}(...)"
                        unresolved.append(f"{source_file}:{func.name}: {text[:90]}")

            elif fname == "getattr" and len(child.args) >= 2:
                holder, attr = child.args[0], child.args[1]
                if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                    module = _module_of(holder)
                    if module:
                        add(module, attr.value, "getattr")

    return reqs, unresolved


def _module_of(node: ast.AST) -> str | None:
    """Best-effort: which module a getattr target refers to.

    Handles the one real shape — ``sys.modules.get("hermes_cli.main")``
    stashed in a local and then getattr'd (managed_uv does exactly this).
    """
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if node.args and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    return value
    return None


def _resolve_sys_modules_locals(func: _AnyFunc) -> dict[str, str]:
    """Locals bound to ``sys.modules.get("<module>")`` inside *func*."""
    bound: dict[str, str] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                module = _module_of(node.value)
                if module:
                    bound[target.id] = module
    return bound


def _getattr_on_bound_locals(
    func: _AnyFunc, source_file: str
) -> list[Requirement]:
    """``m = sys.modules.get("x")`` … ``getattr(m, "y")`` → x.y required."""
    bound = _resolve_sys_modules_locals(func)
    if not bound:
        return []
    out: list[Requirement] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.id if isinstance(node.func, ast.Name) else ""
        if fname != "getattr" or len(node.args) < 2:
            continue
        holder, attr = node.args[0], node.args[1]
        if not (isinstance(holder, ast.Name) and holder.id in bound):
            continue
        if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
            module = bound[holder.id]
            if _first_party(module):
                out.append(
                    Requirement(module, attr.value, "getattr", func.name, source_file)
                )
    return out


def analyse(source: str, source_file: str, *, entrypoints: bool) -> Analysis | None:
    """Requirements of one version of one file.

    *entrypoints* selects the reachability seed: an update module starts
    from ``UPDATE_ENTRYPOINTS``; a post-swap helper module is entered
    wholesale, so every function in it counts.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    result = Analysis(path=source_file)
    functions = _function_table(tree)
    constants = _string_constants(tree)

    if entrypoints:
        seen: set[str] = set()
        stack = [name for name in UPDATE_ENTRYPOINTS if name in functions]
        if not stack:
            return result  # not an update module at this revision
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            for callee in _called_names(functions[name]):
                if callee in functions and callee not in seen:
                    stack.append(callee)
        reachable = seen
    else:
        reachable = set(functions)

    result.reachable = reachable

    for name in sorted(reachable):
        func = functions[name]
        reqs, unresolved = _requirements_in(func, source_file, constants)
        result.requirements.extend(reqs)
        result.requirements.extend(_getattr_on_bound_locals(func, source_file))
        result.unresolved.extend(unresolved)

    return result


# ─── history walking ────────────────────────────────────────────────────


def _all_audited_paths() -> tuple[str, ...]:
    return UPDATE_MODULE_CANDIDATES + POST_SWAP_HELPER_MODULES


def shipped_commits() -> list[str]:
    """Commits a user could actually be running, newest first.

    Restricted to ``origin/main``: the update channel is ``main`` or
    ``stable`` (``installation/tree.py``) and both track this branch.
    Unmerged topic branches are not something anyone updates from, and
    including them would freeze names that never shipped.

    Only commits that touched the audited files are listed; every other
    commit leaves them byte-identical to its parent.
    """
    proc = subprocess.run(
        ["git", "log", "origin/main", "--format=%H", "--", *_all_audited_paths()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.split() if line]


def _batch_read_blobs(refs: list[str]) -> dict[str, str]:
    """Read many ``<commit>:<path>`` revisions in ONE git process.

    A ``git show`` per pair is thousands of spawns; ``cat-file --batch``
    streams them through a single pipe. Bytes both ways, because payloads
    are located by the byte offset in each header and text decoding would
    shift every offset at the first multi-byte character.
    """
    if not refs:
        return {}

    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=REPO_ROOT,
        input=("\n".join(refs) + "\n").encode(),
        capture_output=True,
        check=True,
    )

    out = proc.stdout
    contents: dict[str, str] = {}
    pos = 0
    for ref in refs:
        newline = out.find(b"\n", pos)
        if newline == -1:
            break
        header = out[pos:newline].decode("utf-8", "replace")
        pos = newline + 1
        if header.endswith(" missing"):
            continue
        try:
            size = int(header.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
        contents[ref] = out[pos : pos + size].decode("utf-8", "replace")
        pos += size + 1
    return contents


@dataclass
class Surface:
    required: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    kinds: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    sites: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    unresolved: set[str] = field(default_factory=set)
    stats: dict = field(default_factory=dict)


def audit_history() -> Surface:
    commits = shipped_commits()
    paths = _all_audited_paths()
    refs = [f"{c}:{p}" for c in commits for p in paths]
    blobs = _batch_read_blobs(refs)

    surface = Surface()
    memo: dict[int, Analysis | None] = {}
    distinct = 0

    for ref, source in blobs.items():
        commit, _, path = ref.partition(":")
        fingerprint = hash(source)
        if fingerprint not in memo:
            memo[fingerprint] = analyse(
                source, path, entrypoints=path in UPDATE_MODULE_CANDIDATES
            )
            distinct += 1
        analysis = memo[fingerprint]
        if analysis is None:
            continue
        for req in analysis.requirements:
            surface.required.setdefault(req.key(), set()).add(commit[:12])
            surface.kinds.setdefault(req.key(), set()).add(req.kind)
            surface.sites.setdefault(req.key(), set()).add(
                f"{req.source_file}:{req.function}"
            )
        surface.unresolved.update(analysis.unresolved)

    surface.stats = {
        "commits": len(commits),
        "revisions_read": len(blobs),
        "distinct_file_versions": distinct,
    }
    return surface


# ─── resolution against the working tree ────────────────────────────────


def resolve_in_tree(module: str, symbol: str | None, root: Path) -> tuple[bool, str]:
    """Does *module* (and *symbol*) exist in the tree at *root*?

    Static resolution against the FILES, deliberately: importing would
    RUN the module, and the question is what an updater finds on disk,
    not what this interpreter can execute.
    """
    rel = Path(module.replace(".", "/"))
    for candidate in (root / f"{rel}.py", root / rel / "__init__.py"):
        if candidate.is_file():
            path = candidate
            break
    else:
        return False, f"module {module} not found"

    if symbol is None:
        return True, ""

    # `from hermes_cli import gateway_windows` names a SUBMODULE, not an
    # attribute of the package body.
    submodule = root / rel / symbol
    if submodule.with_suffix(".py").is_file() or (submodule / "__init__.py").is_file():
        return True, ""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return False, f"{module}: unreadable ({exc})"

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True, ""
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    return True, ""
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == symbol:
                return True, ""
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # A re-export counts.
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == symbol:
                    return True, ""

    return False, f"{module}.{symbol} not found"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero when this tree is missing something an old "
        "updater needs.",
    )
    parser.add_argument(
        "--explain", metavar="MODULE", help="Show every requirement on MODULE."
    )
    ns = parser.parse_args(argv)

    surface = audit_history()

    missing = []
    for (module, symbol), commits in sorted(surface.required.items()):
        ok, why = resolve_in_tree(module, symbol or None, REPO_ROOT)
        if not ok:
            missing.append((module, symbol, sorted(commits), why))

    if ns.explain:
        print(f"Requirements on {ns.explain!r}:")
        for (module, symbol), commits in sorted(surface.required.items()):
            if module != ns.explain:
                continue
            kinds = "/".join(sorted(surface.kinds[(module, symbol)]))
            where = ", ".join(sorted(surface.sites[(module, symbol)])[:3])
            print(
                f"  {module}.{symbol or '<module>'}  [{kinds}]"
                f"  {len(commits)} commits  {where}"
            )
        return 0

    if ns.json:
        print(
            json.dumps(
                {
                    "stats": surface.stats,
                    "required": [
                        {
                            "module": m,
                            "symbol": s or None,
                            "kinds": sorted(surface.kinds[(m, s)]),
                            "commits": sorted(c),
                            "sites": sorted(surface.sites[(m, s)]),
                        }
                        for (m, s), c in sorted(surface.required.items())
                    ],
                    "unresolved_dynamic": sorted(surface.unresolved),
                    "missing": [
                        {"module": m, "symbol": s or None, "commits": c, "why": w}
                        for m, s, c, w in missing
                    ],
                },
                indent=2,
            )
        )
        return 1 if (missing and ns.check) else 0

    st = surface.stats
    print(
        f"Walked every shipped commit that touched the update flow: "
        f"{st['commits']} commits, {st['revisions_read']} file revisions, "
        f"{st['distinct_file_versions']} distinct versions."
    )
    print()
    by_kind: dict[str, int] = {}
    for kinds in surface.kinds.values():
        for kind in kinds:
            by_kind[kind] = by_kind.get(kind, 0) + 1
    kinds_summary = ", ".join(f"{v} {k}" for k, v in sorted(by_kind.items()))
    print(
        f"FROZEN COMPAT SURFACE — {len(surface.required)} module/symbol pairs "
        f"an old updater can load from the NEW tree ({kinds_summary}):"
    )
    by_module: dict[str, list[str]] = {}
    for module, symbol in surface.required:
        by_module.setdefault(module, []).append(symbol or "<module>")
    for module in sorted(by_module):
        print(f"  {module}: {', '.join(sorted(by_module[module]))}")

    if surface.unresolved:
        print()
        print(
            f"⚠ {len(surface.unresolved)} dynamic load(s) this script cannot "
            f"resolve — audit by hand before deleting anything they may reach:"
        )
        for item in sorted(surface.unresolved):
            print(f"    {item}")

    print()
    if missing:
        print(f"✗ {len(missing)} name(s) an old updater needs are GONE:")
        for module, symbol, commits, why in missing:
            kinds = "/".join(sorted(surface.kinds[(module, symbol or "")]))
            where = ", ".join(sorted(surface.sites[(module, symbol or "")])[:2])
            shown = ", ".join(commits[:3])
            more = f" +{len(commits) - 3}" if len(commits) > 3 else ""
            print(f"    [{kinds}] {why}\n        from {where}  [{shown}{more}]")
    else:
        print("✓ every name an old updater needs still exists in this tree")

    return 1 if (missing and ns.check) else 0


if __name__ == "__main__":
    raise SystemExit(main())
