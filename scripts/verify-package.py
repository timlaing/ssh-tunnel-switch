"""Check and install standalone wheel and sdist in separate environments."""
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

root = Path(__file__).resolve().parents[1]
wheel, = (root / "dist").glob("*.whl")
sdist, = (root / "dist").glob("*.tar.gz")
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert any(name.endswith("/licenses/LICENSE") for name in names)
    assert "tunnel_switch/cli.py" in names
    assert not any("plugin/" in name or name.endswith("config.yaml") for name in names)
with tarfile.open(sdist) as archive:
    names = archive.getnames()
    assert any(name.endswith("/LICENSE") for name in names)
    assert not any("/plugin/" in name for name in names)
for artifact in (wheel, sdist):
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment = directory / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        executable = environment / "bin/python"
        subprocess.run([executable, "-m", "pip", "install", str(artifact)], check=True, cwd=directory)
        subprocess.run([executable, "-c", "import tunnel_switch"], check=True, cwd=directory)
        cli = environment / "bin/tunnel-switch"
        subprocess.run([cli, "--help"], check=True, cwd=directory)
        subprocess.run([cli, "validate", "--config", str(root / "examples/config.yaml")], check=True, cwd=directory)
