from __future__ import annotations

from dataclasses import dataclass
import hashlib
import shutil
from pathlib import Path

from autodbg.host.storage import get_drive_info
from autodbg.models.profile import TaskProfile, TransportProfile


@dataclass(slots=True)
class DeployPlan:
    strategy: str
    channels: list[str]


@dataclass(slots=True)
class DeploymentResult:
    source_path: str
    target_path: str
    size_bytes: int
    sha256: str
    verified: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "source_path": self.source_path,
            "target_path": self.target_path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "verified": self.verified,
        }


class Deployer:
    def __init__(self, task_profile: TaskProfile, transport_profile: TransportProfile) -> None:
        self.task_profile = task_profile
        self.transport_profile = transport_profile

    def plan(self) -> DeployPlan:
        return DeployPlan(
            strategy=self.task_profile.deploy_strategy,
            channels=list(self.transport_profile.deploy_channels),
        )

    def stage_to_sd(
        self,
        *,
        source_path: Path,
        sdcard_drive: str,
        target_subdir: str = r"debug\autodbg",
        destination_name: str | None = None,
        verify: bool = True,
        allow_non_removable: bool = False,
    ) -> DeploymentResult:
        if not source_path.is_file():
            raise FileNotFoundError(f"Source file does not exist: {source_path}")

        drive_root = Path(sdcard_drive)
        if not drive_root.exists():
            raise FileNotFoundError(f"Configured SD card drive is not available: {drive_root}")

        drive_info = get_drive_info(sdcard_drive)
        if drive_info.drive_type != "removable" and not allow_non_removable:
            raise RuntimeError(
                f"Refusing to stage onto non-removable drive {drive_info.root} "
                f"(type={drive_info.drive_type}). Use an actual SD/removable drive or explicitly override."
            )

        target_dir = drive_root / Path(target_subdir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / (destination_name or source_path.name)
        shutil.copy2(source_path, target_path)

        source_hash = _sha256_file(source_path)
        verified = True
        if verify:
            verified = source_hash == _sha256_file(target_path)
            if not verified:
                raise RuntimeError(f"SHA256 mismatch after staging file to SD card: {target_path}")

        return DeploymentResult(
            source_path=str(source_path),
            target_path=str(target_path),
            size_bytes=source_path.stat().st_size,
            sha256=source_hash,
            verified=verified,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
