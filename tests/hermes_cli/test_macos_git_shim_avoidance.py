"""macOS never resolves git to the xcode-select shim.

`/usr/bin/git` on a Mac without the Command Line Tools is a stub whose
only behaviour is to pop a modal "install developer tools?" dialog. A
background agent process must never invoke it: the user gets a dialog
they did not ask for, from an app that looks idle. Bundling a real git
(dugite-native) is what makes the fallback unnecessary; these tests are
the guarantee that the fallback is actually gone.

The rule is host-independent logic — `is_macos_xcode_shim` takes the
platform from `sys.platform`, so the darwin arm is asserted through the
resolver's data rather than by pretending to be another OS.
"""

import pytest

from installation import env as runtime_env
from installation.registry import RuntimeFact, save_facts


class TestManagedToolBinary:
    def test_returns_the_recorded_binary(self, tmp_path):
        binary = tmp_path / "git" / "bin" / "git"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        save_facts({"git": RuntimeFact(version="2.53.0", path="git/bin/git")}, tmp_path)

        assert runtime_env.managed_tool_binary("git", tmp_path) == binary

    def test_unprovisioned_tool_is_none(self, tmp_path):
        assert runtime_env.managed_tool_binary("git", tmp_path) is None

    def test_recorded_but_vanished_is_none(self, tmp_path):
        """Half-deleted runtime dir must not hand back a path that is not
        there — the caller would spawn it and get ENOENT."""
        save_facts({"git": RuntimeFact(version="2.53.0", path="git/bin/git")}, tmp_path)

        assert runtime_env.managed_tool_binary("git", tmp_path) is None


@pytest.mark.macos_only
class TestXcodeShimDetectionOnMacos:
    def test_usr_bin_git_is_the_shim(self):
        assert runtime_env.is_macos_xcode_shim("/usr/bin/git") is True

    def test_xcrun_is_the_shim(self):
        assert runtime_env.is_macos_xcode_shim("/usr/bin/xcrun") is True

    def test_a_real_git_is_not_the_shim(self):
        assert runtime_env.is_macos_xcode_shim("/opt/homebrew/bin/git") is False
        assert runtime_env.is_macos_xcode_shim("/usr/local/bin/git") is False

    def test_none_is_not_the_shim(self):
        assert runtime_env.is_macos_xcode_shim(None) is False


@pytest.mark.linux_only
class TestUsrBinGitIsFineOffMacos:
    def test_linux_usr_bin_git_is_a_real_git(self):
        """The shim is a macOS mechanism. Refusing /usr/bin/git on Linux
        would break the common case for no reason."""
        assert runtime_env.is_macos_xcode_shim("/usr/bin/git") is False


class TestPluginGitResolutionPrefersManaged:
    def test_managed_git_wins_over_path(self, tmp_path, monkeypatch):
        from hermes_cli import plugins_cmd

        binary = tmp_path / "git" / "bin" / "git"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        save_facts({"git": RuntimeFact(version="2.53.0", path="git/bin/git")}, tmp_path)

        monkeypatch.setattr(
            plugins_cmd, "managed_tool_binary", lambda tool, *a, **kw: binary
        )
        monkeypatch.setattr(plugins_cmd.shutil, "which", lambda _: "/usr/bin/git")
        plugins_cmd._resolve_git_executable.cache_clear()

        assert plugins_cmd._resolve_git_executable() == str(binary)

    @pytest.mark.macos_only
    def test_macos_never_returns_the_shim(self, monkeypatch):
        """With no managed git and only the shim on PATH, the resolver must
        report 'no git' rather than hand back the dialog-popper."""
        from hermes_cli import plugins_cmd

        monkeypatch.setattr(
            plugins_cmd, "managed_tool_binary", lambda tool, *a, **kw: None
        )
        monkeypatch.setattr(plugins_cmd.shutil, "which", lambda _: "/usr/bin/git")
        monkeypatch.setattr(plugins_cmd.os.path, "isfile", lambda _: False)
        plugins_cmd._resolve_git_executable.cache_clear()

        assert plugins_cmd._resolve_git_executable() is None

    def teardown_method(self):
        from hermes_cli import plugins_cmd

        plugins_cmd._resolve_git_executable.cache_clear()
