"""SSH SOCKS tunnel rotation service."""

from importlib.metadata import PackageNotFoundError, version

PACKAGE_NAME = "ssh-tunnel-switch"


def _package_version() -> str:
    """The installed distribution version, or a marker in a bare source tree."""
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _package_version()
