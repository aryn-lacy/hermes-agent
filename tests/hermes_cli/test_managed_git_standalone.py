"""The managed git works with nothing from the host.

This is the point of bundling git at all: on macOS `/usr/bin/git` is the
xcode-select SHIM, and invoking it on a machine without the Command Line
Tools pops a modal install dialog. Hermes must never need it.

The proof is a real clone run with an EMPTY environment — no PATH, no
system git, no /etc/gitconfig — using only what the registry facts and
`managed_tool_env()` provide. If the portable-git contract were wrong,
git would fail to find its own helpers and this would break.

Marked `network`: it downloads the pinned dugite-native release.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from installation import env as runtime_env
from installation import provisioner as rp
from installation.registry import load_facts, load_pins, save_facts, RuntimeFact

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="dugite-native is the POSIX supplier; win32 uses PortableGit",
)


@pytest.fixture(scope="module")
def provisioned_git(tmp_path_factory) -> Path:
    """A real, digest-verified git from the pin table.

    Goes through the normal provisioner: this is the path users get, and
    a fixture that shortcut it would prove less. The system-git-first
    probe is disabled for the module — this host HAS a git, but these
    tests exist to exercise the PINNED artifact's contract (the exec
    path, templates, PREFIX), which a system fact deliberately skips.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(rp, "probe_system_git", lambda: None)
    runtime_dir = tmp_path_factory.mktemp("runtime")
    result = rp.provision_tool("git", runtime_dir=runtime_dir)
    mp.undo()
    if not result.ok:  # offline CI, GitHub outage
        pytest.skip(f"could not provision git: {result.detail}")
    return runtime_dir


@pytest.fixture(scope="module")
def git_entry(provisioned_git) -> Path:
    """The git tree's root inside the store, read from the facts.

    Derived rather than spelled out: the entry name carries the pinned
    version, so restating it here would make these tests fail on every
    pin bump for no behavioural reason.
    """
    binary = runtime_env.managed_tool_binary("git", provisioned_git)
    assert binary is not None, "git provisioned but not resolvable from its facts"
    return binary.parent.parent


class TestManagedGitStandsAlone:
    def test_the_pinned_archive_matched_its_digest(self, provisioned_git, git_entry):
        """Provisioning at all proves it: the provisioner aborts on a
        digest mismatch before extracting anything."""
        assert (git_entry / "bin" / "git").is_file()
        assert load_facts(provisioned_git)["git"].version == load_pins()["git"]["version"]

    def test_git_runs_with_an_empty_environment(self, provisioned_git, git_entry):
        git = git_entry / "bin" / "git"
        env = runtime_env.managed_tool_env(provisioned_git)

        proc = subprocess.run(
            [str(git), "--version"], env=env, capture_output=True, text=True
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("git version")

    def test_the_portable_git_contract_is_exported(self, provisioned_git, git_entry):
        env = runtime_env.managed_tool_env(provisioned_git)

        # Every one of these must point INSIDE git's own store entry: the
        # whole idea is that nothing resolves against the host.
        for key in (
            "GIT_EXEC_PATH",
            "GIT_TEMPLATE_DIR",
            "GIT_CONFIG_SYSTEM",
            "GIT_SSL_CAINFO",
        ):
            assert key in env, f"{key} missing from the managed git env"
            assert str(git_entry) in env[key], f"{key} escapes git's store entry"

    def test_a_real_clone_needs_no_system_git(
        self, provisioned_git, git_entry, tmp_path
    ):
        """The end-to-end claim. Empty env + the managed git only."""
        git = git_entry / "bin" / "git"
        env = runtime_env.managed_tool_env(provisioned_git)
        env["HOME"] = str(tmp_path)  # git wants somewhere to look for ~/.gitconfig

        source = tmp_path / "source"
        source.mkdir()
        for args in (
            ["init", "-q"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"],
        ):
            proc = subprocess.run(
                [str(git), *args], cwd=source, env=env, capture_output=True, text=True
            )
            assert proc.returncode == 0, proc.stderr

        target = tmp_path / "clone"
        proc = subprocess.run(
            [str(git), "clone", "-q", str(source), str(target)],
            env=env,
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, proc.stderr
        assert (target / ".git").is_dir()

    def test_managed_git_leads_the_assembled_path(self, provisioned_git, git_entry):
        dirs = runtime_env.managed_path_dirs(provisioned_git)
        assert dirs, "the provisioned git should contribute a PATH entry"

        merged = runtime_env.with_managed_runtimes(
            {"PATH": "/usr/bin:/bin"}, provisioned_git
        )
        entries = merged["PATH"].split(os.pathsep)

        git_dir = str(git_entry / "bin")
        assert git_dir in entries
        # /usr/bin holds the xcode-select shim on macOS: ours must win.
        assert entries.index(git_dir) < entries.index("/usr/bin")

    def test_system_git_gets_no_git_env(self, tmp_path):
        """Exporting GIT_EXEC_PATH at a git we do not own would break it."""
        empty_runtime = tmp_path / "empty"
        empty_runtime.mkdir()

        env = runtime_env.managed_tool_env(empty_runtime)

        assert "GIT_EXEC_PATH" not in env
        assert "GIT_SSL_CAINFO" not in env
