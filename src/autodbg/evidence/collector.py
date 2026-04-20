from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path
from typing import Any

from autodbg.models.profile import RunProfiles
from autodbg.models.session import SessionContext
from autodbg.state.machine import StateSnapshot


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class EvidenceCollector:
    def __init__(self, session: SessionContext) -> None:
        self.session = session
        self.summary_path = session.session_paths.root / "summary.json"
        self.events_path = session.session_paths.root / "events.jsonl"
        self.manifest_path = session.session_paths.root / "artifacts_manifest.json"
        self.manual_actions_path = session.session_paths.root / "manual_actions.jsonl"
        self.report_path = session.session_paths.root / "report.md"

    def bootstrap(
        self,
        profiles: RunProfiles,
        state_snapshot: StateSnapshot,
        workflow_name: str,
        workflow_steps: list[dict[str, Any]],
        plan_details: dict[str, Any],
    ) -> None:
        summary = {
            "session": self.session.to_dict(),
            "profiles": profiles.to_dict(),
            "workflow": workflow_name,
            "workflow_steps": workflow_steps,
            "state": state_snapshot.to_dict(),
            "plan_details": plan_details,
            "status": "planned",
        }
        self.summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.report_path.write_text(self._render_report(summary), encoding="utf-8", newline="\n")
        self.events_path.write_text("", encoding="utf-8", newline="\n")
        manifest = {
            "session_id": self.session.session_id,
            "artifacts": [
                {"type": "summary", "path": str(self.summary_path)},
                {"type": "report", "path": str(self.report_path)},
                {"type": "events", "path": str(self.events_path)},
                {"type": "manual_actions", "path": str(self.manual_actions_path)},
            ],
        }
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.manual_actions_path.write_text("", encoding="utf-8", newline="\n")

    def append_event(
        self,
        event_type: str,
        source: str,
        summary: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "event_type": event_type,
            "source": source,
            "summary": summary,
            "severity": severity,
            "payload": payload or {},
        }
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=_json_default) + "\n")

    def append_manual_action(self, action_type: str, result: str, notes: str = "") -> None:
        record = {
            "manual_action_type": action_type,
            "operator_result": result,
            "notes": notes,
        }
        with self.manual_actions_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def write_text_artifact(self, relative_path: str, content: str) -> Path:
        target = self.session.session_paths.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        self._register_artifact(target)
        return target

    def write_binary_artifact(self, relative_path: str, content: bytes, artifact_type: str = "binary") -> Path:
        target = self.session.session_paths.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self._register_artifact(target, artifact_type=artifact_type)
        return target

    def write_retrieved_artifact(
        self,
        relative_path: str,
        content: bytes,
        artifact_type: str = "retrieved_file",
    ) -> Path:
        target = self.session.session_paths.retrieved_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self._register_artifact(target, artifact_type=artifact_type)
        return target

    def register_artifact(self, path: Path, artifact_type: str | None = None) -> Path:
        self._register_artifact(path, artifact_type=artifact_type)
        return path

    def update_summary(self, patch: dict[str, Any]) -> None:
        summary = json.loads(self.summary_path.read_text(encoding="utf-8"))
        summary.update(patch)
        self.summary_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.report_path.write_text(self._render_report(summary), encoding="utf-8", newline="\n")

    def _register_artifact(self, path: Path, artifact_type: str | None = None) -> None:
        artifact_type = artifact_type or path.suffix.lstrip(".") or "file"
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        artifacts = manifest.setdefault("artifacts", [])
        artifact_entry = {"type": artifact_type, "path": str(path)}
        if artifact_entry not in artifacts:
            artifacts.append(artifact_entry)
        self.manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    def _render_report(self, summary: dict[str, Any]) -> str:
        session = summary.get("session", {})
        profiles = summary.get("profiles", {})
        evaluation = summary.get("evaluation", {})
        evidence = summary.get("evidence_results", {})
        lines = [
            "# Session Report",
            "",
            "## Session",
            f"- Session ID: `{session.get('session_id', 'unknown')}`",
            f"- Created At: `{session.get('created_at', 'unknown')}`",
            f"- Device: `{session.get('device_id', 'unknown')}`",
            f"- Task: `{session.get('task_type', 'unknown')}`",
            f"- Status: `{summary.get('status', 'unknown')}`",
            f"- Workflow: `{summary.get('workflow', 'unknown')}`",
        ]

        model = profiles.get("model", {})
        if model:
            lines.append(f"- App: `{model.get('app_name', 'unknown')}`")

        manual_checks = profiles.get("task", {}).get("manual_check_items", [])
        if manual_checks:
            lines.extend(["", "## Manual Checks"])
            for item in manual_checks:
                lines.append(f"- {item}")

        if evaluation:
            lines.extend(
                [
                    "",
                    "## Evaluation",
                    f"- Verdict: `{evaluation.get('verdict', 'unknown')}`",
                    f"- Summary: {evaluation.get('summary', '')}",
                ]
            )
            highlights = evaluation.get("highlights", {})
            if highlights:
                lines.extend(["", "## Highlights"])
                for key, value in highlights.items():
                    lines.append(f"- {key}: `{value}`")
            findings = evaluation.get("findings", [])
            if findings:
                lines.extend(["", "## Findings"])
                for finding in findings:
                    lines.append(
                        f"- `{finding.get('level', 'info')}` {finding.get('check_name', 'unknown')}: "
                        f"{finding.get('message', '')}"
                    )

        if evidence:
            command_results = evidence.get("command_results", [])
            file_results = evidence.get("file_results", [])
            command_failures = [item for item in command_results if item.get("exit_code") != 0]
            file_failures = [item for item in file_results if item.get("status") != "ok"]
            lines.extend(["", "## Evidence"])
            lines.append(f"- Commands: `{len(command_results)}`")
            if file_results:
                ok_files = sum(1 for item in file_results if item.get("status") == "ok")
                lines.append(f"- Files: `{ok_files}/{len(file_results)}`")
            if command_failures:
                lines.append(f"- Command failures: `{len(command_failures)}`")
            if file_failures:
                lines.append(f"- File failures: `{len(file_failures)}`")

        session_paths = session.get("session_paths", {})
        if session_paths:
            lines.extend(["", "## Paths"])
            for key, value in session_paths.items():
                lines.append(f"- {key}: `{value}`")

        return "\n".join(lines) + "\n"
