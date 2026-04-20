from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SessionPaths:
    root: Path
    core_dir: Path
    deploy_dir: Path
    logs_dir: Path
    retrieved_dir: Path


@dataclass(slots=True)
class SessionContext:
    session_id: str
    created_at: str
    device_id: str
    task_type: str
    session_paths: SessionPaths
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        session_id: str,
        device_id: str,
        task_type: str,
        session_paths: SessionPaths,
    ) -> "SessionContext":
        return cls(
            session_id=session_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            device_id=device_id,
            task_type=task_type,
            session_paths=session_paths,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["session_paths"] = {
            "root": str(self.session_paths.root),
            "core_dir": str(self.session_paths.core_dir),
            "deploy_dir": str(self.session_paths.deploy_dir),
            "logs_dir": str(self.session_paths.logs_dir),
            "retrieved_dir": str(self.session_paths.retrieved_dir),
        }
        return payload
