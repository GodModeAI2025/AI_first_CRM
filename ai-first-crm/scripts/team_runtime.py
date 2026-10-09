#!/usr/bin/env python3
"""Private, per-operation process guards and ownership-aware cleanup support."""
from __future__ import annotations
import functools, hashlib, json, os, socket
from pathlib import Path
from typing import Any
from team_contract import TeamError, digest


def inventory(target: Path) -> str:
    files = {}
    for path in sorted(target.rglob("*")):
        parts = path.relative_to(target).parts
        if (
            parts[0] == ".git"
            or parts[0] == ".llmwiki.lock"
            or path.name in {".DS_Store", "Thumbs.db"}
        ):
            continue
        if path.is_symlink():
            raise TeamError("unsafe_runtime")
        if path.is_file():
            files[path.relative_to(target).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digest(files)


def guarded(function: Any) -> Any:
    @functools.wraps(function)
    def call(session_file: Path, *args: Any, **kwargs: Any) -> Any:
        root = session_file.parent.resolve()
        lock = root / "session.lock"
        if (
            session_file.is_symlink()
            or not session_file.is_file()
            or session_file.stat().st_mode & 0o077
        ):
            raise TeamError("unsafe_runtime")
        state = json.loads(session_file.read_text())
        stat = root.stat()
        if (
            state.get("root_identity") != [stat.st_dev, stat.st_ino]
            or stat.st_mode & 0o077
        ):
            raise TeamError("unsafe_runtime")
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise TeamError(
                "session_busy",
                "Another process owns this private session. If it ended, use recover; never force a live process.",
            ) from exc
        identity = os.fstat(fd)
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "uid": os.geteuid() if hasattr(os, "geteuid") else None,
        }
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            return function(session_file, *args, **kwargs)
        finally:
            if lock.exists():
                current = lock.stat()
                if (current.st_dev, current.st_ino) == (
                    identity.st_dev,
                    identity.st_ino,
                ):
                    lock.unlink()

    return call


def clear_dead_guard(session_file: Path) -> None:
    root = session_file.parent.resolve()
    lock = root / "session.lock"
    if not lock.exists():
        return
    if lock.is_symlink():
        raise TeamError("unsafe_runtime")
    identity = lock.stat()
    data = json.loads(lock.read_text())
    if data.get("host") != socket.gethostname() or (
        hasattr(os, "geteuid")
        and (identity.st_uid != os.geteuid() or data.get("uid") != os.geteuid())
    ):
        raise TeamError(
            "session_busy", "The session guard belongs to another machine or OS user."
        )
    pid = data.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        raise TeamError("session_busy")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise TeamError("session_busy") from exc
    else:
        raise TeamError("session_busy", "The session process is still alive.")
    current = lock.stat()
    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
        raise TeamError("session_busy")
    lock.unlink()
