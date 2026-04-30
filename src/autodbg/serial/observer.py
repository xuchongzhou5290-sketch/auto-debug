from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import time

from autodbg.models.profile import ModelProfile, SerialSettings
from autodbg.serial.runtime import SerialPortProtocol, open_serial_port
from autodbg.state.machine import DeviceState
from autodbg.utils.text import contains_shell_prompt, strip_ansi


@dataclass(slots=True)
class SerialObserverPlan:
    port: str
    baudrate: int
    markers: list[str]


@dataclass(slots=True)
class MarkerHit:
    marker: str
    line: str
    tag: str = "marker"
    device_state: str | None = None


@dataclass(slots=True)
class MarkerWindow:
    kind: str
    marker: str
    line: str
    line_index: int
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ObservationResult:
    lines_captured: int
    last_lines: list[str]
    marker_hits: list[MarkerHit] = field(default_factory=list)
    marker_windows: list[MarkerWindow] = field(default_factory=list)
    marker_verdict: str | None = None
    last_device_state: str | None = None
    interrupted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "lines_captured": self.lines_captured,
            "last_lines": self.last_lines,
            "marker_hits": [
                {
                    "marker": hit.marker,
                    "line": hit.line,
                    "tag": hit.tag,
                    "device_state": hit.device_state,
                }
                for hit in self.marker_hits
            ],
            "marker_windows": [
                {
                    "kind": window.kind,
                    "marker": window.marker,
                    "line": window.line,
                    "line_index": window.line_index,
                    "before": window.before,
                    "after": window.after,
                }
                for window in self.marker_windows
            ],
            "marker_verdict": self.marker_verdict,
            "last_device_state": self.last_device_state,
            "interrupted": self.interrupted,
        }


