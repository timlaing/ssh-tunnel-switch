"""Timestamped service logging: a private rotating file, plus console when headless."""

import logging
import os
import time
from logging.handlers import RotatingFileHandler

from .config import Logging

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 3
FILE_MODE = 0o600


class UTCFormatter(logging.Formatter):
    """Render record times as ISO 8601 UTC, matching the API's timestamps."""

    converter = time.gmtime


class PrivateFileHandler(RotatingFileHandler):
    """Create the log privately (mode 0600), including after rollover."""

    def _open(self):
        directory = os.path.dirname(self.baseFilename)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        descriptor = os.open(
            self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE
        )
        os.chmod(self.baseFilename, FILE_MODE)
        return os.fdopen(
            descriptor, self.mode, encoding=self.encoding, errors="replace"
        )


def configure_logging(settings: Logging, console: bool):
    """Send timestamped records to the log file and, when console is true, stderr.

    Returns the configured log file, or None when file logging is disabled.
    The file is opened lazily, so an idle service never creates it.
    """
    formatter = UTCFormatter(FORMAT, DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(settings.level)
    for handler in [h for h in root.handlers if getattr(h, "_tunnel_switch", False)]:
        root.removeHandler(handler)
        handler.close()
    if settings.file is not None:
        file_handler = PrivateFileHandler(
            settings.file,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(formatter)
        file_handler._tunnel_switch = True
        root.addHandler(file_handler)
    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        stream_handler._tunnel_switch = True
        root.addHandler(stream_handler)
    return settings.file
