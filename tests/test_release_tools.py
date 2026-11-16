"""Exercise release asset retry behavior without contacting GitHub."""

import hashlib
import importlib.util
import io
import json
import subprocess
import tarfile
import urllib.request
import venv
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def load_tool(filename: str) -> ModuleType:
    """Load a standalone release tool without invoking its command-line entrypoint."""
    source = Path("scripts", filename).resolve()
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), source)
    if spec is None or spec.loader is None:
        pytest.fail(f"Unable to load release tool: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str | Path]]:
    """Replace subprocess execution and environment creation with inert recorders."""
    calls: list[list[str | Path]] = []

    def run(arguments: list[str | Path], **_kwargs: object) -> None:
        calls.append(arguments)

    def create(_self: venv.EnvBuilder, _path: str | Path) -> None:
        pass

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(venv.EnvBuilder, "create", create)
    return calls


@pytest.mark.parametrize("existing", [None, b"verified", b"different"])
def test_release_asset_upload_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bytes | None,
) -> None:
    """Upload missing assets, reuse identical assets, and reject different contents."""
    tool = load_tool("attach-release.py")
    monkeypatch.setattr(tool.shutil, "which", lambda _name: "/trusted/bin/gh")
    distribution = tmp_path / "dist"
    distribution.mkdir()
    (distribution / "server.whl").write_bytes(b"verified")
    monkeypatch.setenv("RELEASE_TAG", "service-v0.1.1")
    calls: list[list[str | Path]] = []
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *_args, **_kwargs: json.dumps(
            {
                "assets": [] if existing is None else [{"name": "server.whl"}],
            },
        ),
    )

    def command(arguments: list[str | Path], **_kwargs: object) -> None:
        calls.append(arguments)
        if arguments[2] == "download":
            if existing is None:
                pytest.fail("Release tool downloaded an asset that does not exist")
            Path(arguments[arguments.index("--dir") + 1], "server.whl").write_bytes(
                existing,
            )

    monkeypatch.setattr(subprocess, "run", command)
    if existing == b"different":
        with pytest.raises(RuntimeError, match="Existing release asset differs"):
            tool.attach_assets(distribution, "service-v0.1.1")
    else:
        tool.attach_assets(distribution, "service-v0.1.1")
    uploads = [call for call in calls if call[2] == "upload"]
    assert len(uploads) == (1 if existing is None else 0), (
        "Release retry uploaded an unexpected number of assets"
    )


@pytest.mark.parametrize("valid_hash", [True, False])
def test_registry_verification_checks_integrity_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid_hash: bool,
) -> None:
    """Check registry hashes before installing the exact version without dependencies."""
    tool = load_tool("verify-registry.py")
    distribution = tmp_path / "dist"
    distribution.mkdir()
    wheel = distribution / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "example-1.0.dist-info/METADATA",
            "Name: example\nVersion: 1.0\n",
        )
    digest = (
        hashlib.sha256(wheel.read_bytes()).hexdigest() if valid_hash else "different"
    )
    payload = {
        "info": {"version": "1.0"},
        "urls": [{"filename": wheel.name, "digests": {"sha256": digest}}],
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    calls = record_commands(monkeypatch)

    if not valid_hash:
        with pytest.raises(AssertionError):
            tool.verify_registry("testpypi", distribution)
        assert not calls, "Hash mismatch must prevent all subprocess execution"
    else:
        tool.verify_registry("testpypi", distribution)
        registry_installs = [
            args for args in calls if "https://test.pypi.org/simple/" in args
        ]
        assert len(registry_installs) == 1, (
            "Expected one install from the selected registry"
        )
        assert "--no-deps" in registry_installs[0], (
            "Registry install must not mix dependency indexes"
        )
        assert "example==1.0" in registry_installs[0], (
            "Registry install must pin the verified version"
        )
        assert any("check" in args for args in calls), (
            "Installed dependencies were not checked"
        )
        assert any("init-config" in args for args in calls), (
            "Installed CLI config creation was not tested"
        )
        assert any("validate" in args for args in calls), (
            "Installed CLI config validation was not tested"
        )


@pytest.mark.parametrize("unexpected", [None, "plugin/index.ts", "config.yaml"])
def test_package_verification_rejects_unexpected_wheel_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unexpected: str | None,
) -> None:
    """Reject forbidden wheel files before running isolated wheel and sdist checks."""
    tool = load_tool("verify-package.py")
    wheel = tmp_path / "example-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in [
            "tunnel_switch/cli.py",
            "tunnel_switch/example.yaml",
            "example.dist-info/licenses/LICENSE",
        ]:
            archive.writestr(name, "example")
        if unexpected:
            archive.writestr(unexpected, "unexpected")
    with tarfile.open(tmp_path / "example-1.0.tar.gz", "w:gz") as archive:
        entry = tarfile.TarInfo("example-1.0/LICENSE")
        entry.size = 7
        archive.addfile(entry, io.BytesIO(b"license"))
    calls = record_commands(monkeypatch)
    if unexpected:
        with pytest.raises(AssertionError):
            tool.verify_package(tmp_path)
        assert not calls, "Unexpected wheel contents must prevent subprocess execution"
    else:
        tool.verify_package(tmp_path)
        artifact_count = 2
        assert sum("install" in args for args in calls) == artifact_count, (
            "Both wheel and sdist must be installed"
        )
        assert sum("init-config" in args for args in calls) == artifact_count, (
            "Both installed artifacts must create config"
        )
        assert sum("validate" in args for args in calls) == artifact_count, (
            "Both installed artifacts must validate config"
        )
