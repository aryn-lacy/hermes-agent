"""Hostile archives must not write outside the tool's own directory.

Every managed runtime arrives as an archive from the internet. The
sha256 pin proves the bytes are the ones we reviewed, but that is a
supply-chain control, not a containment one: it says nothing about what
a legitimately-published archive does when unpacked, and a pin refresh
is a human copying a digest. So extraction itself must be safe against
path traversal, symlink escapes, and collisions — and must never
clobber a file it did not create.

Each test here corresponds to an attack that was actually run against
this code. Two of them found real defects.
"""

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from installation import provisioner as rp


def _tar(path: Path, members: dict[str, bytes]) -> Path:
    staging = path.parent / f".stage-{path.stem}"
    with tarfile.open(path, "w:gz") as tf:
        for rel, data in members.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            tf.add(target, arcname=rel)
    return path


class TestPathTraversal:
    def test_zip_entries_cannot_escape_the_destination(self, tmp_path):
        archive = tmp_path / "slip.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../../ESCAPED.txt", "pwned")
            zf.writestr("bin/tool", "fine")

        dest = tmp_path / "sub" / "dest"
        rp._extract(archive, dest)

        assert not (tmp_path / "ESCAPED.txt").exists()
        assert not (tmp_path.parent / "ESCAPED.txt").exists()
        assert (dest / "bin" / "tool").is_file()

    def test_absolute_zip_paths_stay_inside_the_destination(self, tmp_path):
        archive = tmp_path / "abs.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("/etc/PWNED.txt", "pwned")

        dest = tmp_path / "dest"
        rp._extract(archive, dest)

        assert not Path("/etc/PWNED.txt").exists()
        assert (dest / "etc" / "PWNED.txt").is_file()

    def test_tar_entries_cannot_escape_the_destination(self, tmp_path):
        payload = tmp_path / "payload.txt"
        payload.write_text("pwned")
        archive = tmp_path / "slip.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(payload, arcname="../../ESCAPED.txt")

        with pytest.raises(Exception):
            rp._extract(archive, tmp_path / "sub" / "dest")

        assert not (tmp_path.parent / "ESCAPED.txt").exists()

    def test_a_symlink_cannot_be_used_to_write_outside(self, tmp_path):
        """Classic two-entry attack: a symlink pointing out of the tree,
        then a regular file written through it."""
        victim = tmp_path / "outside.txt"
        victim.write_text("ORIGINAL")

        archive = tmp_path / "symlink.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            link = tarfile.TarInfo("escape")
            link.type = tarfile.SYMTYPE
            link.linkname = "../outside.txt"
            tf.addfile(link)

            data = b"OVERWRITTEN"
            entry = tarfile.TarInfo("escape")
            entry.size = len(data)
            tf.addfile(entry, io.BytesIO(data))

        with pytest.raises(Exception):
            rp._extract(archive, tmp_path / "sub" / "dest")

        assert victim.read_text() == "ORIGINAL"


class TestChmodStaysInsideTheDestination:
    def test_a_traversing_entry_cannot_chmod_a_file_outside(self, tmp_path):
        """REGRESSION: the executable-bit restore used the raw entry name
        instead of the path zipfile actually wrote to, so an entry called
        "../../victim" chmod'd a file outside the destination — an
        arbitrary chmod +x for anyone who can serve an archive. The
        extract was always safe; the chmod loop was not."""
        victim = tmp_path / "victim.txt"
        victim.write_text("not executable")
        victim.chmod(0o644)

        archive = tmp_path / "chmod.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            info = zipfile.ZipInfo("../../victim.txt")
            info.external_attr = 0o777 << 16
            zf.writestr(info, "x")

        rp._extract(archive, tmp_path / "sub" / "dest")

        assert victim.stat().st_mode & 0o777 == 0o644
        assert victim.read_text() == "not executable"

    def test_the_executable_bit_is_still_restored_for_real_entries(self, tmp_path):
        """The fix must not break the reason the loop exists: zip drops
        the exec bit, and an un-executable `uv` is a broken install."""
        archive = tmp_path / "exec.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            info = zipfile.ZipInfo("uv")
            info.external_attr = 0o755 << 16
            zf.writestr(info, "#!/bin/sh\n")

        dest = tmp_path / "dest"
        rp._extract(archive, dest)

        assert (dest / "uv").stat().st_mode & 0o111


