"""The installer does not provision runtime tools; the provisioner does.

Historically ``install.sh`` downloaded Node itself into ``$HERMES_HOME/node``
and had to redirect the bundled npm's global prefix onto PATH, and
``scripts/lib/node-bootstrap.sh`` carried a second copy of the same download
for self-heal. Both are gone (hermes-home lifetime split, phase 3.8): the
installer bootstraps only what Python needs, then hands off to
``hermes_cli.post_update --install-phase``, which installs every pinned tool
into the install-scoped runtime dir.

These are structural assertions about install.sh — the shell cannot be
imported, and a full install is not a unit test.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def test_installer_hands_dep_provisioning_to_the_shared_engine() -> None:
    text = INSTALL_SH.read_text()

    assert "provision_managed_runtimes()" in text
    # The handoff is the whole point: one engine for install AND update.
    assert "hermes_cli.post_update --install-phase" in text


def test_installer_no_longer_downloads_node_itself() -> None:
    """The nodejs.org fetch lived in install_node(); it now lives once, in
    the provisioner. A second copy is how installer and self-heal drift."""
    text = INSTALL_SH.read_text()

    assert "install_node()" not in text
    assert "nodejs.org/dist" not in text


def test_node_bootstrap_helper_is_gone() -> None:
    """It was the third implementation of the same download."""
    assert not (REPO_ROOT / "scripts" / "lib" / "node-bootstrap.sh").exists()


def test_installer_does_not_link_managed_node_onto_path() -> None:
    """Managed Node is install-scoped and private. Symlinking node/npm/npx
    into ~/.local/bin is exactly the cross-install collision the lifetime
    split removes — two installs would fight over one link."""
    text = INSTALL_SH.read_text()

    assert "configure_managed_node_npm_prefix" not in text
    assert 'ln -sf "$HERMES_HOME/node/bin/node"' not in text


def test_installer_does_not_system_package_install_ripgrep() -> None:
    """ripgrep is a pinned managed runtime now, not a system-package hope
    whose version nobody controlled."""
    text = INSTALL_SH.read_text()

    assert 'pkgs+=("ripgrep")' not in text
    assert "cargo install ripgrep" not in text


def test_node_deps_stage_name_survives_for_the_gui_driver() -> None:
    """The stage NAME is protocol (the desktop install driver renders it);
    only its body changed."""
    text = INSTALL_SH.read_text()

    assert '{"name":"node-deps"' in text
    assert "        node-deps)" in text
