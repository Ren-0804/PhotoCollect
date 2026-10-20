"""Stable, privacy-preserving Windows identity for silent authorization.

The server never needs the raw Windows SID or hardware values.  We keep one
random installation id locally and hash the values before they leave the
machine.  The resulting device id is an identifier, not a secret.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import socket
import string
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _normalise(value: str) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_machine_guid() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except (OSError, ImportError):
        return ""


def _read_volume_serial() -> str:
    """Read the system volume serial without invoking a shell process."""
    root = (os.getenv("SystemDrive") or "C:").rstrip("\\/") + "\\"
    try:
        serial = ctypes.c_uint32()
        max_component = ctypes.c_uint32()
        flags = ctypes.c_uint32()
        filesystem = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            None,
            0,
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem,
            len(filesystem),
        )
        return f"{serial.value:08x}" if ok else ""
    except (AttributeError, OSError):
        return ""


def get_current_user_sid() -> str:
    """Return the current Windows SID, or an empty string off Windows."""
    try:
        import win32api
        import win32con
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32con.TOKEN_QUERY,
        )
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            return win32security.ConvertSidToStringSid(sid)
        finally:
            token.Close()
    except (AttributeError, ImportError, OSError):
        return ""


def _load_install_id(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="ascii").strip()
        if len(value) == 32 and all(ch in string.hexdigits for ch in value):
            return value.lower()
    except OSError:
        pass
    value = secrets.token_hex(16)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(value, encoding="ascii")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return value


@dataclass(frozen=True)
class ClientIdentity:
    computer_name: str
    username: str
    user_sid_hash: str
    device_id: str
    fingerprint_version: str = "v1"

    def as_request(self, app_version: str) -> dict[str, str]:
        return {
            "computer_name": self.computer_name,
            "username": self.username,
            "user_sid_hash": self.user_sid_hash,
            "device_id": self.device_id,
            "fingerprint_version": self.fingerprint_version,
            "app_version": str(app_version),
        }


def collect_identity(storage_dir: Path | None = None) -> ClientIdentity:
    storage_dir = Path(storage_dir or (Path(os.getenv("APPDATA") or Path.home()) / "container_picker"))
    install_id = _load_install_id(storage_dir / ".install-id")
    computer_name = socket.gethostname()
    username = os.getenv("USERNAME") or os.getenv("USER") or "unknown"
    sid = get_current_user_sid()
    machine_guid = _read_machine_guid()
    volume_serial = _read_volume_serial()
    material = "|".join(
        (
            "photocollect-device-v1",
            _normalise(machine_guid),
            _normalise(volume_serial),
            _normalise(install_id),
        )
    )
    return ClientIdentity(
        computer_name=computer_name[:128],
        username=username[:128],
        user_sid_hash=_sha256("photocollect-sid-v1|" + _normalise(sid)),
        device_id=_sha256(material),
    )


def identity_to_dict(identity: ClientIdentity) -> dict[str, Any]:
    return asdict(identity)
