"""Collect every git invocation in the repo and the flags it uses.

The SYSTEM_GIT_FLOOR in installation/provisioner.py is derived from this
inventory: the newest-introduced flag sets the floor. Re-run after adding
git calls; if a new flag postdates the floor, raise the floor.

AST-walks call sites whose argv starts with a git reference (a "git"
literal, git_cmd + [...], GIT_CMD variables) and prints the flag set.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

REPO = Path(".")
PACKAGES = ("hermes_cli", "installation", "gateway", "tools", "agent", "scripts")

flags: dict[str, set[str]] = defaultdict(set)
subcommands: dict[str, set[str]] = defaultdict(set)


def strings_in(node: ast.AST) -> list[str]:
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(child.value)
    return out


def looks_like_git(argv_node: ast.AST) -> bool:
    text = ast.dump(argv_node)
    if "'git'" in text or '"git"' in text:
        return True
    # git_cmd + [...] / GIT_CMD variables
    for child in ast.walk(argv_node):
        if isinstance(child, ast.Name) and "git" in child.id.lower():
            return True
    return False


for package in PACKAGES:
    root = REPO / package
    if not root.exists():
        continue
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name not in ("run", "check_output", "check_call", "Popen", "call"):
                continue
            if not node.args:
                continue
            argv = node.args[0]
            if not looks_like_git(argv):
                continue
            strs = strings_in(argv)
            if not strs:
                continue
            # find the subcommand: first non-flag, non-git, non-option-arg
            sub = ""
            for token in strs:
                if token in ("git", "-C", "-c") or token.startswith("-"):
                    continue
                if "/" in token or "\\" in token or "=" in token or " " in token:
                    continue
                sub = token
                break
            where = f"{path}:{node.lineno}"
            if sub:
                subcommands[sub].add(where)
            for token in strs:
                if token.startswith("--") or (
                    token.startswith("-") and len(token) == 2
                ):
                    flags[f"{sub} {token}" if sub else token].add(where)
                if "=" in token and token.split("=")[0] in (
                    "windows.appendAtomically",
                    "core.autocrlf",
                    "safe.directory",
                    "credential.helper",
                ):
                    flags[f"-c {token.split('=')[0]}"].add(where)

print("== subcommands used ==")
for sub in sorted(subcommands):
    print(f"  {sub}  ({len(subcommands[sub])} sites)")
print()
print("== flags by subcommand ==")
for key in sorted(flags):
    sites = sorted(flags[key])
    print(f"  {key}  ({len(sites)}): {sites[0]}")
