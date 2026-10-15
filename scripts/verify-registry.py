"""Verify registry hashes and install the exact published version."""
import argparse
import email
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import venv
import zipfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--registry", choices=("pypi", "testpypi"), required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
wheel, = (root / "dist").glob("*.whl")
with zipfile.ZipFile(wheel) as archive:
    member, = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
    metadata = email.message_from_bytes(archive.read(member))
name, version = metadata["Name"], metadata["Version"]
host = "pypi.org" if args.registry == "pypi" else "test.pypi.org"
for attempt in range(6):
    try:
        with urllib.request.urlopen(f"https://{host}/pypi/{name}/{version}/json", timeout=30) as response:
            published = json.load(response)
        break
    except urllib.error.URLError:
        if attempt == 5:
            raise
        time.sleep(10)
assert published["info"]["version"] == version
artifacts = {item["filename"]: item for item in published["urls"]}
for file in (root / "dist").iterdir():
    if file.suffix not in {".whl", ".gz"}:
        continue
    assert hashlib.sha256(file.read_bytes()).hexdigest() == artifacts[file.name]["digests"]["sha256"], file.name
    print(f"Verified registry SHA256: {file.name}", flush=True)
with tempfile.TemporaryDirectory() as temporary:
    directory = Path(temporary)
    venv.EnvBuilder(with_pip=True).create(directory / "venv")
    python = directory / "venv/bin/python"
    # Resolve constrained dependencies against production PyPI, never a mixed index.
    subprocess.run([python, "-m", "pip", "install", "--index-url", "https://pypi.org/simple/", str(wheel)], check=True, cwd=directory)
    subprocess.run([python, "-m", "pip", "install", "--no-cache-dir", "--force-reinstall", "--no-deps", "--index-url", f"https://{host}/simple/", f"{name}=={version}"], check=True, cwd=directory)
    subprocess.run([python, "-m", "pip", "check"], check=True, cwd=directory)
    cli = directory / "venv/bin/tunnel-switch"
    subprocess.run([cli, "--help"], check=True, cwd=directory)
    config = directory / "config.yaml"
    subprocess.run([cli, "init-config", "--output", str(config)], check=True, cwd=directory)
    subprocess.run([cli, "validate", "--config", str(config)], check=True, cwd=directory)
print(f"Verified {name}=={version} from {host}")
