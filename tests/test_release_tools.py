"""Exercise release asset retry behavior without contacting GitHub."""
import json
import importlib.util
from pathlib import Path
import subprocess

import pytest


def load_tool(filename):
    source = Path("scripts", filename).resolve()
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("existing", [None, b"verified", b"different"])
def test_release_asset_upload_is_idempotent(tmp_path, monkeypatch, existing):
    tool = load_tool("attach-release.py")
    distribution = tmp_path / "dist"
    distribution.mkdir()
    (distribution / "server.whl").write_bytes(b"verified")
    monkeypatch.setenv("RELEASE_TAG", "service-v0.1.1")
    calls = []
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: json.dumps({
        "assets": [] if existing is None else [{"name": "server.whl"}],
    }))

    def command(arguments, **kwargs):
        calls.append(arguments)
        if arguments[2] == "download":
            Path(arguments[arguments.index("--dir") + 1], "server.whl").write_bytes(existing)

    monkeypatch.setattr(subprocess, "run", command)
    if existing == b"different":
        with pytest.raises(RuntimeError, match="Existing release asset differs"):
            tool.attach_assets(distribution, "service-v0.1.1")
    else:
        tool.attach_assets(distribution, "service-v0.1.1")
    uploads = [call for call in calls if call[2] == "upload"]
    assert len(uploads) == (1 if existing is None else 0)


@pytest.mark.parametrize("valid_hash", [True, False])
def test_registry_verification_checks_integrity_before_install(tmp_path, monkeypatch, valid_hash):
    import hashlib
    import io
    import urllib.request
    import venv
    import zipfile

    tool = load_tool("verify-registry.py")
    distribution = tmp_path / "dist"
    distribution.mkdir()
    wheel = distribution / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example-1.0.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if valid_hash else "different"
    payload = {"info": {"version": "1.0"}, "urls": [{"filename": wheel.name, "digests": {"sha256": digest}}]}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(venv.EnvBuilder, "create", lambda self, path: None)

    if not valid_hash:
        with pytest.raises(AssertionError):
            tool.verify_registry("testpypi", distribution)
        assert not calls
    else:
        tool.verify_registry("testpypi", distribution)
        registry_installs = [args for args in calls if "https://test.pypi.org/simple/" in args]
        assert len(registry_installs) == 1
        assert "--no-deps" in registry_installs[0]
        assert "example==1.0" in registry_installs[0]
        assert any("check" in args for args in calls)
        assert any("init-config" in args for args in calls)
        assert any("validate" in args for args in calls)


@pytest.mark.parametrize("unexpected", [None, "plugin/index.ts", "config.yaml"])
def test_package_verification_rejects_unexpected_wheel_contents(tmp_path, monkeypatch, unexpected):
    import io
    import tarfile
    import venv
    import zipfile

    tool = load_tool("verify-package.py")
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ["tunnel_switch/cli.py", "tunnel_switch/example.yaml", "example.dist-info/licenses/LICENSE"]:
            archive.writestr(name, "example")
        if unexpected:
            archive.writestr(unexpected, "unexpected")
    with tarfile.open(tmp_path / "example-1.0.tar.gz", "w:gz") as archive:
        entry = tarfile.TarInfo("example-1.0/LICENSE")
        entry.size = 7
        archive.addfile(entry, io.BytesIO(b"license"))
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(venv.EnvBuilder, "create", lambda self, path: None)
    if unexpected:
        with pytest.raises(AssertionError):
            tool.verify_package(tmp_path)
        assert not calls
    else:
        tool.verify_package(tmp_path)
        assert sum("install" in args for args in calls) == 2
        assert sum("init-config" in args for args in calls) == 2
        assert sum("validate" in args for args in calls) == 2
