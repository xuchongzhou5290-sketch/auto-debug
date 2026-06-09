from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from pathlib import Path
from typing import Any

from autodbg.serial.runtime import (
    SerialTraceEntry,
    append_serial_trace_marker,
    serial_trace_log_dir,
    serial_trace_log_path,
)


_SAFE_CASE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(slots=True)
class SerialCaseResult:
    case_id: str
    serial_port: str
    case_dir: Path
    metadata_path: Path
    trace_jsonl_path: Path | None = None
    trace_text_path: Path | None = None
    evidence_patch_path: Path | None = None
    selected_lines: int = 0
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "serial_port": self.serial_port,
            "case_dir": str(self.case_dir),
            "metadata_path": str(self.metadata_path),
            "trace_jsonl_path": str(self.trace_jsonl_path) if self.trace_jsonl_path else None,
            "trace_text_path": str(self.trace_text_path) if self.trace_text_path else None,
            "evidence_patch_path": str(self.evidence_patch_path) if self.evidence_patch_path else None,
            "selected_lines": self.selected_lines,
            "metadata": self.metadata or {},
        }


def begin_serial_case(
    port: str,
    case_id: str,
    *,
    title: str | None = None,
    note: str | None = None,
) -> SerialCaseResult:
    safe_case_id = _safe_case_id(case_id)
    active_path = _active_case_path(port, safe_case_id)
    if active_path.is_file():
        active_payload = _read_json(active_path)
        active_case_dir = Path(str(active_payload.get("case_dir", "")))
        if (active_case_dir / "metadata.json").is_file():
            active_metadata = _read_json(active_case_dir / "metadata.json")
            if active_metadata.get("status") == "active":
                raise FileExistsError(f"Serial case already active for {port} case_id={case_id}.")
    trace_path = serial_trace_log_path(port)
    start_line = _count_lines(trace_path)
    started_at = _now()
    case_dir = _cases_root(port) / f"{_timestamp_slug(started_at)}-{safe_case_id}"
    case_dir.mkdir(parents=True, exist_ok=False)

    metadata = {
        "case_id": case_id,
        "safe_case_id": safe_case_id,
        "title": title,
        "note": note,
        "serial_port": port,
        "status": "active",
        "result": None,
        "started_at": started_at,
        "ended_at": None,
        "start_line": start_line,
        "end_line": None,
        "trace_path": str(trace_path),
        "case_dir": str(case_dir),
    }
    metadata_path = case_dir / "metadata.json"
    _write_json(metadata_path, metadata)
    _write_json(active_path, {"case_dir": str(case_dir)})
    append_serial_trace_marker(port, _case_marker("CASE_START", case_id=case_id, title=title))
    return SerialCaseResult(
        case_id=case_id,
        serial_port=port,
        case_dir=case_dir,
        metadata_path=metadata_path,
        metadata=metadata,
    )


def end_serial_case(
    port: str,
    case_id: str,
    *,
    result: str = "unknown",
    note: str | None = None,
) -> SerialCaseResult:
    safe_case_id = _safe_case_id(case_id)
    case_dir = _load_active_case_dir(port, safe_case_id)
    metadata_path = case_dir / "metadata.json"
    metadata = _read_json(metadata_path)
    append_serial_trace_marker(port, _case_marker("CASE_END", case_id=case_id, result=result, note=note))

    trace_path = Path(str(metadata.get("trace_path") or serial_trace_log_path(port)))
    start_line = int(metadata.get("start_line", 0) or 0)
    end_line = _count_lines(trace_path)
    entries = _read_trace_entries(trace_path, start_line=start_line, end_line=end_line)
    trace_jsonl_path = case_dir / "trace.jsonl"
    trace_text_path = case_dir / "trace.txt"
    evidence_patch_path = case_dir / "evidence_patch.md"

    _write_trace_jsonl(trace_jsonl_path, entries)
    trace_text_path.write_text(_format_entries_text(entries), encoding="utf-8", newline="\n")
    metadata.update(
        {
            "status": "ended",
            "result": result,
            "ended_at": _now(),
            "end_line": end_line,
            "line_count": len(entries),
            "end_note": note,
            "trace_jsonl_path": str(trace_jsonl_path),
            "trace_text_path": str(trace_text_path),
            "evidence_patch_path": str(evidence_patch_path),
        }
    )
    _write_json(metadata_path, metadata)
    evidence_patch_path.write_text(
        _render_evidence_patch(metadata, entries, selected_entries=_compact_entries(entries)),
        encoding="utf-8",
        newline="\n",
    )
    _remove_active_case(port, safe_case_id)
    return SerialCaseResult(
        case_id=case_id,
        serial_port=port,
        case_dir=case_dir,
        metadata_path=metadata_path,
        trace_jsonl_path=trace_jsonl_path,
        trace_text_path=trace_text_path,
        evidence_patch_path=evidence_patch_path,
        selected_lines=len(entries),
        metadata=metadata,
    )


