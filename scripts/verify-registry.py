"""Verify registry hashes and install the exact published version."""

import argparse
import email
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
import zipfile
from pathlib import Path

REGISTRY_ATTEMPTS = 6


def verify_registry(registry: str, distribution: Path) -> None:
    """Check published hashes, then install from the selected registry."""
    (wheel,) = distribution.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        (member,) = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        metadata = email.message_from_bytes(archive.read(member))
    name, version = metadata["Name"], metadata["Version"]
    host = "pypi.org" if registry == "pypi" else "test.pypi.org"
    for attempt in range(REGISTRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(
                f"https://{host}/pypi/{name}/{version}/json",
                timeout=30,
            ) as response:
                published = json.load(response)
            break
        except urllib.error.URLError:
            if attempt == REGISTRY_ATTEMPTS - 1:
                raise
            time.sleep(10)
    if published["info"]["version"] != version:
        message = f"Registry version does not match {version}"
        raise AssertionError(message)
    artifacts = {item["filename"]: item for item in published["urls"]}
    for file in distribution.iterdir():
        if file.suffix not in {".whl", ".gz"}:
            continue
        actual_digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if actual_digest != artifacts[file.name]["digests"]["sha256"]:
            message = f"Registry SHA256 does not match: {file.name}"
            raise AssertionError(message)
        sys.stdout.write(f"Verified registry SHA256: {file.name}\n")
        sys.stdout.flush()
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(directory / "venv")
        python = directory / "venv/bin/python"
        # Resolve constrained dependencies against production PyPI, never a mixed index.
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--index-url",
                "https://pypi.org/simple/",
                str(wheel),
            ],
            check=True,
            cwd=directory,
        )
        subprocess.run(
            [
                python,
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                "--force-reinstall",
                "--no-deps",
                "--index-url",
                f"https://{host}/simple/",
                f"{name}=={version}",
            ],
            check=True,
            cwd=directory,
        )
        subprocess.run([python, "-m", "pip", "check"], check=True, cwd=directory)
        cli = directory / "venv/bin/tunnel-switch"
        subprocess.run([cli, "--help"], check=True, cwd=directory)
        config = directory / "config.yaml"
        subprocess.run(
            [cli, "init-config", "--output", str(config)],
            check=True,
            cwd=directory,
        )
        subprocess.run(
            [cli, "validate", "--config", str(config)],
            check=True,
            cwd=directory,
        )
    sys.stdout.write(f"Verified {name}=={version} from {host}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", choices=("pypi", "testpypi"), required=True)
    args = parser.parse_args()
    verify_registry(args.registry, Path(__file__).resolve().parents[1] / "dist")
