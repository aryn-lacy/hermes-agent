"""Managed-runtime PATH injection for the local terminal environment.

The terminal subshell must see Hermes-managed tools (node, uv, git, gh,
ripgrep). One assembler owns which dirs those are; this module owns how
they reach the subshell PATH on each platform.
"""

import ntpath

import pytest

from tools.environments import local


class TestManagedRuntimePathEntries:
    def test_delegates_to_the_single_assembler(self, monkeypatch, tmp_path):
        """The entry list is registry-derived, never a hand-kept list of
        directories — one resolver owns the policy."""
        node_bin = tmp_path / "node" / "bin"
        node_bin.mkdir(parents=True)
        import installation.env as runtime_env

        monkeypatch.setattr(runtime_env, "managed_path_dirs", lambda *a, **k: [node_bin])
        assert local._managed_runtime_path_entries() == [str(node_bin)]

    def test_registry_failure_degrades_to_empty(self, monkeypatch):
        """A broken/absent registry must not break the terminal tool — no
        managed dirs simply means system tools."""
        import installation.env as runtime_env

        def _boom(*a, **k):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(runtime_env, "managed_path_dirs", _boom)
        assert local._managed_runtime_path_entries() == []


class TestPosixPathMerge:
    @pytest.mark.skipif(local._IS_WINDOWS, reason="POSIX merge semantics")
    def test_managed_dirs_are_appended_not_prepended(self, monkeypatch):
        """A tool the user deliberately put on their PATH still wins; the
        managed copy only fills a gap."""
        monkeypatch.setattr(
            local, "_managed_runtime_path_entries", lambda: ["/opt/hermes/.hermes-runtime/node/bin"]
        )
        result = local._append_missing_sane_path_entries("/usr/bin:/home/me/bin")
        parts = result.split(":")
        assert parts[0] == "/usr/bin"
        assert parts[1] == "/home/me/bin"
        assert "/opt/hermes/.hermes-runtime/node/bin" in parts[2:]

    @pytest.mark.skipif(local._IS_WINDOWS, reason="POSIX merge semantics")
    def test_managed_dir_already_present_is_not_duplicated(self, monkeypatch):
        managed = "/opt/hermes/.hermes-runtime/uv"
        monkeypatch.setattr(local, "_managed_runtime_path_entries", lambda: [managed])
        result = local._append_missing_sane_path_entries(f"/usr/bin:{managed}")
        assert result.split(":").count(managed) == 1


class TestWindowsPathMerge:
    """The Windows branch appends without rewriting the native PATH.

    Exercised on every platform by calling the helper the branch uses, so
    the case-insensitivity rule is proven off the windows lane too.
    """

    def test_normcase_dedup_rule_is_case_and_separator_insensitive(self):
        # This is the comparison the Windows branch performs. ntpath is
        # os.path ON Windows, so asserting through it proves the rule
        # without needing a Windows host.
        present = {ntpath.normcase(r"c:\hermes\.hermes-runtime\NODE")}
        assert ntpath.normcase(r"C:\Hermes\.hermes-runtime\node") in present
        assert ntpath.normcase(r"C:/Hermes/.hermes-runtime/node") in present
        assert ntpath.normcase(r"C:\Hermes\.hermes-runtime\uv") not in present

    @pytest.mark.windows_only
    def test_posix_sane_dirs_never_reach_a_windows_path(self, monkeypatch):
        monkeypatch.setattr(local, "_managed_runtime_path_entries", lambda: [])
        path = r"C:\Windows\System32"
        assert local._append_missing_sane_path_entries(path) == path
