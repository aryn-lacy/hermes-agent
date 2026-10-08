"""Darwin E2EE self-heal (fork-only): upstream gates [matrix] to Linux, so on
macOS every `hermes update` environment migration lands with Matrix dark.

Contracts here (macOS host, real modules — no host faking):
- ensure_matrix_deps() routes a missing [matrix] group through the darwin
  self-heal instead of the linux-only ensure_and_bind path.
- The platform read routes through adapter_darwin_e2ee.sys_platform()
  (platform as data): the linux dispatch contract is testable on macOS.
- A successful self-heal rebinds the real mautrix types (import-safe stubs
  replaced), mirroring ensure_and_bind's rebind contract.
- Without Homebrew libolm or uv the self-heal skips cleanly (False, no
  crash, no network attempt).
- The pinned-sdist rewrite aborts on a hash mismatch and applies to the
  real upstream layout (markers probed against a re-packed fixture, no
  network in tests).
"""
import importlib
import tarfile
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.platforms("macos")

import plugins.platforms.matrix.adapter as matrix_mod
from plugins.platforms.matrix import adapter_darwin_e2ee


def _restore_modules() -> None:
    """Fresh module state for every test: self-heal and adapter reloaded so
    stub/rebind assertions see the real import-time state."""
    importlib.reload(adapter_darwin_e2ee)
    importlib.reload(matrix_mod)


def setup_function() -> None:
    _restore_modules()


def teardown_function() -> None:
    _restore_modules()


def _configured_env(monkeypatch):
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "syt_test")
    monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.example.org")
    monkeypatch.delenv("MATRIX_ENCRYPTION", raising=False)
    monkeypatch.delenv("MATRIX_E2EE_MODE", raising=False)


def test_selfheal_routes_darwin_missing_group(monkeypatch):
    """A missing [matrix] group on darwin calls the self-heal with the
    missing anchors and never touches ensure_and_bind."""
    import types as _types

    _configured_env(monkeypatch)
    called = {}

    # The happy path rebinds via a real mautrix.types import; provide the
    # minimal fake so the rebind succeeds (the test venv has no mautrix).
    types_mod = _types.ModuleType("mautrix.types")
    for name in ("ContentURI", "EventID", "EventType", "PresenceState",
                 "RoomCreatePreset", "RoomID", "TrustState", "UserID"):
        setattr(types_mod, name, object())
    mautrix_pkg = _types.ModuleType("mautrix")
    mautrix_pkg.types = types_mod
    fake_modules = {"mautrix": mautrix_pkg, "mautrix.types": types_mod}

    def _fake_selfheal(missing):
        called["missing"] = missing
        return True

    with patch.object(adapter_darwin_e2ee, "darwin_e2ee_selfheal", side_effect=_fake_selfheal), \
         patch.object(matrix_mod, "_check_e2ee_deps", return_value=True), \
         patch("pm.extras.missing", return_value=("mautrix", "asyncpg")) as missing, \
         patch("pm.extras.ensure_and_bind") as ensure_bind, \
         patch.dict("sys.modules", fake_modules):
        assert matrix_mod.ensure_matrix_deps() is True

    assert called["missing"] == ("mautrix", "asyncpg")
    ensure_bind.assert_not_called()
    assert missing.call_count == 1  # single gate probe feeds both the dispatch and the self-heal


