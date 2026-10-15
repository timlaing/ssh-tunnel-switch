"""Exercise release asset retry behavior without contacting GitHub."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("existing", [None, b"verified", b"different"])
def test_release_asset_upload_is_idempotent(tmp_path, monkeypatch, existing):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(Path("scripts/attach-release.py"), scripts / "attach-release.py")
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
            runpy.run_path(str(scripts / "attach-release.py"))
    else:
        runpy.run_path(str(scripts / "attach-release.py"))
    uploads = [call for call in calls if call[2] == "upload"]
    assert len(uploads) == (1 if existing is None else 0)
