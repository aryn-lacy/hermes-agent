"""The three call sites now carry the portable-git contract.

Each asserts on the ENV a real caller would hand to git, against a real
provisioned runtime dir. The bug these cover: PATH alone put a relocated
git in front of the agent with none of the vars it needs to find its own
helpers, so `git clone https://…` failed with "'remote-http' is not a git
command" while `git --version` kept working.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from installation import env as runtime_env
from installation import provisioner as rp
from hermes_cli._subprocess_compat import noninteractive_git_env

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="dugite-native is the POSIX supplier; win32 uses PortableGit",
)


@pytest.fixture(scope="module")
def provisioned_git(tmp_path_factory) -> Path:
    """A real, digest-verified git from the pin table.

    System-git-first is bypassed: these tests assert the PINNED git's
    env contract (GIT_EXEC_PATH and friends), which a system fact
    deliberately does not get.
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
    """git's tree inside the store, resolved through its own facts.

    Derived rather than spelled out: the store entry name carries the
    pinned version, and hardcoding it here would break these tests on
    every pin bump without any behaviour changing.
    """
    binary = runtime_env.managed_tool_binary("git", provisioned_git)
    assert binary is not None, "git provisioned but not resolvable from its facts"
    return binary.parent.parent


def _clone_stderr(env: dict) -> str:
    """Stderr of a clone against a closed port.

    Port 9 (discard) refuses fast. A connection error means git got as
    far as the network — i.e. it FOUND its remote helper. The helper
    failure happens earlier and looks completely different.
    """
    proc = subprocess.run(
        ["git", "clone", "http://127.0.0.1:9/x", "dest"],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        cwd="/tmp",
        env=env,
    )
    return proc.stderr


class TestTheHelperContractReachesGit:
    def test_path_alone_cannot_find_the_remote_helper(self, git_entry):
        """The bug, pinned: this is what PATH-only used to produce."""
        git_bin = git_entry / "bin"
        stderr = _clone_stderr({"PATH": f"{git_bin}:/usr/bin:/bin", "HOME": "/tmp"})

        assert "remote-http" in stderr, (
            "expected the helper-resolution failure this test exists to "
            f"contrast against, got: {stderr!r}"
        )

    def test_the_tool_env_makes_the_same_clone_reach_the_network(
        self, provisioned_git, git_entry
    ):
        git_bin = git_entry / "bin"
        env = {"PATH": f"{git_bin}:/usr/bin:/bin", "HOME": "/tmp"}
        env.update(runtime_env.managed_tool_env(provisioned_git))

        stderr = _clone_stderr(env)

        assert "remote-http" not in stderr
        assert "unable to access" in stderr or "Could not connect" in stderr

    def test_prefix_is_exported_for_a_relocated_git(self, provisioned_git, git_entry):
        """Dugite sets PREFIX on linux so a git running from an arbitrary
        location can resolve things; we were missing it."""
        env = runtime_env.managed_tool_env(provisioned_git)

        if sys.platform.startswith("linux"):
            assert env["PREFIX"] == str(git_entry)
        else:
            assert "PREFIX" not in env


class TestInternalGitCallersGetIt:
    def test_noninteractive_git_env_carries_the_tool_env(
        self, provisioned_git, git_entry, monkeypatch
    ):
        """Every internal caller (MCP installs, plugin updates, worktree
        fetches) goes through this one helper."""
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(provisioned_git))

        env = noninteractive_git_env(base={"PATH": "/usr/bin:/bin"})

        assert env["GIT_EXEC_PATH"] == str(git_entry / "libexec" / "git-core")
        # ...without losing what it already did.
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_a_caller_value_is_never_clobbered(self, provisioned_git, monkeypatch):
        """A user pointing at their own git tooling wins."""
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(provisioned_git))

        env = noninteractive_git_env(base={"GIT_EXEC_PATH": "/opt/mine/libexec"})

        assert env["GIT_EXEC_PATH"] == "/opt/mine/libexec"

    def test_it_fails_open_without_a_runtime_dir(self, tmp_path, monkeypatch):
        """A system git must keep working; exporting GIT_EXEC_PATH at a
        git we do not own would break it."""
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "absent"))

        env = noninteractive_git_env(base={"PATH": "/usr/bin:/bin"})

        assert "GIT_EXEC_PATH" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"


class TestTheTerminalToolGetsIt:
    def test_the_subshell_env_carries_the_tool_env(
        self, provisioned_git, git_entry, monkeypatch
    ):
        """The agent's own `git clone` runs here."""
        from tools.environments import local

        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(provisioned_git))
        env: dict = {}

        local._apply_managed_runtime_tool_env(env)

        assert env["GIT_EXEC_PATH"] == str(git_entry / "libexec" / "git-core")

    def test_it_does_not_overwrite_an_existing_value(
        self, provisioned_git, monkeypatch
    ):
        from tools.environments import local

        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(provisioned_git))
        env = {"GIT_EXEC_PATH": "/opt/mine/libexec"}

        local._apply_managed_runtime_tool_env(env)

        assert env["GIT_EXEC_PATH"] == "/opt/mine/libexec"