def capture_serial_case(
    port: str,
    case_id: str,
    *,
    focus: list[str] | None = None,
    before: int = 20,
    after: int = 40,
) -> SerialCaseResult:
    safe_case_id = _safe_case_id(case_id)
    case_dir = _find_case_dir(port, safe_case_id)
    metadata_path = case_dir / "metadata.json"
    metadata = _read_json(metadata_path)
    entries = _case_entries_from_metadata(metadata)
    selected_entries = _select_entries(entries, focus=focus or [], before=before, after=after)
    captured_at = _now()
    capture_path = case_dir / f"capture-{_timestamp_slug(captured_at)}.md"
    capture_metadata = dict(metadata)
    capture_metadata.update(
        {
            "captured_at": captured_at,
            "capture_focus": list(focus or []),
            "capture_before": before,
            "capture_after": after,
            "capture_path": str(capture_path),
        }
    )
    capture_path.write_text(
        _render_evidence_patch(capture_metadata, entries, selected_entries=selected_entries),
        encoding="utf-8",
        newline="\n",
    )
    return SerialCaseResult(
        case_id=case_id,
        serial_port=port,
        case_dir=case_dir,
        metadata_path=metadata_path,
        evidence_patch_path=capture_path,
        selected_lines=len(selected_entries),
        metadata=capture_metadata,
    )


def _case_entries_from_metadata(metadata: dict[str, Any]) -> list[SerialTraceEntry]:
    trace_jsonl_path = metadata.get("trace_jsonl_path")
    if trace_jsonl_path and Path(str(trace_jsonl_path)).is_file():
        return _read_trace_entries(Path(str(trace_jsonl_path)))
    trace_path = Path(str(metadata.get("trace_path")))
    start_line = int(metadata.get("start_line", 0) or 0)
    end_line = metadata.get("end_line")
    return _read_trace_entries(
        trace_path,
        start_line=start_line,
        end_line=int(end_line) if end_line is not None else None,
    )


def _cases_root(port: str) -> Path:
    path = serial_trace_log_dir(port) / "cases"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _active_case_path(port: str, safe_case_id: str) -> Path:
    path = _cases_root(port) / ".active" / f"{safe_case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_active_case_dir(port: str, safe_case_id: str) -> Path:
    active_path = _active_case_path(port, safe_case_id)
    if active_path.is_file():
        payload = _read_json(active_path)
        case_dir = Path(str(payload.get("case_dir", "")))
        if (case_dir / "metadata.json").is_file():
            return case_dir
    case_dir = _find_case_dir(port, safe_case_id, active_only=True)
    if case_dir:
        return case_dir
    raise FileNotFoundError(f"No active serial case found for {port} case_id={safe_case_id}.")


def _find_case_dir(port: str, safe_case_id: str, *, active_only: bool = False) -> Path:
    candidates = []
    for metadata_path in sorted(_cases_root(port).glob(f"*-{safe_case_id}/metadata.json"), reverse=True):
        metadata = _read_json(metadata_path)
        if active_only and metadata.get("status") != "active":
            continue
        candidates.append(metadata_path.parent)
    if not candidates:
        raise FileNotFoundError(f"No serial case found for {port} case_id={safe_case_id}.")
    return candidates[0]