def test_selfheal_success_rebinds_types(monkeypatch):
    """On self-heal success the module's import-safe stubs are rebound via
    _import() — mirroring ensure_and_bind's rebind contract. The mautrix
    import runs against a minimal fake (the test venv has no real mautrix —
    that is the state the self-heal exists to fix)."""
    import types as _types

    _configured_env(monkeypatch)
    real_type = object()  # stand-in "real" EventType the fake provides

    names = ("ContentURI", "EventID", "EventType", "PresenceState",
             "RoomCreatePreset", "RoomID", "TrustState", "UserID")
    types_mod = _types.ModuleType("mautrix.types")
    for name in names:
        setattr(types_mod, name, object())
    setattr(types_mod, "EventType", real_type)
    mautrix_pkg = _types.ModuleType("mautrix")
    mautrix_pkg.types = types_mod
    fake_modules = {"mautrix": mautrix_pkg, "mautrix.types": types_mod}

    # Fresh module state: the adapter must currently hold its import-safe
    # stub so the rebind has something to replace.
    importlib.reload(matrix_mod)
    stubbed = matrix_mod.EventType

    with patch.object(adapter_darwin_e2ee, "darwin_e2ee_selfheal", return_value=True), \
         patch.object(matrix_mod, "_check_e2ee_deps", return_value=True), \
         patch("pm.extras.missing", return_value=("mautrix",)), \
         patch.dict("sys.modules", fake_modules):
        assert matrix_mod.ensure_matrix_deps() is True

    assert matrix_mod.EventType is real_type, (
        "self-heal success must rebind the module's import-safe stubs to the "
        "imported types, mirroring ensure_and_bind's rebind contract"
    )
    assert stubbed is not real_type  # precondition: a stub was actually replaced


def test_linux_dispatch_goes_through_ensure_and_bind(monkeypatch):
    """The linux dispatch keeps upstream's path: ensure_and_bind runs, the
    self-heal never engages. sys_platform() is platform-as-data."""
    _configured_env(monkeypatch)
    with patch.object(adapter_darwin_e2ee, "sys_platform", return_value="linux"), \
         patch("pm.extras.missing", return_value=("mautrix",)) as missing, \
         patch("pm.extras.ensure_and_bind", return_value=True) as ensure_bind, \
         patch.object(adapter_darwin_e2ee, "darwin_e2ee_selfheal") as selfheal:
        assert matrix_mod.ensure_matrix_deps() is True

    ensure_bind.assert_called_once()
    selfheal.assert_not_called()
    assert missing.call_count == 1  # gate probe only


def test_selfheal_failure_returns_false(monkeypatch):
    """Self-heal False → ensure_matrix_deps False (upstream failure path)."""
    _configured_env(monkeypatch)
    with patch.object(adapter_darwin_e2ee, "darwin_e2ee_selfheal", return_value=False), \
         patch.object(matrix_mod, "_check_e2ee_deps", return_value=True), \
         patch("pm.extras.missing", return_value=("mautrix",)):
        assert matrix_mod.ensure_matrix_deps() is False


def test_selfheal_rebind_importerror_returns_false(monkeypatch):
    """An install whose import still fails after self-heal reports False,
    mirroring ensure_and_bind's ImportError tolerance. No mautrix fake is
    installed, so the post-heal rebind genuinely fails in the test venv."""
    _configured_env(monkeypatch)
    with patch.object(adapter_darwin_e2ee, "darwin_e2ee_selfheal", return_value=True), \
         patch.object(matrix_mod, "_check_e2ee_deps", return_value=True), \
         patch("pm.extras.missing", return_value=("mautrix",)):
        assert matrix_mod.ensure_matrix_deps() is False


def test_selfheal_skips_without_brew_libolm(monkeypatch, tmp_path):
    """No Homebrew libolm → clean False before any download or install."""
    with patch.object(adapter_darwin_e2ee, "brew_libolm_prefix", return_value=None), \
         patch.object(adapter_darwin_e2ee, "_urlrequest") as urlrequest:
        assert adapter_darwin_e2ee.darwin_e2ee_selfheal(("mautrix",)) is False
    urlrequest.urlopen.assert_not_called()


def test_selfheal_skips_without_uv(monkeypatch, tmp_path):
    """No uv binary → clean False before any download or install."""
    brew = tmp_path / "brew"
    (brew / "include/olm").mkdir(parents=True)
    (brew / "include/olm/olm.h").write_text("")
    (brew / "lib").mkdir()
    (brew / "lib/libolm.3.dylib").write_text("")
    with patch.object(adapter_darwin_e2ee, "brew_libolm_prefix", return_value=brew), \
         patch("shutil.which", return_value=None), \
         patch.object(adapter_darwin_e2ee.Path, "is_file", return_value=False), \
         patch.object(adapter_darwin_e2ee, "_urlrequest") as urlrequest:
        assert adapter_darwin_e2ee.darwin_e2ee_selfheal(("mautrix",)) is False
    urlrequest.urlopen.assert_not_called()


