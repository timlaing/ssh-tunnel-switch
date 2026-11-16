"""Attach verified release files, accepting existing assets only when identical."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def attach_assets(distribution: Path, tag: str) -> None:
    """Upload new assets and reject different content under an existing name."""
    executable = shutil.which("gh")
    if executable is None:
        message = "GitHub CLI executable was not found"
        raise FileNotFoundError(message)
    release = json.loads(
        subprocess.check_output(
            [executable, "release", "view", tag, "--json", "assets"],
            text=True,
        ),
    )
    existing = {asset["name"] for asset in release["assets"]}
    for file in sorted(distribution.iterdir()):
        if not (file.name.endswith((".whl", ".tar.gz")) or file.name == "SHA256SUMS"):
            continue
        if file.name in existing:
            with tempfile.TemporaryDirectory() as temporary:
                subprocess.run(
                    [
                        executable,
                        "release",
                        "download",
                        tag,
                        "--pattern",
                        file.name,
                        "--dir",
                        temporary,
                    ],
                    check=True,
                )
                current = Path(temporary) / file.name
                current_digest = hashlib.sha256(current.read_bytes()).digest()
                if current_digest != hashlib.sha256(file.read_bytes()).digest():
                    message = f"Existing release asset differs: {file.name}"
                    raise RuntimeError(message)
            sys.stdout.write(f"Existing release asset matches: {file.name}\n")
        else:
            subprocess.run(
                [executable, "release", "upload", tag, str(file)],
                check=True,
            )


if __name__ == "__main__":
    attach_assets(
        Path(__file__).resolve().parents[1] / "dist",
        os.environ["RELEASE_TAG"],
    )
