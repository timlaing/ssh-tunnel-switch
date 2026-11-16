"""Check and install standalone wheel and sdist in separate environments."""

import subprocess
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path


def _require(*, condition: bool, message: str) -> None:
    """Reject invalid release artifacts even when Python runs with optimization."""
    if not condition:
        raise AssertionError(message)


def verify_package(distribution: Path) -> None:
    """Check archive boundaries and install wheel/sdist in fresh environments."""
    (wheel,) = distribution.glob("*.whl")
    (sdist,) = distribution.glob("*.tar.gz")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        _require(
            condition=any(name.endswith("/licenses/LICENSE") for name in names),
            message="Wheel is missing its license",
        )
        _require(
            condition="tunnel_switch/cli.py" in names,
            message="Wheel is missing the CLI",
        )
        _require(
            condition="tunnel_switch/example.yaml" in names,
            message="Wheel is missing the example configuration",
        )
        _require(
            condition=not any(
                "plugin/" in name or name.endswith("config.yaml") for name in names
            ),
            message="Wheel contains a plugin or live configuration",
        )
    with tarfile.open(sdist) as archive:
        names = archive.getnames()
        _require(
            condition=any(name.endswith("/LICENSE") for name in names),
            message="Source distribution is missing its license",
        )
        _require(
            condition=not any("/plugin/" in name for name in names),
            message="Source distribution contains a plugin",
        )
    for artifact in (wheel, sdist):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment = directory / "venv"
            venv.EnvBuilder(with_pip=True).create(environment)
            executable = environment / "bin/python"
            subprocess.run(
                [executable, "-m", "pip", "install", str(artifact)],
                check=True,
                cwd=directory,
            )
            subprocess.run(
                [executable, "-c", "import tunnel_switch"],
                check=True,
                cwd=directory,
            )
            cli = environment / "bin/tunnel-switch"
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


if __name__ == "__main__":
    verify_package(Path(__file__).resolve().parents[1] / "dist")
