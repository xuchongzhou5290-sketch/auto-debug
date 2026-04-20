from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
from pathlib import Path
import tarfile


@dataclass(slots=True)
class SerialBundle:
    bundle_path: Path
    sha256: str
    size_bytes: int
    artifact_count: int
    relative_paths: list[str]

    def read_base64_text(self) -> str:
        return base64.b64encode(self.bundle_path.read_bytes()).decode("ascii")

    def to_dict(self) -> dict[str, object]:
        return {
            "bundle_path": str(self.bundle_path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "artifact_count": self.artifact_count,
            "relative_paths": list(self.relative_paths),
        }


def build_serial_bundle(
    root: Path,
    *,
    output_path: Path,
    exclude_names: tuple[str, ...] = (),
) -> SerialBundle:
    root = root.resolve()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    excluded = set(exclude_names)
    relative_paths: list[str] = []
    with tarfile.open(output_path, mode="w") as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name in excluded:
                continue
            if path.resolve() == output_path:
                continue
            relative_path = path.relative_to(root).as_posix()
            archive.add(path, arcname=relative_path)
            relative_paths.append(relative_path)

    return SerialBundle(
        bundle_path=output_path,
        sha256=_sha256_file(output_path),
        size_bytes=output_path.stat().st_size,
        artifact_count=len(relative_paths),
        relative_paths=relative_paths,
    )


def split_base64_payload(payload: str, *, chunk_size: int) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    return [payload[index : index + chunk_size] for index in range(0, len(payload), chunk_size)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