class SerialObserver:
    def __init__(self, serial_settings: SerialSettings, model_profile: ModelProfile) -> None:
        self.serial_settings = serial_settings
        self.model_profile = model_profile

    def plan(self) -> SerialObserverPlan:
        markers = list(
            dict.fromkeys(
                self.model_profile.app_start_markers
                + self.model_profile.app_ready_markers
                + self.model_profile.panic_markers
                + self._success_markers()
                + self._fatal_markers()
            )
        )
        return SerialObserverPlan(
            port=self.serial_settings.port,
            baudrate=self.serial_settings.baudrate,
            markers=markers,
        )

    def capture(
        self,
        seconds: float,
        log_path: Path,
        *,
        serial_port: SerialPortProtocol | None = None,
        max_lines: int | None = None,
        line_callback: Callable[[str, MarkerHit | None], None] | None = None,
        startup_lines: list[str] | None = None,
    ) -> ObservationResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        owns_port = serial_port is None
        if owns_port:
            with open_serial_port(
                self.serial_settings.port,
                self.serial_settings.baudrate,
                timeout=0.2,
            ) as handle:
                return self._capture_with_port(
                    handle,
                    seconds,
                    log_path,
                    max_lines=max_lines,
                    line_callback=line_callback,
                    startup_lines=startup_lines,
                )
        return self._capture_with_port(
            serial_port,
            seconds,
            log_path,
            max_lines=max_lines,
            line_callback=line_callback,
            startup_lines=startup_lines,
        )

    def _capture_with_port(
        self,
        serial_port: SerialPortProtocol,
        seconds: float,
        log_path: Path,
        *,
        max_lines: int | None = None,
        line_callback: Callable[[str, MarkerHit | None], None] | None = None,
        startup_lines: list[str] | None = None,
    ) -> ObservationResult:
        end_time = None if seconds <= 0 else time.monotonic() + max(seconds, 0.1)
        lines_captured = 0
        last_lines: list[str] = []
        captured_lines: list[str] = []
        marker_hits: list[MarkerHit] = []
        last_device_state: str | None = None
        interrupted = False

        with log_path.open("a", encoding="utf-8", newline="\n") as log_handle:
            try:
                for startup_line in startup_lines or []:
                    serial_port.write((startup_line + "\n").encode("utf-8"))
                    serial_port.flush()

                while end_time is None or time.monotonic() < end_time:
                    raw = serial_port.readline()
                    if not raw:
                        continue

                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    log_handle.write(line + "\n")
                    log_handle.flush()
                    lines_captured += 1
                    clean_line = strip_ansi(line)
                    captured_lines.append(clean_line)
                    last_lines.append(clean_line)
                    if len(last_lines) > 20:
                        last_lines.pop(0)

                    hit = self.classify_line(clean_line)
                    if hit is not None:
                        marker_hits.append(hit)
                        if hit.device_state:
                            last_device_state = hit.device_state

                    if line_callback is not None:
                        line_callback(clean_line, hit)

                    if max_lines is not None and lines_captured >= max_lines:
                        break
            except KeyboardInterrupt:
                interrupted = True

        marker_windows = self._build_marker_windows(captured_lines)
        return ObservationResult(
            lines_captured=lines_captured,
            last_lines=last_lines,
            marker_hits=marker_hits,
            marker_windows=marker_windows,
            marker_verdict=self._marker_verdict(marker_windows),
            last_device_state=last_device_state,
            interrupted=interrupted,
        )

    def _success_markers(self) -> list[str]:
        return self.model_profile.success_markers or self.model_profile.app_ready_markers

    def _fatal_markers(self) -> list[str]:
        return self.model_profile.fatal_markers or self.model_profile.panic_markers

    def _marker_context_lines(self) -> int:
        return max(int(self.model_profile.marker_context_lines), 0)

    def _build_marker_windows(self, lines: list[str]) -> list[MarkerWindow]:
        context = self._marker_context_lines()
        windows: list[MarkerWindow] = []
        rules = (
            ("fatal", self._fatal_markers()),
            ("success", self._success_markers()),
        )
        for line_index, line in enumerate(lines):
            for kind, markers in rules:
                marker = next((item for item in markers if item and item.lower() in line.lower()), None)
                if marker is None:
                    continue
                windows.append(
                    MarkerWindow(
                        kind=kind,
                        marker=marker,
                        line=line,
                        line_index=line_index,
                        before=lines[max(0, line_index - context) : line_index],
                        after=lines[line_index + 1 : line_index + 1 + context],
                    )
                )
                break
        return windows

    @staticmethod
    def _marker_verdict(marker_windows: list[MarkerWindow]) -> str | None:
        if any(window.kind == "fatal" for window in marker_windows):
            return "fatal"
        if any(window.kind == "success" for window in marker_windows):
            return "success"
        return None

    def classify_line(self, line: str) -> MarkerHit | None:
        if contains_shell_prompt(line, self.serial_settings.shell_prompt):
            return MarkerHit(
                marker=self.serial_settings.shell_prompt,
                line=line,
                tag="shell",
                device_state=DeviceState.ROOT_SHELL.value,
            )
        if self.serial_settings.login_prompt in line:
            return MarkerHit(
                marker=self.serial_settings.login_prompt,
                line=line,
                tag="login",
                device_state=DeviceState.LOGIN_PROMPT.value,
            )

        for marker in self.model_profile.fatal_markers:
            if marker.lower() in line.lower():
                return MarkerHit(marker=marker, line=line, tag="fatal", device_state=DeviceState.PANIC_OR_HANG.value)

        for marker in self.model_profile.app_ready_markers:
            if marker in line:
                return MarkerHit(marker=marker, line=line, tag="app_ready", device_state=DeviceState.APP_READY.value)

        for marker in self.model_profile.success_markers:
            if marker.lower() in line.lower():
                return MarkerHit(marker=marker, line=line, tag="success", device_state=DeviceState.APP_READY.value)

        for marker in self.model_profile.app_start_markers:
            if marker in line:
                return MarkerHit(marker=marker, line=line, tag="app_start", device_state=DeviceState.APP_STARTING.value)

        for marker in self.model_profile.panic_markers:
            if marker.lower() in line.lower():
                return MarkerHit(marker=marker, line=line, tag="panic", device_state=DeviceState.PANIC_OR_HANG.value)

        if "U-Boot" in line:
            return MarkerHit(marker="U-Boot", line=line, tag="boot", device_state=DeviceState.BOOTLOADER.value)

        if "Starting kernel" in line or line.startswith("["):
            return MarkerHit(marker="kernel", line=line, tag="kernel", device_state=DeviceState.KERNEL_BOOTING.value)

        return None
