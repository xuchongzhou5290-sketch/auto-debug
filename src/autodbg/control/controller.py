from __future__ import annotations

from dataclasses import dataclass
import os
import time
from uuid import uuid4

from autodbg.models.profile import DeviceProfile, ModelProfile, TransportProfile
from autodbg.serial.runtime import SerialPortProtocol, open_serial_port
from autodbg.utils.text import contains_shell_prompt, strip_ansi


@dataclass(slots=True)
class ControlPlan:
    preferred_channels: list[str]
    login_prompt: str
    shell_prompt: str
    app_name: str


@dataclass(slots=True)
class LoginResult:
    success: bool
    shell_prompt_seen: bool
    login_prompt_seen: bool
    transcript: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "shell_prompt_seen": self.shell_prompt_seen,
            "login_prompt_seen": self.login_prompt_seen,
            "transcript": self.transcript,
        }


@dataclass(slots=True)
class CommandResult:
    command: str
    exit_code: int | None
    output_lines: list[str]
    transcript: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "output_lines": self.output_lines,
            "transcript": self.transcript,
        }


@dataclass(slots=True)
class MultiCommandResult:
    results: list[CommandResult]

    def to_dict(self) -> dict[str, object]:
        return {"results": [result.to_dict() for result in self.results]}


class LoginRequiredError(RuntimeError):
    """Raised when a password-protected login cannot be completed."""


