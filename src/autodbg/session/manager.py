from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from uuid import uuid4

from autodbg.models.session import SessionContext, SessionPaths


def _slugify(raw: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-").lower()


class SessionManager:
    def __init__(self, artifacts_root: Path, retrieved_root: Path | None = None) -> None:
        self.artifacts_root = artifacts_root
        if retrieved_root is None:
            if artifacts_root.name.lower() == "artifacts":
                retrieved_root = artifacts_root.parent / "retrieved"
            else:
                retrieved_root = artifacts_root / "retrieved"
        self.retrieved_root = retrieved_root

    def create(self, device_id: str, task_type: str) -> SessionContext:
        now = datetime.now()
        day_dir = self.artifacts_root / now.strftime("%Y%m%d")
        retrieved_day_dir = self.retrieved_root / now.strftime("%Y%m%d")
        session_id = (
            f"{now.strftime('%H%M%S')}"
            f"{now.strftime('%f')[:3]}-"
            f"{_slugify(device_id)}-"
            f"{_slugify(task_type)}-"
            f"{uuid4().hex[:4]}"
        )
        root = day_dir / session_id
        retrieved_root = retrieved_day_dir / session_id
        paths = SessionPaths(
            root=root,
            core_dir=root / "core",
            deploy_dir=root / "deploy",
            logs_dir=root / "logs",
            retrieved_dir=retrieved_root,
        )
        for path in (root, paths.core_dir, paths.deploy_dir, paths.logs_dir, paths.retrieved_dir):
            path.mkdir(parents=True, exist_ok=True)
        return SessionContext.create(
            session_id=session_id,
            device_id=device_id,
            task_type=task_type,
            session_paths=paths,
        )

    def latest_session_dir(self) -> Path:
        if not self.artifacts_root.exists():
            raise FileNotFoundError(f"Artifacts root does not exist: {self.artifacts_root}")

        session_dirs: list[Path] = []
        for day_dir in self.artifacts_root.iterdir():
            if not day_dir.is_dir():
                continue
            for session_dir in day_dir.iterdir():
                if session_dir.is_dir():
                    session_dirs.append(session_dir)

        if not session_dirs:
            raise FileNotFoundError(f"No session directories found under {self.artifacts_root}")

        return max(session_dirs, key=lambda path: path.relative_to(self.artifacts_root).as_posix())
