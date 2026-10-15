"""Attach verified release files, accepting existing assets only when identical."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def attach_assets(distribution: Path, tag: str):
    """Upload new assets and reject different content under an existing name."""
    release = json.loads(subprocess.check_output(["gh", "release", "view", tag, "--json", "assets"], text=True))
    existing = {asset["name"] for asset in release["assets"]}
    for file in sorted(distribution.iterdir()):
        if not (file.name.endswith((".whl", ".tar.gz")) or file.name == "SHA256SUMS"):
            continue
        if file.name in existing:
            with tempfile.TemporaryDirectory() as temporary:
                subprocess.run(["gh", "release", "download", tag, "--pattern", file.name, "--dir", temporary], check=True)
                current = Path(temporary) / file.name
                if hashlib.sha256(current.read_bytes()).digest() != hashlib.sha256(file.read_bytes()).digest():
                    raise RuntimeError(f"Existing release asset differs: {file.name}")
            print(f"Existing release asset matches: {file.name}")
        else:
            subprocess.run(["gh", "release", "upload", tag, str(file)], check=True)


if __name__ == "__main__":
    attach_assets(Path(__file__).resolve().parents[1] / "dist", os.environ["RELEASE_TAG"])
