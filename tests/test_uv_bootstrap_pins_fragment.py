"""The uv bootstrap pin fragments must match installation/runtime-pins.json.

The installers (install.sh / install.ps1) are fetched standalone (curl | sh,
irm | iex) and bootstrap uv BEFORE any checkout exists, so they cannot read
the pin table at run time. scripts/gen-uv-bootstrap-pins.py derives an
inline fragment from the table and splices it between markers in each
script. These tests enforce the derive-don't-store contract: the stored
bytes can never drift from the pin table, and the installers actually
consume the pinned values instead of an unpinned "latest" channel.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-uv-bootstrap-pins.py"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
PINS = REPO_ROOT / "installation" / "runtime-pins.json"


def test_fragments_match_the_pin_table():
    """--check regenerates from the table and fails on any drift."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"uv bootstrap fragments drifted from the pin table:\n"
        f"{result.stdout}{result.stderr}"
    )


def test_installers_carry_the_pinned_uv_version():
    """Both installers hold the exact uv version the pin table names."""
    version = json.loads(PINS.read_text(encoding="utf-8"))["tools"]["uv"]["version"]
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    assert f'UV_PIN_VERSION="{version}"' in sh
    assert f'$script:UvPinVersion = "{version}"' in ps1


def test_installers_do_not_fetch_unpinned_uv():
    """The astral latest-channel installers must never come back.

    astral.sh/uv/install.sh and install.ps1 resolve "latest" at run time,
    which defeats pins-as-repo-data: a hermes install could silently get a
    uv nobody reviewed. The only astral.sh mentions allowed are the manual
    -install docs URL (docs.astral.sh).
    """
    for path in (INSTALL_SH, INSTALL_PS1):
        source = path.read_text(encoding="utf-8")
        assert "astral.sh/uv/install.sh" not in source, path.name
        assert "astral.sh/uv/install.ps1" not in source, path.name
