from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
from typing import Any

if os.name == "nt":  # pragma: no cover - exercised in Windows runtime
    import ctypes
    from ctypes import wintypes


DRIVE_TYPE_NAMES = {
    0: "unknown",
    1: "no_root_dir",
    2: "removable",
    3: "fixed",
    4: "remote",
    5: "cdrom",
    6: "ramdisk",
}


@dataclass(slots=True)
class DriveInfo:
    root: str
    drive_type: str
    drive_type_code: int
    volume_name: str | None
    filesystem: str | None
    total_bytes: int | None
    free_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "drive_type": self.drive_type,
            "drive_type_code": self.drive_type_code,
            "volume_name": self.volume_name,
            "filesystem": self.filesystem,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
        }


def list_host_drives() -> list[DriveInfo]:
    if os.name != "nt":
        return []

    drives: list[DriveInfo] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for index in range(26):
        if not (bitmask & (1 << index)):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        drives.append(get_drive_info(root))
    return drives


def get_drive_info(path: str | Path) -> DriveInfo:
    root = _normalize_drive_root(path)
    if os.name != "nt":
        resolved = Path(root)
        total_bytes: int | None = None
        free_bytes: int | None = None
        if resolved.exists():
            usage = shutil.disk_usage(resolved)
            total_bytes = usage.total
            free_bytes = usage.free
        return DriveInfo(
            root=str(resolved),
            drive_type="unknown",
            drive_type_code=0,
            volume_name=None,
            filesystem=None,
            total_bytes=total_bytes,
            free_bytes=free_bytes,
        )

    drive_type_code = ctypes.windll.kernel32.GetDriveTypeW(root)
    drive_type = DRIVE_TYPE_NAMES.get(drive_type_code, "unknown")
    volume_name, filesystem = _get_volume_information(root)
    total_bytes: int | None = None
    free_bytes: int | None = None
    try:
        usage = shutil.disk_usage(root)
    except OSError:
        pass
    else:
        total_bytes = usage.total
        free_bytes = usage.free
    return DriveInfo(
        root=root,
        drive_type=drive_type,
        drive_type_code=drive_type_code,
        volume_name=volume_name,
        filesystem=filesystem,
        total_bytes=total_bytes,
        free_bytes=free_bytes,
    )


def is_probable_sd_drive(path: str | Path) -> bool:
    return get_drive_info(path).drive_type == "removable"


def _normalize_drive_root(path: str | Path) -> str:
    raw = str(path)
    if os.name != "nt":
        return raw
    drive = os.path.splitdrive(raw)[0]
    if not drive:
        drive = raw.rstrip("\\/")
    if len(drive) == 1 and drive.isalpha():
        drive += ":"
    return drive.rstrip("\\/") + "\\"


def _get_volume_information(root: str) -> tuple[str | None, str | None]:
    if os.name != "nt":
        return None, None
    volume_buffer = ctypes.create_unicode_buffer(261)
    filesystem_buffer = ctypes.create_unicode_buffer(261)
    serial_number = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    success = ctypes.windll.kernel32.GetVolumeInformationW(
        wintypes.LPCWSTR(root),
        volume_buffer,
        len(volume_buffer),
        ctypes.byref(serial_number),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem_buffer,
        len(filesystem_buffer),
    )
    if not success:
        return None, None
    volume_name = volume_buffer.value or None
    filesystem = filesystem_buffer.value or None
    return volume_name, filesystem
