"""installation.pip_ladder: one ladder, policy by argument.

Driven with fake uv/pip binaries so every tier decision is observable
without network. The stdlib-only contract rides the same run-bare audit
as the rest of the installation package.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

from installation import pip_ladder
from tests.test_installation_stdlib_only import run_bare


def _fake_bin(path: Path, exit_code: int = 0, marker: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stderr.write({marker!r})\n"
        f"raise SystemExit({exit_code})\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestStdlibOnly:
    def test_imports_bare(self):
        result = run_bare(
            """
            from installation import pip_ladder
            out = pip_ladder.pip_install(())
            assert out.ok and out.tier == "none", out
            print("ok")
            """
        )
        assert result.returncode == 0, result.stderr


class TestTierPolicy:
    def test_uv_success_never_reaches_pip(self, tmp_path, monkeypatch):
        uv = _fake_bin(tmp_path / "uv", exit_code=0)
        calls: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, **kw):
            calls.append([str(c) for c in cmd])
            return real_run(cmd, **kw)

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(["somepkg"], uv_bin=str(uv))

        assert out.ok and out.tier == "uv"
        assert len(calls) == 1
        assert calls[0][:3] == [str(uv), "pip", "install"]

    def test_resolver_failure_final_stops_at_uv(self, tmp_path, monkeypatch):
        """Lazy policy: uv said no after SEEING the requirements; pip
        must not get a second opinion without uv's exclude-newer etc."""
        uv = _fake_bin(tmp_path / "uv", exit_code=1, marker="no solution")
        pip_ran = []
        monkeypatch.setattr(
            pip_ladder.sys, "executable", str(_fake_bin(tmp_path / "py"))
        )

        out = pip_ladder.pip_install(
            ["somepkg"], uv_bin=str(uv), uv_resolver_failure_is_final=True
        )

        assert not out.ok and out.tier == "uv"
        assert "no solution" in out.stderr
        assert pip_ran == []

    def test_setup_policy_falls_through_to_pip(self, tmp_path, monkeypatch):
        """Setup-hook policy: any tier that works is a win."""
        uv = _fake_bin(tmp_path / "uv", exit_code=1, marker="uv unhappy")

        seen = []
        real_run = subprocess.run

        def spy(cmd, **kw):
            seen.append([str(c) for c in cmd])
            if "-m" in cmd and "pip" in cmd:
                if "--version" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, "pip 25.0", "")
                return subprocess.CompletedProcess(cmd, 0, "installed", "")
            return real_run(cmd, **kw)

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(
            ["somepkg"], uv_bin=str(uv), uv_resolver_failure_is_final=False
        )

        assert out.ok and out.tier == "pip"
        assert any("--version" in c for c in seen)  # probed before install

    def test_vanished_uv_is_availability_not_verdict(self, tmp_path, monkeypatch):
        """FileNotFoundError means uv never evaluated anything — pip is a
        valid fallback even under resolver-final policy."""
        seen = []

        def spy(cmd, **kw):
            seen.append([str(c) for c in cmd])
            if str(cmd[0]).endswith("uv-gone"):
                raise FileNotFoundError(cmd[0])
            if "--version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "pip 25.0", "")
            return subprocess.CompletedProcess(cmd, 0, "installed", "")

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(
            ["somepkg"],
            uv_bin=str(tmp_path / "uv-gone"),
            uv_resolver_failure_is_final=True,
        )

        assert out.ok and out.tier == "pip"

    def test_target_and_constraints_reach_both_tiers(self, tmp_path, monkeypatch):
        uv = _fake_bin(tmp_path / "uv", exit_code=1)
        seen = []

        def spy(cmd, **kw):
            seen.append([str(c) for c in cmd])
            if str(cmd[0]) == str(uv):
                return subprocess.CompletedProcess(cmd, 1, "", "resolver no")
            if "--version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "pip", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        pip_ladder.pip_install(
            ["pkg"],
            uv_bin=str(uv),
            target=tmp_path / "overlay",
            constraints=tmp_path / "cons.txt",
        )

        installs = [c for c in seen if "install" in c and "--version" not in c]
        for cmd in installs:
            assert "--target" in cmd and "--constraint" in cmd

    def test_ensurepip_heals_a_pipless_venv(self, monkeypatch):
        """The `uv venv` no-pip case, with uv also unavailable: the
        ladder must bootstrap pip rather than dead-ending."""
        stages = {"probed": False, "bootstrapped": False}

        def spy(cmd, **kw):
            cmd_s = [str(c) for c in cmd]
            if "--version" in cmd_s:
                stages["probed"] = True
                return subprocess.CompletedProcess(cmd, 1, "", "No module named pip")
            if "ensurepip" in cmd_s:
                stages["bootstrapped"] = True
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, "installed", "")

        monkeypatch.setattr(pip_ladder.subprocess, "run", spy)

        out = pip_ladder.pip_install(["pkg"], uv_bin=None)

        assert out.ok and out.tier == "pip"
        assert stages == {"probed": True, "bootstrapped": True}

    def test_never_raises(self, monkeypatch):
        def explode(cmd, **kw):
            raise OSError("everything is broken")

        monkeypatch.setattr(pip_ladder.subprocess, "run", explode)

        out = pip_ladder.pip_install(["pkg"], uv_bin=None)

        assert not out.ok
        assert "broken" in out.stderr or "failed" in out.stderr


class TestConsumersRideTheLadder:
    """The three former copies must actually delegate — a revert to a
    private ladder would silently restore the drift this killed."""

    @pytest.mark.parametrize(
        "module_name,function_name",
        [
            ("hermes_cli.tools_config", "_pip_install"),
            ("tools.lazy_deps", "_venv_pip_install"),
        ],
    )
    def test_the_copy_is_gone(self, module_name, function_name):
        import ast
        import importlib

        module = importlib.import_module(module_name)
        module_file = module.__file__
        assert module_file is not None
        source = Path(module_file).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == function_name
            ):
                # Judge the CODE, not the docstring — prose is allowed to
                # mention ensurepip when explaining what the ladder does.
                body = list(node.body)
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    body = body[1:]
                code_src = "\n".join(
                    ast.get_source_segment(source, stmt) or "" for stmt in body
                )
                assert "pip_ladder" in code_src, (
                    f"{module_name}.{function_name} no longer delegates to "
                    f"installation.pip_ladder — the third copy is back"
                )
                assert "ensurepip" not in code_src, (
                    f"{module_name}.{function_name} grew its own ladder again"
                )
                return
        pytest.fail(f"{module_name}.{function_name} not found")
