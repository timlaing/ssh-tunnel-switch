"""Versioned, atomically replaced state identifying the last successful SSH server."""
import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from .config import Server, StrictModel


class LastConnection(StrictModel):
    version: Literal[1] = 1
    server: str
    host: str


def load_last_server(path: Path, servers: list[Server]) -> int | None:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read tunnel state file: {path}") from exc
    try:
        saved = LastConnection.model_validate_json(contents)
    except ValueError as exc:
        raise ValueError(f"Invalid tunnel state file: {path}; move it aside or select another state file") from exc
    for index, server in enumerate(servers):
        if (server.name, server.host) == (saved.server, saved.host):
            return index
    return None


def save_last_server(path: Path, server: Server):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(LastConnection(server=server.name, host=server.host).model_dump(), stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Persist the renamed directory entry as well as the file contents.
        # Windows does not support opening directory handles with os.open.
        try:
            sync_directory(path.parent)
        except OSError as exc:
            # Replacement is already visible. Restore the prior selection before
            # rotate() treats this as a failed connection and stops the tunnel.
            try:
                if previous is None:
                    path.unlink()
                else:
                    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                                     prefix=f".{path.name}.", delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(previous)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                sync_directory(path.parent)
            except OSError as rollback_error:
                exc.add_note(f"State rollback or its directory sync failed: {rollback_error}")
            raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def sync_directory(path: Path):
    if os.name == "nt":
        return
    directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
