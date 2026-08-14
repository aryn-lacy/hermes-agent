"""`hermes update` must leave boot-bootstrap records behind (stamp gap 1+2).

The post-update phase runs the same user-state steps boot_bootstrap
would run on next launch (config migration, skills sync, state.db
guard). If the update does not RECORD that it did, the next boot
re-runs the whole slow path — harmless but wasteful, and the record
system's entire point is that the fast path is two file reads.

These tests drive ``cmd_update`` through the mocked-pull harness the
rest of test_cmd_update.py uses and assert on the artifacts: both
record files exist afterwards and carry the checkout's identity.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.main import PROJECT_ROOT, cmd_update

FAKE_SHA = "deadbeefcafe0123456789abcdef0123456789ab"


@pytest.fixture(autouse=True)
def _treat_test_root_as_managed(monkeypatch):
    import installation.tree as runtime_tree

    monkeypatch.setattr(runtime_tree, "is_managed_install_root", lambda p: True)


@pytest.fixture(autouse=True)
def _force_inprocess_phase(monkeypatch):
    """The record write lives at the end of the phase; run it here."""
    import hermes_cli.main as hm

    monkeypatch.setattr(hm, "_spawn_post_update_phase", lambda **kw: None)


@pytest.fixture(autouse=True)
def _home_in_tmp(tmp_path, monkeypatch):
    """Records anchor at HERMES_HOME — keep them out of the real one."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir()


def _git_aware_side_effect(commit_count="1"):
    """The shared fake git, extended to answer ``rev-parse HEAD``.

    read_git_head shells out now (no more packed-refs parsing), so the
    identity the records carry comes from THIS answer — which is what
    lets the test assert records match it exactly.
    """

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n", stderr="")
        if "rev-parse" in joined and "--verify" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "rev-parse" in joined and "HEAD" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{FAKE_SHA}\n", stderr=""
            )
        if "rev-list" in joined:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{commit_count}\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


def _which(name, *args, **kwargs):
    # git resolves (read_git_head needs a binary to exist before the
    # mocked subprocess ever sees the argv); everything else is absent
    # so the update flow takes its no-uv/no-node fallbacks.
    return "/usr/bin/git" if name == "git" else None


def test_update_writes_both_boot_records(tmp_path):
    """Gap 1: after a mocked-pull update, home AND machine records exist
    and carry the identity the fake git reported."""
    from hermes_cli.boot_bootstrap import read_last_known, record_path

    with patch("shutil.which", side_effect=_which), patch(
        "subprocess.run"
    ) as mock_run, patch(
        "hermes_cli.update_cmd._reload_config_modules"
    ), patch(
        "hermes_cli.update_cmd._run_config_check_fresh", return_value=(2, 2)
    ), patch(
        "hermes_cli.main.sys"
    ) as mock_sys:
        mock_sys.stdin.isatty.return_value = False
        mock_sys.stdout.isatty.return_value = False
        mock_sys.executable = "python3"
        mock_run.side_effect = _git_aware_side_effect(commit_count="1")

        cmd_update(SimpleNamespace())

    for scope in ("home", "machine"):
        record = record_path(PROJECT_ROOT, scope)
        assert record.is_file(), f"{scope} record missing after update"
        data = read_last_known(record)
        assert data.get("identity") == FAKE_SHA, (
            f"{scope} record carries {data.get('identity')!r}, "
            f"expected the pulled HEAD {FAKE_SHA!r}"
        )
        assert json.loads(record.read_text())["results"] == {
            "source": "hermes-update"
        }


def test_up_to_date_update_writes_no_records(tmp_path):
    """The records mean 'the steps ran for this identity'. An update that
    finds nothing to pull runs no steps and must record nothing — or a
    later REAL identity change could be masked by a stale record."""
    from hermes_cli.boot_bootstrap import record_path

    with patch("shutil.which", side_effect=_which), patch(
        "subprocess.run"
    ) as mock_run, patch("hermes_cli.main.sys") as mock_sys:
        mock_sys.stdin.isatty.return_value = False
        mock_sys.stdout.isatty.return_value = False
        mock_run.side_effect = _git_aware_side_effect(commit_count="0")

        cmd_update(SimpleNamespace())

    for scope in ("home", "machine"):
        assert not record_path(PROJECT_ROOT, scope).exists()
