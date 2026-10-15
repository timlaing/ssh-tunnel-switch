"""Exercise release asset retry behavior without contacting GitHub."""
import json
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize("existing", [None, b"verified", b"different"])
def test_release_asset_upload_is_idempotent(tmp_path, monkeypatch, existing):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = Path("scripts/attach-release.py").resolve()

    def execute():
        exec(compile(source.read_text(), str(source), "exec"), {"__file__": str(scripts / "attach-release.py"), "__name__": "__main__"})
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
            execute()
    else:
        execute()
    uploads = [call for call in calls if call[2] == "upload"]
    assert len(uploads) == (1 if existing is None else 0)


@pytest.mark.parametrize("valid_hash", [True, False])
def test_registry_verification_checks_integrity_before_install(tmp_path, monkeypatch, valid_hash):
    import hashlib
    import io
    import sys
    import urllib.request
    import venv
    import zipfile

    source = Path("scripts/verify-registry.py").resolve()
    distribution = tmp_path / "dist"
    distribution.mkdir()
    wheel = distribution / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("example-1.0.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest() if valid_hash else "different"
    payload = {"info": {"version": "1.0"}, "urls": [{"filename": wheel.name, "digests": {"sha256": digest}}]}
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(sys, "argv", ["verify-registry.py", "--registry", "testpypi"])
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(venv.EnvBuilder, "create", lambda self, path: None)

    def execute():
        exec(compile(source.read_text(), str(source), "exec"), {"__file__": str(tmp_path / "scripts/verify-registry.py"), "__name__": "__main__"})

    if not valid_hash:
        with pytest.raises(AssertionError):
            execute()
        assert not calls
    else:
        execute()
        registry_installs = [args for args in calls if "https://test.pypi.org/simple/" in args]
        assert len(registry_installs) == 1
        assert "--no-deps" in registry_installs[0]
        assert "example==1.0" in registry_installs[0]
        assert any("check" in args for args in calls)
        assert any("init-config" in args for args in calls)
        assert any("validate" in args for args in calls)