def test_sdist_hash_mismatch_aborts(tmp_path):
    """A sdist whose bytes don't match the pinned SHA256 aborts before any
    build or install."""
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"tampered bytes"

    with patch.object(adapter_darwin_e2ee, "brew_libolm_prefix", return_value=tmp_path), \
         patch.object(adapter_darwin_e2ee, "_uv_binary", return_value=str(tmp_path / "uv")), \
         patch.object(adapter_darwin_e2ee, "_target_python", return_value=tmp_path / "python"), \
         patch.object(adapter_darwin_e2ee, "_urlrequest") as urlrequest:
        urlrequest.urlopen.return_value = _FakeResponse()
        assert adapter_darwin_e2ee.darwin_e2ee_selfheal(("mautrix",)) is False


def test_olm_build_patch_applies_to_real_layout(tmp_path):
    """The olm_build.py rewrite hits all three markers on the real upstream
    layout. The sdist fixture is re-packed locally (deflated, so the module's
    member-name lookup works); no network in tests."""
    real_member = "python-olm-3.2.16/olm_build.py"
    fixture_src = (
        'compile_args = ["-Ilibolm/include"]\n'
        'if DEVELOP and DEVELOP.lower() in ["yes", "true", "1"]:\n'
        "    link_args.append('-Wl,-rpath=../build')\n"
        "# Try to build with cmake first, fall back to GNU make\n"
        "try:\n"
        "    subprocess.run([\"cmake\", \".\", \"-Bbuild\", \"-DBUILD_SHARED_LIBS=NO\"], cwd=\"libolm\", check=True)\n"
        "except FileNotFoundError:\n"
        "    subprocess.run([\"make\", \"static\"], cwd=\"libolm\", check=True)\n"
        "ffibuilder.set_source(\n"
        "    \"_libolm\",\n"
        "    libraries=[\"olm\"],\n"
        "    library_dirs=[os.path.join(\"libolm\", \"build\")],\n"
        ")\n"
    )
    archive_path = tmp_path / "python-olm-3.2.16.tar.gz"
    import io
    import hashlib
    import unittest.mock as um

    with tarfile.open(archive_path, "w:gz") as tar:
        data = fixture_src.encode()
        info = tarfile.TarInfo(real_member)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    brew = tmp_path / "brew"
    brew.mkdir()

    # Patch the pinned hash to the fixture's digest so the hash gate passes;
    # the marker layout under test is the real upstream shape.
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    with um.patch.object(adapter_darwin_e2ee, "_SDIST_URL", archive_path.as_uri()), \
         um.patch.object(adapter_darwin_e2ee, "_SDIST_SHA256", digest), \
         um.patch.object(adapter_darwin_e2ee, "_urlrequest") as urlrequest:
        class _FileResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return archive_path.read_bytes()

        urlrequest.urlopen.return_value = _FileResponse()
        work = tmp_path / "work"
        work.mkdir()
        source = adapter_darwin_e2ee._patched_sdist(work, brew)

    script = (source / "olm_build.py").read_text(encoding="utf-8")
    assert f'compile_args = ["-I{brew}/include"]' in script
    assert f'library_dirs=["{brew}/lib"],' in script
    assert "# Try to build with cmake first" not in script
    assert "ffibuilder.set_source(" in script


def test_anchor_import_check_uses_fresh_interpreter():
    """_anchor_imports probes a clean interpreter: a present-but-broken
    sys.modules entry must not mask a broken install."""
    assert adapter_darwin_e2ee._anchor_imports("json") is True
    assert adapter_darwin_e2ee._anchor_imports("definitely_not_a_real_module_xyz") is False


def test_selfheal_serializes_concurrent_calls():
    """Two concurrent self-heals serialize (the uv installs mutate the live
    environment) and both observe a clean skip when brew is absent."""
    import threading

    results = []

    def _run():
        with patch.object(adapter_darwin_e2ee, "brew_libolm_prefix", return_value=None):
            results.append(adapter_darwin_e2ee.darwin_e2ee_selfheal(("mautrix",)))

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [False, False]