def _remove_active_case(port: str, safe_case_id: str) -> None:
    try:
        _active_case_path(port, safe_case_id).unlink()
    except FileNotFoundError:
        pass


def _safe_case_id(case_id: str) -> str:
    cleaned = _SAFE_CASE_ID_RE.sub("-", case_id.strip()).strip("-._")
    return cleaned or "case"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _timestamp_slug(value: str) -> str:
    return value.replace("-", "").replace(":", "").replace("T", "-")


def _case_marker(kind: str, **fields: str | None) -> str:
    chunks = [kind]
    for key, value in fields.items():
        if value:
            chunks.append(f"{key}={value}")
    return " ".join(chunks)


def _count_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return sum(1 for _line in handle)
    except FileNotFoundError:
        return 0


def _read_trace_entries(
    path: Path,
    *,
    start_line: int = 0,
    end_line: int | None = None,
) -> list[SerialTraceEntry]:
    entries: list[SerialTraceEntry] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index < start_line:
                    continue
                if end_line is not None and index >= end_line:
                    break
                cleaned = line.strip()
                if not cleaned:
                    continue
                try:
                    entries.append(SerialTraceEntry.from_json_line(cleaned))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
    except FileNotFoundError:
        return []
    return entries


def _write_trace_jsonl(path: Path, entries: list[SerialTraceEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")


def _format_entries_text(entries: list[SerialTraceEntry]) -> str:
    lines = [_format_entry(entry) for entry in entries]
    return "\n".join(lines) + ("\n" if lines else "")


def _format_entry(entry: SerialTraceEntry) -> str:
    return f"[{entry.timestamp}] {entry.direction.upper()} {entry.payload}"


def _compact_entries(entries: list[SerialTraceEntry], *, limit: int = 120) -> list[SerialTraceEntry]:
    if len(entries) <= limit:
        return entries
    head = max(limit // 3, 1)
    tail = max(limit - head, 1)
    return [*entries[:head], *entries[-tail:]]


def _select_entries(
    entries: list[SerialTraceEntry],
    *,
    focus: list[str],
    before: int,
    after: int,
) -> list[SerialTraceEntry]:
    terms = [term.lower() for term in focus if term.strip()]
    if not terms:
        return _compact_entries(entries)
    selected_indexes: set[int] = set()
    for index, entry in enumerate(entries):
        text = f"{entry.direction} {entry.payload}".lower()
        if any(term in text for term in terms):
            start = max(0, index - max(before, 0))
            end = min(len(entries), index + max(after, 0) + 1)
            selected_indexes.update(range(start, end))
    return [entry for index, entry in enumerate(entries) if index in selected_indexes]


def _render_evidence_patch(
    metadata: dict[str, Any],
    entries: list[SerialTraceEntry],
    *,
    selected_entries: list[SerialTraceEntry],
) -> str:
    lines = [
        "# Serial Evidence Patch",
        "",
        f"- Serial Port: `{metadata.get('serial_port', 'unknown')}`",
        f"- Case ID: `{metadata.get('case_id', 'unknown')}`",
        f"- Title: `{metadata.get('title') or 'none'}`",
        f"- Result: `{metadata.get('result') or 'unknown'}`",
        f"- Started At: `{metadata.get('started_at') or 'unknown'}`",
        f"- Ended At: `{metadata.get('ended_at') or metadata.get('captured_at') or 'active'}`",
        f"- Full Lines: `{len(entries)}`",
        f"- Selected Lines: `{len(selected_entries)}`",
    ]
    if metadata.get("trace_text_path"):
        lines.append(f"- Trace Text: `{metadata['trace_text_path']}`")
    if metadata.get("trace_jsonl_path"):
        lines.append(f"- Trace JSONL: `{metadata['trace_jsonl_path']}`")
    lines.extend(["", "## Excerpt", "", "```text"])
    if selected_entries:
        lines.extend(_format_entry(entry) for entry in selected_entries)
    else:
        lines.append("(no matching serial lines)")
    lines.extend(["```", ""])
    return "\n".join(lines)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
