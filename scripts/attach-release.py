"""Attach verified release files, accepting existing assets only when identical."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

tag = os.environ["RELEASE_TAG"]
root = Path(__file__).resolve().parents[1]
release = json.loads(subprocess.check_output(["gh", "release", "view", tag, "--json", "assets"], text=True))
existing = {asset["name"] for asset in release["assets"]}
for file in sorted((root / "dist").iterdir()):
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