class DeviceController:
    def __init__(
        self,
        device_profile: DeviceProfile,
        model_profile: ModelProfile,
        transport_profile: TransportProfile,
    ) -> None:
        self.device_profile = device_profile
        self.model_profile = model_profile
        self.transport_profile = transport_profile

    def plan(self) -> ControlPlan:
        return ControlPlan(
            preferred_channels=list(self.transport_profile.control_channels),
            login_prompt=self.device_profile.serial.login_prompt,
            shell_prompt=self.device_profile.serial.shell_prompt,
            app_name=self.model_profile.app_name,
        )

    def login(
        self,
        timeout: float = 20.0,
        *,
        serial_port: SerialPortProtocol | None = None,
    ) -> LoginResult:
        if serial_port is None:
            with open_serial_port(
                self.device_profile.serial.port,
                self.device_profile.serial.baudrate,
                timeout=0.2,
            ) as handle:
                return self._login_with_port(handle, timeout)
        return self._login_with_port(serial_port, timeout)

    def execute(
        self,
        command: str,
        timeout: float = 15.0,
        *,
        serial_port: SerialPortProtocol | None = None,
    ) -> CommandResult:
        if serial_port is None:
            with open_serial_port(
                self.device_profile.serial.port,
                self.device_profile.serial.baudrate,
                timeout=0.2,
            ) as handle:
                return self._execute_with_port(handle, command, timeout)
        return self._execute_with_port(serial_port, command, timeout)

    def execute_many(
        self,
        commands: list[str],
        timeout: float = 15.0,
        *,
        serial_port: SerialPortProtocol | None = None,
    ) -> MultiCommandResult:
        if serial_port is None:
            with open_serial_port(
                self.device_profile.serial.port,
                self.device_profile.serial.baudrate,
                timeout=0.2,
            ) as handle:
                return self._execute_many_with_port(handle, commands, timeout)
        return self._execute_many_with_port(serial_port, commands, timeout)

    def _login_with_port(self, serial_port: SerialPortProtocol, timeout: float) -> LoginResult:
        transcript: list[str] = []
        shell_prompt_seen = False
        login_prompt_seen = False

        self._send_line(serial_port, "")
        end_time = time.monotonic() + timeout
        last_poke_at = time.monotonic()
        while time.monotonic() < end_time:
            line = self._read_line(serial_port)
            if line is None:
                if time.monotonic() - last_poke_at >= 2.0:
                    self._send_line(serial_port, "")
                    last_poke_at = time.monotonic()
                continue
            clean_line = strip_ansi(line)
            transcript.append(clean_line)
            if contains_shell_prompt(clean_line, self.device_profile.serial.shell_prompt):
                shell_prompt_seen = True
                return LoginResult(True, shell_prompt_seen, login_prompt_seen, transcript)
            if self.device_profile.serial.login_prompt in clean_line:
                login_prompt_seen = True
                self._send_line(serial_port, self._username())
                continue
            if "password" in clean_line.lower():
                password = self._password()
                if not password:
                    return LoginResult(False, shell_prompt_seen, login_prompt_seen, transcript)
                self._send_line(serial_port, password)
                continue
            if time.monotonic() - last_poke_at >= 2.0:
                self._send_line(serial_port, "")
                last_poke_at = time.monotonic()

        return LoginResult(
            success=shell_prompt_seen,
            shell_prompt_seen=shell_prompt_seen,
            login_prompt_seen=login_prompt_seen,
            transcript=transcript,
        )

    def _execute_many_with_port(
        self,
        serial_port: SerialPortProtocol,
        commands: list[str],
        timeout: float,
    ) -> MultiCommandResult:
        results = [self._execute_with_port(serial_port, command, timeout) for command in commands]
        return MultiCommandResult(results=results)

    def _execute_with_port(
        self,
        serial_port: SerialPortProtocol,
        command: str,
        timeout: float,
    ) -> CommandResult:
        login_result = self._login_with_port(serial_port, timeout=min(timeout, 20.0))
        if not login_result.success:
            if login_result.login_prompt_seen and not login_result.shell_prompt_seen:
                raise LoginRequiredError(
                    "Login prompt reached but shell access was not established. "
                    "Set AUTO_DBG_DEVICE_PASSWORD or provide the device password."
                )
            raise RuntimeError("Unable to establish a root shell over serial.")
        transcript = list(login_result.transcript)
        marker = f"__AUTODBG_{uuid4().hex[:8].upper()}__"
        begin_marker = f"{marker}_BEGIN"
        end_prefix = f"{marker}_END:"
        wrapped_command = (
            f"printf '{begin_marker}\\n'; "
            f"{command}; "
            f"printf '{end_prefix}%s\\n' $?"
        )
        self._send_line(serial_port, wrapped_command)

        output_lines: list[str] = []
        exit_code: int | None = None
        capture = False
        end_time = time.monotonic() + timeout

        while time.monotonic() < end_time:
            line = self._read_line(serial_port)
            if line is None:
                continue
            clean_line = strip_ansi(line)
            transcript.append(clean_line)
            if clean_line.strip() == begin_marker:
                capture = True
                continue
            if clean_line.startswith(end_prefix):
                exit_code = self._parse_exit_code(clean_line, end_prefix)
                break
            if capture and self._should_collect_output_line(
                clean_line=clean_line,
                command=command,
                begin_marker=begin_marker,
            ):
                output_lines.append(clean_line)

        return CommandResult(
            command=command,
            exit_code=exit_code,
            output_lines=output_lines,
            transcript=transcript,
        )

    @staticmethod
    def _read_line(serial_port: SerialPortProtocol) -> str | None:
        raw = serial_port.readline()
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    @staticmethod
    def _send_line(serial_port: SerialPortProtocol, text: str) -> None:
        serial_port.write((text + "\n").encode("utf-8"))
        serial_port.flush()

    def _username(self) -> str:
        if self.device_profile.credentials is None:
            return "root"
        return self.device_profile.credentials.username

    def _password(self) -> str | None:
        if self.device_profile.credentials is None:
            return None
        if self.device_profile.credentials.password is not None:
            return self.device_profile.credentials.password
        if self.device_profile.credentials.password_env:
            return os.getenv(self.device_profile.credentials.password_env)
        return None

    @staticmethod
    def _parse_exit_code(line: str, prefix: str) -> int | None:
        value = line.removeprefix(prefix).strip()
        try:
            return int(value)
        except ValueError:
            return None

    def _should_collect_output_line(self, *, clean_line: str, command: str, begin_marker: str) -> bool:
        stripped = clean_line.strip()
        if not stripped:
            return False
        if stripped == begin_marker:
            return False
        if stripped == command:
            return False
        if contains_shell_prompt(stripped, self.device_profile.serial.shell_prompt):
            return False
        return True
