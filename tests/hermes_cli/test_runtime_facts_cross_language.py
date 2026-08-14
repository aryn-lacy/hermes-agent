"""The runtimes.json contract holds across Python and TypeScript.

Two languages read the same facts file: installation/env.py builds
the PATH for Python-spawned subprocesses, apps/desktop/electron/backend-env.ts
does it for the Electron backend. AGENTS.md's rule for cross-language
manifest writers applies — write it with one, read it with the other.

The TS side is exercised through node with a small driver, so this is a
real round-trip and not a restatement of the Python behaviour.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from installation import env as runtime_env
from installation import registry as rr

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ENV_TS = REPO_ROOT / "apps" / "desktop" / "electron" / "backend-env.ts"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the cross-language contract test")
    return node


def _provision(runtime_dir: Path, name: str, rel: str, version="1.0.0", path_dirs=None,
               path_order=None):
    binary = runtime_dir / rel
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    facts = rr.load_facts(runtime_dir)
    facts[name] = rr.RuntimeFact(version=version, path=rel, path_dirs=path_dirs)
    rr.save_facts(facts, runtime_dir, path_order=path_order)


def _ts_path_entries(
    tmp_path: Path, runtime_dir: Path, store_dir: Path | None = None
) -> list[str]:
    """Run the TypeScript reader over a Python-written facts file."""
    # Strip the TS types with a throwaway transpile via node's own stripping
    # (node >= 22.6 --experimental-strip-types); fall back to a skip when the
    # runtime is too old, rather than silently testing nothing.
    driver = tmp_path / "driver.mts"
    options = (
        "{}" if store_dir is None else f"{{ storeDir: {json.dumps(str(store_dir))} }}"
    )
    driver.write_text(
        textwrap.dedent(
            f"""
            import {{ managedRuntimePathEntries }} from {json.dumps(str(BACKEND_ENV_TS))}
            const dirs = managedRuntimePathEntries({json.dumps(str(runtime_dir))}, {options})
            process.stdout.write(JSON.stringify(dirs))
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [_node(), "--experimental-strip-types", "--no-warnings", str(driver)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    if proc.returncode != 0:
        pytest.skip(f"node could not run the TS driver: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


class TestCrossLanguageFactsContract:
    def test_both_languages_agree_on_a_single_tool(self, tmp_path):
        runtime_dir = tmp_path / ".hermes-runtime"
        _provision(runtime_dir, "node", "node/bin/node", version="26.5.1")

        python_dirs = [str(d) for d in runtime_env.managed_path_dirs(runtime_dir)]
        assert python_dirs == _ts_path_entries(tmp_path, runtime_dir)

    def test_both_languages_agree_on_assembly_ORDER(self, tmp_path):
        """Order is a contract, not an accident: it decides which copy of a
        tool wins when several are on PATH."""
        runtime_dir = tmp_path / ".hermes-runtime"
        order = ["node", "uv", "ripgrep"]
        # Written in reverse of the expected order on purpose.
        _provision(runtime_dir, "ripgrep", "ripgrep/rg", path_order=order)
        _provision(runtime_dir, "uv", "uv/uv", path_order=order)
        _provision(runtime_dir, "node", "node/bin/node", path_order=order)

        python_dirs = [str(d) for d in runtime_env.managed_path_dirs(runtime_dir)]
        ts_dirs = _ts_path_entries(tmp_path, runtime_dir)

        assert python_dirs == ts_dirs
        assert [Path(d).name for d in python_dirs] == ["bin", "uv", "ripgrep"]

    def test_both_languages_follow_a_recorded_extender_first_order(self, tmp_path):
        """The pin table's `extends` edge reaches both readers as DATA.

        npm extends node, so npm's bin dir must come first or node's
        bundled npm shadows the pinned one. Neither language may reach
        that conclusion from a list of its own — this asserts they both
        take it from the facts file the provisioner wrote.
        """
        runtime_dir = tmp_path / ".hermes-runtime"
        pins = {
            "node": {"version": "26.7.0", "files": {}},
            "npm": {"version": "12.0.2", "extends": ["node"], "files": {}},
        }
        order = rr.path_order(pins)
        _provision(runtime_dir, "node", "node/bin/node", path_order=order)
        _provision(runtime_dir, "npm", "npm/bin/npm", path_order=order)

        python_dirs = [str(d) for d in runtime_env.managed_path_dirs(runtime_dir)]

        assert python_dirs == _ts_path_entries(tmp_path, runtime_dir)
        assert [Path(d).parent.name for d in python_dirs] == ["npm", "node"]

    def test_both_languages_spread_pathDirs(self, tmp_path):
        runtime_dir = tmp_path / ".hermes-runtime"
        for sub in ("git/cmd", "git/bin", "git/usr/bin"):
            (runtime_dir / sub).mkdir(parents=True)
        _provision(
            runtime_dir,
            "git",
            "git/cmd/git.exe",
            version="2.55.0",
            path_dirs=["git/cmd", "git/bin", "git/usr/bin"],
        )

        python_dirs = [str(d) for d in runtime_env.managed_path_dirs(runtime_dir)]
        assert len(python_dirs) == 3
        assert python_dirs == _ts_path_entries(tmp_path, runtime_dir)

    def test_both_languages_ignore_a_vanished_binary(self, tmp_path):
        runtime_dir = tmp_path / ".hermes-runtime"
        _provision(runtime_dir, "node", "node/bin/node")
        (runtime_dir / "node" / "bin" / "node").unlink()

        assert runtime_env.managed_path_dirs(runtime_dir) == []
        assert _ts_path_entries(tmp_path, runtime_dir) == []

    def test_both_languages_treat_a_foreign_schema_as_unprovisioned(self, tmp_path):
        runtime_dir = tmp_path / ".hermes-runtime"
        runtime_dir.mkdir(parents=True)
        (runtime_dir / rr.FACTS_FILENAME).write_text(
            json.dumps(
                {"schemaVersion": 999, "tools": {"node": {"version": "1", "path": "n"}}}
            )
        )

        assert runtime_env.managed_path_dirs(runtime_dir) == []
        assert _ts_path_entries(tmp_path, runtime_dir) == []


class TestTheStoreSplitCrossesTheLanguageBoundary:
    """Facts live with the install, bytes live in a shared store. Both
    readers have to join a fact's path onto the SAME base, or the desktop
    builds a PATH of directories that do not exist while Python builds a
    working one (or the reverse)."""

    def _provision_into_store(self, facts_dir: Path, store: Path, entry: str, rel: str):
        binary = store / entry / rel
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        facts = rr.load_facts(facts_dir)
        facts["node"] = rr.RuntimeFact(version="26.7.0", path=f"{entry}/{rel}")
        rr.save_facts(facts, facts_dir)

    def test_both_languages_join_the_fact_onto_the_store(self, tmp_path):
        facts_dir = tmp_path / "install" / ".hermes-runtime"
        store = tmp_path / "tools"
        self._provision_into_store(
            facts_dir, store, "node-26.7.0-linux-x64", "bin/node"
        )

        python_dirs = [
            str(d) for d in runtime_env.managed_path_dirs(facts_dir, store_dir=store)
        ]

        assert python_dirs == [str(store / "node-26.7.0-linux-x64" / "bin")]
        assert python_dirs == _ts_path_entries(tmp_path, facts_dir, store)

    def test_two_installs_sharing_one_store_agree_in_both_languages(self, tmp_path):
        """The point of the store: separate facts, one copy of the bytes."""
        store = tmp_path / "tools"
        dirs_per_install = []
        for name in ("install-a", "install-b"):
            facts_dir = tmp_path / name / ".hermes-runtime"
            self._provision_into_store(
                facts_dir, store, "node-26.7.0-linux-x64", "bin/node"
            )
            python_dirs = [
                str(d)
                for d in runtime_env.managed_path_dirs(facts_dir, store_dir=store)
            ]
            assert python_dirs == _ts_path_entries(tmp_path, facts_dir, store)
            dirs_per_install.append(python_dirs)

        assert dirs_per_install[0] == dirs_per_install[1]
        assert len(list(store.iterdir())) == 1

    def test_both_languages_spread_store_relative_pathDirs(self, tmp_path):
        """PortableGit's three dirs must resolve against the store too."""
        facts_dir = tmp_path / "install" / ".hermes-runtime"
        store = tmp_path / "tools"
        entry = "git-2.53.0.3-win32-x64"
        for sub in ("cmd", "bin", "usr/bin"):
            (store / entry / sub).mkdir(parents=True)
        (store / entry / "cmd" / "git.exe").write_text("#!/bin/sh\n")
        rr.save_facts(
            {
                "git": rr.RuntimeFact(
                    version="2.53.0.3",
                    path=f"{entry}/cmd/git.exe",
                    path_dirs=[f"{entry}/cmd", f"{entry}/bin", f"{entry}/usr/bin"],
                )
            },
            facts_dir,
        )

        python_dirs = [
            str(d) for d in runtime_env.managed_path_dirs(facts_dir, store_dir=store)
        ]

        assert len(python_dirs) == 3
        assert all(d.startswith(str(store / entry)) for d in python_dirs)
        assert python_dirs == _ts_path_entries(tmp_path, facts_dir, store)

    def test_a_self_contained_artifact_needs_no_store_argument(self, tmp_path):
        """The Nix bundle and the desktop payload ARE their own store, and
        both readers must default to that without being told."""
        payload = tmp_path / "agent-payload"
        self._provision_into_store(payload, payload, "node-26.7.0-linux-x64", "bin/node")

        python_dirs = [str(d) for d in runtime_env.managed_path_dirs(payload)]

        assert python_dirs == [str(payload / "node-26.7.0-linux-x64" / "bin")]
        assert python_dirs == _ts_path_entries(tmp_path, payload)


class TestSchemaConstantsMatch:
    def test_schema_version_is_the_same_number_on_both_sides(self):
        ts = BACKEND_ENV_TS.read_text(encoding="utf-8")
        assert f"RUNTIME_FACTS_SCHEMA_VERSION = {rr.FACTS_SCHEMA_VERSION}" in ts

    def test_facts_filename_is_the_same_string_on_both_sides(self):
        ts = BACKEND_ENV_TS.read_text(encoding="utf-8")
        assert f"RUNTIME_FACTS_FILENAME = '{rr.FACTS_FILENAME}'" in ts

    # There is deliberately no test asserting a tool-order literal in the
    # TypeScript source. There used to be one, because the order WAS a
    # literal in both languages and reading the source was the only way to
    # compare them. The order is data now — derived from the pin table's
    # `extends` edges and recorded in the facts file — so the round-trip
    # tests above check the real behaviour instead of the source text.
