from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


log = logging.getLogger(__name__)

MetadataMutator = Callable[[dict], dict | None]
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def metadata_write_lock(path: Path | str, *, timeout: float = 10.0):
    """Serialize a larger transaction with all writers of one metadata file."""
    with _metadata_write_lock(Path(path), timeout):
        yield


def read_metadata(path: Path | str, backup_path: Path | str | None = None) -> dict:
    """Read a metadata object, falling back to the last known-good copy."""
    target = Path(path)
    fallback = Path(backup_path) if backup_path is not None else _default_backup(target)
    return _read_unlocked(target, fallback)


def write_metadata(
    path: Path | str,
    data: dict,
    backup_path: Path | str | None = None,
    *,
    lock_timeout: float = 10.0,
) -> None:
    """Atomically replace a metadata object while serializing writers."""
    if not isinstance(data, dict):
        raise TypeError("metadata must be a dictionary")
    target = Path(path)
    fallback = Path(backup_path) if backup_path is not None else _default_backup(target)
    with metadata_write_lock(target, timeout=lock_timeout):
        _write_unlocked(target, copy.deepcopy(data), fallback)


def update_metadata(
    path: Path | str,
    mutator: MetadataMutator,
    backup_path: Path | str | None = None,
    *,
    lock_timeout: float = 10.0,
) -> dict:
    """Apply one read-modify-write update without losing concurrent changes."""
    target = Path(path)
    fallback = Path(backup_path) if backup_path is not None else _default_backup(target)
    with metadata_write_lock(target, timeout=lock_timeout):
        working = copy.deepcopy(_read_unlocked(target, fallback))
        replacement = mutator(working)
        if replacement is not None:
            working = replacement
        if not isinstance(working, dict):
            raise TypeError("metadata mutator must return a dictionary or None")
        _write_unlocked(target, working, fallback)
        return copy.deepcopy(working)


def _default_backup(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak")


def _read_unlocked(path: Path, backup_path: Path) -> dict:
    for candidate in (path, backup_path):
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _read_valid_primary(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _write_unlocked(path: Path, data: dict, backup_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_valid_primary(path)
    if previous is not None:
        try:
            _replace_json(backup_path, previous)
        except OSError:
            log.warning("Failed to update metadata backup %s", backup_path, exc_info=True)

    _replace_json(path, data)

    if previous is None and not backup_path.exists():
        try:
            _replace_json(backup_path, data)
        except OSError:
            log.warning("Failed to create metadata backup %s", backup_path, exc_info=True)


def _replace_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _metadata_write_lock(path: Path, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    local_lock = _thread_lock(path)
    if not local_lock.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"Timed out waiting for metadata lock: {path}")
    handle = None
    locked = False
    lock_path = path.with_name(f".{path.name}.lock")
    try:
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _try_lock_file(handle)
                locked = True
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for metadata lock: {path}")
                time.sleep(0.05)
        yield
    finally:
        if handle is not None:
            if locked:
                try:
                    _unlock_file(handle)
                except OSError:
                    pass
            handle.close()
        local_lock.release()


def _try_lock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