class TestFlattenNeverOverwrites:
    def test_a_hidden_top_level_file_is_not_replaced(self, tmp_path):
        """REGRESSION: the lone-wrapper check skipped dotfiles, so an
        archive shaped {".config", "wrapper/.config"} looked like a bare
        wrapper and the move silently replaced the outer file."""
        archive = _tar(
            tmp_path / "hidden.tar.gz",
            {
                ".config": b"TOP LEVEL ORIGINAL",
                "wrapper/.config": b"FROM WRAPPER",
                "wrapper/bin/tool": b"x",
            },
        )
        dest = tmp_path / "dest"
        rp._extract(archive, dest)

        rp._flatten_single_dir(dest)

        assert (dest / ".config").read_bytes() == b"TOP LEVEL ORIGINAL"

    def test_a_child_named_like_its_wrapper_refuses_rather_than_clobbers(
        self, tmp_path
    ):
        """An unflattened tree is merely ugly; a clobbered file is data
        loss. Refusing also beats shutil.move's own error, which names a
        temp path and reads like a bug in us."""
        archive = _tar(tmp_path / "same.tar.gz", {"gh/gh": b"inner"})
        dest = tmp_path / "dest"
        rp._extract(archive, dest)

        with pytest.raises(RuntimeError, match="would overwrite gh"):
            rp._flatten_single_dir(dest)

        assert (dest / "gh" / "gh").read_bytes() == b"inner"

    def test_a_real_wrapper_still_unwraps(self, tmp_path):
        archive = _tar(
            tmp_path / "wrapped.tar.gz", {"gh_2.97.0_linux_amd64/bin/gh": b"x"}
        )
        dest = tmp_path / "dest"
        rp._extract(archive, dest)

        rp._flatten_single_dir(dest)

        assert (dest / "bin" / "gh").is_file()

    def test_a_lone_layout_dir_is_left_alone(self, tmp_path):
        """A bare bin/ IS the tool's layout, not a wrapper. Hoisting it
        would have broken gh on every platform."""
        archive = _tar(tmp_path / "flat.tar.gz", {"bin/gh": b"x"})
        dest = tmp_path / "dest"
        rp._extract(archive, dest)

        rp._flatten_single_dir(dest)

        assert (dest / "bin" / "gh").is_file()


class TestToolIsolation:
    def test_staging_replaces_only_its_own_tool_directory(self, tmp_path):
        """Each tool owns exactly <runtime dir>/<tool>/. Neighbouring
        tools and the shared cache must survive a reprovision."""
        runtime = tmp_path / "rt"
        (runtime / "node" / "bin").mkdir(parents=True)
        (runtime / "node" / "bin" / "node").write_text("other tool")
        cache = runtime / "cache" / "important.json"
        cache.parent.mkdir(parents=True)
        cache.write_text("USER DATA")

        archive = _tar(tmp_path / "gh.tar.gz", {"bin/gh": b"x"})
        rp._extract(archive, runtime / "gh")

        assert (runtime / "node" / "bin" / "node").read_text() == "other tool"
        assert cache.read_text() == "USER DATA"

    def test_restaging_clears_a_stale_tree_first(self, tmp_path):
        """A file from an older version must not linger inside the tool's
        own directory and shadow the new layout."""
        dest = tmp_path / "gh"
        dest.mkdir()
        (dest / "STALE").write_text("from an older version")

        archive = _tar(tmp_path / "gh.tar.gz", {"bin/gh": b"x"})
        rp._extract(archive, dest)

        assert not (dest / "STALE").exists()
        assert (dest / "bin" / "gh").is_file()
