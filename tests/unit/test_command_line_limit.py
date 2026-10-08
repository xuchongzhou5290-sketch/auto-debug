"""A command line longer than the device shell's line editor keeps (busybox: 1022 bytes) is cut off, closing quotes
included, and the shell then waits at its continuation prompt, swallowing every later command. DeviceController
refuses such a command before opening the port or sending anything.
"""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from autodbg.cli import main as cli_main
from autodbg.cli.transport import _fetch_remote_file_artifact
from autodbg.control import controller as controller_module
from autodbg.control.controller import CommandTooLongError, DeviceController
from autodbg.evidence.collector import EvidenceCollector
from autodbg.models.profile import (
    DEFAULT_MAX_LINE_BYTES,
    Credentials,
    DeviceProfile,
    ModelProfile,
    SerialSettings,
    TransportProfile,
)
from autodbg.session.manager import SessionManager


_MARKER_RE = re.compile(r"__AUTODBG_[0-9A-F]{8}__")
_WRAPPER_BYTES = len(DeviceController._wrap_command("", "__AUTODBG_00000000__").encode("utf-8"))


class _FakeShellPort:
    """Logs in, then answers every wrapped command with its begin marker, one output line and END:0."""

    def __init__(self) -> None:
        self.lines: list[bytes] = [b"closeli login:\n"]
        self.writes: list[str] = []

    def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8")
        self.writes.append(text)
        if text.strip() in ("root", ""):  # the controller logs in (or pokes for the prompt) before every command
            self.lines.append(b"[root@closeli:~]#\n")
        elif _MARKER_RE.search(text):
            marker = _MARKER_RE.search(text).group(0)
            self.lines += [f"{marker}_BEGIN\n".encode(), b"ok\n", f"{marker}_END:0\n".encode()]
        return len(data)

    def flush(self) -> None:
        return None

    def commands_sent(self) -> list[str]:
        return [text for text in self.writes if _MARKER_RE.search(text)]


def _controller(max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> DeviceController:
    return DeviceController(
        device_profile=DeviceProfile(
            device_id="lab",
            model_id="lab-model",
            serial=SerialSettings(
                port="COM16",
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
                max_line_bytes=max_line_bytes,
            ),
            credentials=Credentials(username="root"),
        ),
        model_profile=ModelProfile(model_id="lab-model", platform="LAB", app_name="LeCam"),
        transport_profile=TransportProfile(transport_id="serial", control_channels=["serial"]),
    )


def _command_of(size: int) -> str:
    """A one-line command whose wrapped line is exactly size bytes."""
    return "echo " + "x" * (size - _WRAPPER_BYTES - len("echo "))


# 800 bytes: fits when sent as is (871 typed), not inside _build_structured_command (about 1130), which run, health,
# collect-evidence and the bootstrap/check/list/post-pull/reboot/validation commands all go through.
_TOO_LONG_ONLY_STRUCTURED = "echo " + "x" * 795


class ControllerLineLimitTest(unittest.TestCase):
    def test_default_limit_is_below_what_the_device_line_editor_keeps(self) -> None:
        self.assertEqual(SerialSettings(port="COM1").max_line_bytes, 1000)
        self.assertLess(DEFAULT_MAX_LINE_BYTES, 1022)

    def test_line_at_the_limit_is_sent_and_one_byte_more_is_not(self) -> None:
        at_limit = _command_of(1000)
        wrapped = DeviceController._wrap_command(at_limit, "__AUTODBG_00000000__")
        self.assertEqual(len(wrapped.encode("utf-8")), 1000)
        port = _FakeShellPort()
        result = _controller().execute(at_limit, serial_port=port, timeout=2.0)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(port.commands_sent()), 1)

        port = _FakeShellPort()
        with self.assertRaises(CommandTooLongError) as caught:
            _controller().execute(at_limit + "x", serial_port=port, timeout=2.0)
        self.assertIn("1001 bytes", str(caught.exception))
        self.assertIn("1000-byte line limit", str(caught.exception))
        self.assertEqual(port.writes, [])  # not even the login

    def test_refused_before_the_port_is_opened(self) -> None:
        with patch.object(controller_module, "open_serial_port", side_effect=AssertionError("port opened")):
            with self.assertRaises(CommandTooLongError):
                _controller().execute("echo " + "x" * 2000, timeout=2.0)

    def test_bytes_not_characters(self) -> None:
        # 3-byte characters: about 330 of them already fill a 1000-byte line.
        with self.assertRaises(CommandTooLongError):
            _controller().execute("echo " + "日" * 320, serial_port=_FakeShellPort(), timeout=2.0)
        result = _controller().execute("echo " + "日" * 300, serial_port=_FakeShellPort(), timeout=2.0)
        self.assertEqual(result.exit_code, 0)

    def test_each_typed_line_is_measured_on_its_own(self) -> None:
        line = "echo " + "x" * 800
        for separator in ("\n", "\r\n", "\r"):
            with self.subTest(separator=repr(separator)):
                port = _FakeShellPort()
                result = _controller().execute(separator.join([line, line, line]), serial_port=port, timeout=2.0)
                self.assertEqual(result.exit_code, 0)
        with self.assertRaises(CommandTooLongError) as caught:
            # a middle line carries no wrapper: 1001 bytes on its own
            _controller().execute("\n".join([line, "echo " + "x" * 996, line]), serial_port=_FakeShellPort(), timeout=2.0)
        self.assertIn("Line 2 of the command", str(caught.exception))

    def test_wrapper_counts_on_the_first_and_last_line_only(self) -> None:
        begin, end = DeviceController._wrap_command("\x00", "__AUTODBG_00000000__").split("\x00")
        first_budget = 1000 - len(begin.encode("utf-8"))
        last_budget = 1000 - len(end.encode("utf-8"))
        short = "echo b"
        cases = [
            ("x" * first_budget + "\n" + short, True),
            ("x" * (first_budget + 1) + "\n" + short, False),
            (short + "\n" + "x" * last_budget, True),
            (short + "\n" + "x" * (last_budget + 1), False),
        ]
        for command, fits in cases:
            with self.subTest(fits=fits, lines=[len(line) for line in command.split("\n")]):
                port = _FakeShellPort()
                if fits:
                    _controller().execute(command, serial_port=port, timeout=2.0)
                    self.assertEqual(len(port.commands_sent()), 1)
                else:
                    with self.assertRaises(CommandTooLongError):
                        _controller().execute(command, serial_port=port, timeout=2.0)
                    self.assertEqual(port.writes, [])

    def test_zero_turns_the_check_off(self) -> None:
        port = _FakeShellPort()
        result = _controller(max_line_bytes=0).execute("echo " + "x" * 3000, serial_port=port, timeout=2.0)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(port.commands_sent()), 1)

    def test_execute_many_sends_nothing_if_any_command_is_too_long(self) -> None:
        port = _FakeShellPort()
        with self.assertRaises(CommandTooLongError):
            _controller().execute_many(["echo one", "echo " + "x" * 2000, "echo three"], serial_port=port, timeout=2.0)
        self.assertEqual(port.writes, [])

    def test_profile_setting_is_read(self) -> None:
        self.assertEqual(SerialSettings.from_dict({"port": "COM3", "max_line_bytes": 4000}).max_line_bytes, 4000)
        self.assertEqual(SerialSettings.from_dict({"port": "COM3", "max_line_bytes": 0}).max_line_bytes, 0)
        self.assertEqual(SerialSettings.from_dict({"port": "COM3"}).max_line_bytes, DEFAULT_MAX_LINE_BYTES)


class CallersOfTheLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from autodbg.profiles.loader import load_run_profiles

        root = Path(__file__).resolve().parents[2]
        cls.profiles = load_run_profiles(
            root / "profiles" / "devices" / "av130n-lab.toml",
            root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            root / "profiles" / "tasks" / "startup-check.toml",
            root / "profiles" / "transports" / "network-serial-fallback.toml",
        )

    def _collector(self, temp_dir: str) -> EvidenceCollector:
        session = SessionManager(Path(temp_dir) / "artifacts", Path(temp_dir) / "retrieved").create("lab", "check")
        collector = EvidenceCollector(session)
        collector.bootstrap(
            profiles=self.profiles,
            state_snapshot=cli_main.StateSnapshot(),
            workflow_name="check",
            workflow_steps=[],
            plan_details={"control": {"mode": "test"}},
        )
        return collector

    def test_exec_refuses_with_a_clear_last_line_and_is_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            args = cli_main._build_parser().parse_args(
                ["exec", "--shell-command", "echo " + "x" * 2000, "--artifacts-root", str(artifacts_root)]
            )
            output = io.StringIO()
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=self.profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "r")),
                patch.object(controller_module, "open_serial_port", side_effect=AssertionError("port opened")),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main._command_exec(args)
            self.assertEqual(exit_code, 1)
            lines = output.getvalue().splitlines()
            self.assertIn("1000-byte line limit", lines[-2])
            # Agents report the last line as the error: advice and reason.
            self.assertTrue(lines[-1].startswith("[TODO] Nothing was sent to the device. Shorten the command"), lines[-1])
            self.assertIn("1000-byte line limit", lines[-1])
            summary = json.loads((SessionManager(artifacts_root).latest_session_dir() / "summary.json").read_text("utf-8"))
            self.assertEqual(summary["status"], "exec_refused")
            self.assertIs(summary["result"]["retryable"], False)

    def test_refused_named_command_is_recorded_and_the_run_goes_on(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            collector = self._collector(temp_dir)
            port = _FakeShellPort()
            controller = _controller()
            long_payload = cli_main._execute_named_command(
                name="too_long",
                shell_command="echo " + "x" * 2000,
                controller=controller,
                collector=collector,
                event_type="evidence_command",
                event_summary="Evidence command completed: too_long",
                artifact_prefix="evidence-too_long",
                serial_port=port,
            )
            short_payload = cli_main._execute_named_command(
                name="short",
                shell_command="echo short",
                controller=controller,
                collector=collector,
                event_type="evidence_command",
                event_summary="Evidence command completed: short",
                artifact_prefix="evidence-short",
                serial_port=port,
            )
        self.assertIsNone(long_payload["exit_code"])
        self.assertIn("line limit", long_payload["error"])
        self.assertEqual(short_payload["exit_code"], 0)
        self.assertNotIn("error", short_payload)
        self.assertEqual(len(port.commands_sent()), 1)

    def test_refused_validation_command_is_a_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            port = _FakeShellPort()
            results = cli_main._run_validation_commands(
                controller=_controller(),
                collector=self._collector(temp_dir),
                commands=["echo " + "x" * 2000, "echo fine"],
                timeout=2.0,
                serial_port=port,
                prefix="validation",
            )
        self.assertIsNone(results[0]["exit_code"])
        self.assertIn("line limit", results[0]["transcript"][0])
        self.assertEqual(results[1]["exit_code"], 0)

    def test_fetch_under_a_lower_profile_limit_is_refused_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            port = _FakeShellPort()
            summary = _fetch_remote_file_artifact(
                controller=_controller(max_line_bytes=200),
                collector=self._collector(temp_dir),
                remote_path="/var/log/messages",
                timeout=2.0,
                prefix="fetch-file",
                serial_port=port,
            )
        self.assertEqual(summary["status"], "error")
        self.assertIs(summary["sent"], False)
        self.assertIn("200-byte line limit", summary["error"])
        self.assertEqual(port.writes, [])


def _load_lab_profiles():
    from autodbg.profiles.loader import load_run_profiles

    root = Path(__file__).resolve().parents[2]
    return load_run_profiles(
        root / "profiles" / "devices" / "av130n-lab.toml",
        root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
        root / "profiles" / "tasks" / "startup-check.toml",
        root / "profiles" / "transports" / "network-serial-fallback.toml",
    )


class BuiltinCommandsFitTest(unittest.TestCase):
    """Commands autodbg builds itself must fit the default limit, or its own flows could never run."""

    def test_builtin_commands_fit_the_default_limit(self) -> None:
        from autodbg.cli.transport import (
            _build_auto_wlan_bootstrap_commands,
            _build_fetch_file_command,
            _build_fetch_path_command,
            _FETCH_COMMAND_MAX_BYTES,
        )

        profiles = _load_lab_profiles()
        controller = _controller()
        planned = [(f"health {name}", command, True, "") for name, command in cli_main._build_health_checks(include_sd_write_probe=True)]
        planned += [(f"evidence {name}", command, True, "") for name, command in cli_main._build_collect_evidence_commands(profiles)]
        for network_dir in ("/opt/network", None):  # None: the resolver searches the usual places
            commands = _build_auto_wlan_bootstrap_commands(
                wifi_ssid="S" * 32,  # longest SSID
                wifi_password="p" * 59 + "'''",  # longest WPA2 passphrase; a quote costs 5 bytes once quoted
                wifi_mode="WPA2",
                network_dir=network_dir,
            )
            planned += [(f"wlan bootstrap (network_dir={network_dir})", command, True, "") for command in commands]
        with tempfile.TemporaryDirectory() as temp_dir:
            args = cli_main._build_parser().parse_args(["device-pull", "--root", temp_dir])
            planned += cli_main._device_pull_planned_commands(
                args,
                [],
                [],
                f"find {'/mnt/sdcard/autodbg'} -maxdepth 3 -type f | sort",
                "http://192.168.100.100:8765",
                "/mnt/sdcard/autodbg",
                profiles,
            )
        path_at_fetch_cap = "/" + "d" * (_FETCH_COMMAND_MAX_BYTES - len(_build_fetch_path_command("")) - 1)
        planned += [
            ("fetch-file at its cap", _build_fetch_file_command(path_at_fetch_cap), False, ""),
            ("fetch-path at its cap", _build_fetch_path_command(path_at_fetch_cap), False, ""),
        ]
        for label, command, structured, _ in planned:
            with self.subTest(label=label):
                self.assertIsNone(cli_main._planned_command_refusal(controller, [(label, command, structured, "")]))


class FlowPreflightTest(unittest.TestCase):
    """Multi-command flows check every planned command before sending the first one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = _load_lab_profiles()

    def _run(self, argv: list[str]) -> tuple[int, list[str], dict]:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            argv = [arg.replace("<tmp>", temp_dir) for arg in argv] + ["--artifacts-root", str(artifacts_root)]
            args = cli_main._build_parser().parse_args(argv)
            output = io.StringIO()
            port_guard = AssertionError("the serial port was opened")
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=self.profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "r")),
                patch.object(cli_main, "open_serial_port", side_effect=port_guard),
                patch.object(controller_module, "open_serial_port", side_effect=port_guard),
                patch.object(cli_main, "serve_directory", side_effect=AssertionError("HTTP server started")),
                patch.object(cli_main, "_run_host_build_command", side_effect=AssertionError("host build ran")),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main._dispatch_command(args)
            summary = json.loads((SessionManager(artifacts_root).latest_session_dir() / "summary.json").read_text("utf-8"))
        return exit_code, output.getvalue().splitlines(), summary

    def _assert_refused(self, exit_code: int, lines: list[str], summary: dict, status: str, label: str, advice: str) -> None:
        self.assertEqual(exit_code, 1, lines)
        self.assertEqual(summary["status"], status)
        self.assertIs(summary["result"]["retryable"], False)
        self.assertEqual(summary["result"]["failure_stage"], "validate_command")
        self.assertIn(label, summary["error"])
        self.assertTrue(lines[-1].startswith(f"[TODO] Nothing was sent to the device. {advice}"), lines[-1])
        self.assertTrue(summary["result"]["next_actions"][0]["reason"].startswith(advice))

    def _carried(self, summary: dict) -> dict:
        return summary["result"]["carry_forward_request"]["options"]

    def test_bootstrap_network(self) -> None:
        exit_code, lines, summary = self._run(
            ["bootstrap-network", "--mode", "lan_ready", "--bootstrap-command", "echo ok",
             "--bootstrap-command", _TOO_LONG_ONLY_STRUCTURED, "--check-command", "true", "--wifi-ssid", "lab"]
        )
        self._assert_refused(exit_code, lines, summary, "bootstrap_network_refused", "Bootstrap command #2", "Shorten that command")
        self.assertEqual(self._carried(summary)["wifi_ssid"], "lab")

    def test_bootstrap_network_check_command(self) -> None:
        exit_code, lines, summary = self._run(
            ["bootstrap-network", "--mode", "lan_ready", "--check-command", "true", "--check-command", _TOO_LONG_ONLY_STRUCTURED]
        )
        self._assert_refused(exit_code, lines, summary, "bootstrap_network_refused", "Check command #2", "Shorten that command")

    def test_device_pull_names_the_largest_chunk_size_that_fits(self) -> None:
        exit_code, lines, summary = self._run(
            ["device-pull", "--root", "<tmp>", "--mode", "offline", "--transfer-mode", "serial_bundle",
             "--serial-bundle-chunk-size", "900"]
        )
        self._assert_refused(exit_code, lines, summary, "device_pull_refused", "Serial bundle upload command", "Use --serial-bundle-chunk-size")
        largest = int(re.search(r"--serial-bundle-chunk-size (\d+) or less", lines[-1]).group(1))
        with tempfile.TemporaryDirectory() as temp_dir:
            # The advice keeps 24 bytes in hand for escaping that may grow a chunk line.
            for chunk_size, fits in ((largest, True), (largest + 24, True), (largest + 25, False)):
                args = cli_main._build_parser().parse_args(
                    ["device-pull", "--root", temp_dir, "--mode", "offline", "--transfer-mode", "serial_bundle",
                     "--serial-bundle-chunk-size", str(chunk_size)]
                )
                planned = cli_main._device_pull_planned_commands(
                    args, [], [], "true", "http://192.168.1.2:8765", "/mnt/sdcard/autodbg", self.profiles
                )
                refusal = cli_main._planned_command_refusal(_controller(), planned)
                self.assertEqual(refusal is None, fits, (chunk_size, refusal))

    def test_deploy_verify_refuses_before_building_or_deploying(self) -> None:
        with tempfile.TemporaryDirectory() as artifact_dir:
            artifact = Path(artifact_dir) / "artifact.bin"
            artifact.write_bytes(b"x")
            for slot in (["--post-pull-command", _TOO_LONG_ONLY_STRUCTURED], ["--validation-command", _TOO_LONG_ONLY_STRUCTURED],
                         ["--reboot-command", _TOO_LONG_ONLY_STRUCTURED]):
                with self.subTest(slot=slot[0]):
                    exit_code, lines, summary = self._run(
                        ["deploy-verify", "--artifact", str(artifact), "--build-command", "make all",
                         "--post-pull-command", "echo apply", *slot]
                    )
                    label = {"--post-pull-command": "Post-pull command #2", "--validation-command": "Validation command #1",
                             "--reboot-command": "Reboot command"}[slot[0]]
                    self._assert_refused(exit_code, lines, summary, "deploy_verify_refused", label, "Shorten that command")
                    # The next round must not lose the build step.
                    self.assertEqual(self._carried(summary)["build_command"], "make all")

    def test_run_refuses_a_validation_command_before_observing(self) -> None:
        with patch.object(cli_main.SerialObserver, "capture", side_effect=AssertionError("observed")):
            exit_code, lines, summary = self._run(
                ["run", "--validation-command", "echo ok", "--validation-command", _TOO_LONG_ONLY_STRUCTURED,
                 "--skip-evidence", "--git-commit", "abc123"]
            )
        self._assert_refused(exit_code, lines, summary, "run_refused", "Validation command #2", "Shorten that command")
        self.assertIs(self._carried(summary)["skip_evidence"], True)
        self.assertEqual(summary["result"]["intervention_context"]["git_commit"], "abc123")

    def test_collect_evidence_refuses_a_shell_command_before_sending_any(self) -> None:
        exit_code, lines, summary = self._run(
            ["collect-evidence", "--shell-command", "echo ok", "--shell-command", _TOO_LONG_ONLY_STRUCTURED,
             "--timeout", "7"]
        )
        self._assert_refused(exit_code, lines, summary, "collect_evidence_refused", "Shell command #2", "Shorten that command")
        self.assertEqual(self._carried(summary)["timeout"], 7.0)

    def test_exec_keeps_its_options(self) -> None:
        exit_code, lines, summary = self._run(["exec", "--shell-command", "echo " + "x" * 2000, "--timeout", "7"])
        self._assert_refused(exit_code, lines, summary, "exec_refused", "The command", "Shorten the command")
        self.assertEqual(self._carried(summary)["timeout"], 7.0)

    def test_device_pull_keeps_its_options_for_the_next_round(self) -> None:
        exit_code, lines, summary = self._run(
            ["device-pull", "--root", "<tmp>", "--mode", "lan_ready", "--check-command", "true",
             "--base-url", "http://10.0.0.2:9000", "--reboot-command", "echo " + "x" * 2000]
        )
        self._assert_refused(exit_code, lines, summary, "device_pull_refused", "Reboot command", "Shorten that command")
        options = summary["result"]["carry_forward_request"]["options"]
        self.assertEqual(options["base_url"], "http://10.0.0.2:9000")
        self.assertIn("root", options)


class DevicePullTransferPlanTest(unittest.TestCase):
    """Which transfer commands device-pull checks before sending anything, by --transfer-mode."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.profiles = _load_lab_profiles()

    def _labels(self, *argv: str) -> list[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            args = cli_main._build_parser().parse_args(["device-pull", "--root", temp_dir, *argv])
            planned = cli_main._device_pull_planned_commands(
                args, [], [], "true", "http://192.168.1.2:8765", "/mnt/sdcard/autodbg", self.profiles
            )
        return [label.split(" (")[0] for label, _, _, _ in planned]

    def test_planned_transfer_commands_per_mode(self) -> None:
        transfer = {"HTTP pull command", "SD HTTP helper command", "Serial bundle prepare command",
                    "Serial bundle upload command", "Serial bundle finalize command"}
        cases = {
            ("--transfer-mode", "http"): {"HTTP pull command"},
            # No HTTP fallback outside auto mode.
            ("--transfer-mode", "sd_http_helper", "--sd-http-helper-path", "/mnt/sdcard/h"): {"SD HTTP helper command"},
            ("--transfer-mode", "serial_bundle"): {"Serial bundle prepare command", "Serial bundle upload command",
                                                   "Serial bundle finalize command"},
            # Auto: checked once the probe has picked the mode, except offline where only the serial bundle is possible.
            ("--transfer-mode", "auto"): set(),
            ("--transfer-mode", "auto", "--mode", "offline"): {"Serial bundle prepare command", "Serial bundle upload command",
                                                               "Serial bundle finalize command"},
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(set(self._labels(*argv)) & transfer, expected)

    def test_every_command_slot_is_checked_with_its_wrapper(self) -> None:
        long = _TOO_LONG_ONLY_STRUCTURED
        cases = {
            "Bootstrap command #1": dict(bootstrap=[long]),
            "Check command #1": dict(checks=[long]),
            "List command": dict(list_command=long),
            "Post-pull command #1": dict(argv=["--post-pull-command", long]),
            "Reboot command": dict(argv=["--reboot-command", long]),
            "Validation command #1": dict(argv=["--validation-command", long]),
        }
        for label, case in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                args = cli_main._build_parser().parse_args(["device-pull", "--root", temp_dir, *case.get("argv", [])])
                planned = cli_main._device_pull_planned_commands(
                    args,
                    case.get("bootstrap", []),
                    case.get("checks", []),
                    case.get("list_command", "true"),
                    "http://192.168.1.2:8765",
                    "/mnt/sdcard/autodbg",
                    self.profiles,
                )
                refusal = cli_main._planned_command_refusal(_controller(), planned)
                self.assertIsNotNone(refusal)
                self.assertTrue(refusal[0].startswith(label), refusal[0])

    def test_auto_mode_does_not_refuse_a_chunk_size_it_may_never_use(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            args = cli_main._build_parser().parse_args(
                ["device-pull", "--root", temp_dir, "--mode", "lan_ready", "--serial-bundle-chunk-size", "4096"]
            )
            planned = cli_main._device_pull_planned_commands(
                args, [], ["true"], "true", "http://192.168.1.2:8765", "/mnt/sdcard/autodbg", self.profiles
            )
        self.assertIsNone(cli_main._planned_command_refusal(_controller(), planned))

    def test_bad_chunk_size_is_left_to_the_transfer(self) -> None:
        for chunk_size in ("0", "-5"):
            with self.subTest(chunk_size=chunk_size):
                self.assertNotIn("Serial bundle upload command", self._labels("--transfer-mode", "serial_bundle",
                                                                                "--serial-bundle-chunk-size", chunk_size))

    def test_auto_mode_refuses_after_the_probe_picks_the_serial_bundle(self) -> None:
        class ProbePort(_FakeShellPort):
            def write(self, data: bytes) -> int:
                text = data.decode("utf-8")
                if "AUTODBG_DOWNLOADER" in text and _MARKER_RE.search(text):  # no downloader: auto picks the bundle
                    self.writes.append(text)
                    marker = _MARKER_RE.search(text).group(0)
                    self.lines += [f"{marker}_BEGIN\n".encode()]
                    self.lines += [line.encode() + b"\n" for line in (
                        "AUTODBG_DOWNLOADER=none", "AUTODBG_BASE64=yes", "AUTODBG_TAR=yes", "AUTODBG_SD_HTTP_HELPER=no")]
                    self.lines += [f"{marker}_END:0\n".encode()]
                    return len(data)
                return super().write(data)

        port = ProbePort()

        @contextlib.contextmanager
        def fake_open(*args, **kwargs):
            yield port

        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            args = cli_main._build_parser().parse_args(
                ["device-pull", "--root", temp_dir, "--mode", "lan_ready", "--check-command", "true",
                 "--serial-bundle-chunk-size", "4096", "--artifacts-root", str(artifacts_root)]
            )
            output = io.StringIO()
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=self.profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "r")),
                patch.object(cli_main, "open_serial_port", fake_open),
                patch.object(controller_module, "open_serial_port", side_effect=AssertionError("second port")),
                patch.object(cli_main, "serve_directory", side_effect=AssertionError("HTTP server started")),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main._dispatch_command(args)
            summary = json.loads((SessionManager(artifacts_root).latest_session_dir() / "summary.json").read_text("utf-8"))
        lines = output.getvalue().splitlines()
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["status"], "device_pull_refused")
        self.assertIs(summary["result"]["retryable"], False)
        self.assertEqual(summary["result"]["failure_stage"], "validate_command")
        self.assertTrue(lines[-1].startswith("[TODO] Stopped before the transfer"), lines[-1])
        sent = port.commands_sent()
        self.assertEqual(len(sent), 2)  # the check and the probe; no prepare, no chunk
        self.assertIn("AUTODBG_DOWNLOADER", sent[-1])

    # Under an 800-byte limit the probe (735) and the helper fit but the HTTP pull with this script name does not; a
    # name long enough to push it over the default limit could not be written to disk at all.
    _FALLBACK_LIMIT = 800
    _LONG_SCRIPT_NAME = "p" * 100 + ".sh"

    def test_fallback_premise(self) -> None:
        from autodbg.cli.transport import _build_remote_pull_command, _build_transfer_probe_command

        controller = _controller(max_line_bytes=self._FALLBACK_LIMIT)
        controller.check_command_length(_build_transfer_probe_command("/mnt/sdcard/autodbg/autodbg-http-pull"))
        with self.assertRaises(CommandTooLongError):
            controller.check_command_length(
                _build_remote_pull_command("http://192.168.1.2:8765", workspace="/mnt/sdcard/autodbg", script_name=self._LONG_SCRIPT_NAME)
            )

    def _sd_helper_pull(self, *, helper_exit: int, argv: list[str]) -> tuple[int, dict, list[str]]:
        """device-pull in auto mode against a device whose probe offers the SD HTTP helper and wget."""
        profiles = copy.deepcopy(self.profiles)
        profiles.device.serial.max_line_bytes = self._FALLBACK_LIMIT

        class HelperPort(_FakeShellPort):
            def write(self, data: bytes) -> int:
                text = data.decode("utf-8")
                match = _MARKER_RE.search(text)
                if match and "AUTODBG_DOWNLOADER" in text:
                    replies, exit_code = ["AUTODBG_DOWNLOADER=wget", "AUTODBG_BASE64=yes", "AUTODBG_TAR=yes",
                                          "AUTODBG_SD_HTTP_HELPER=yes"], 0
                elif match and "autodbg-http-pull" in text:
                    replies, exit_code = ["helper ran"], helper_exit
                else:
                    return super().write(data)
                self.writes.append(text)
                marker = match.group(0)
                self.lines += [f"{marker}_BEGIN\n".encode(), *[line.encode() + b"\n" for line in replies],
                               f"{marker}_END:{exit_code}\n".encode()]
                return len(data)

        class Stoppable:
            def shutdown(self) -> None: ...
            def server_close(self) -> None: ...
            def join(self, timeout=None) -> None: ...
            def is_alive(self) -> bool:
                return False

        port = HelperPort()

        @contextlib.contextmanager
        def fake_open(*args, **kwargs):
            yield port

        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "payload.bin").write_bytes(b"x")
            artifacts_root = Path(temp_dir) / "artifacts"
            args = cli_main._build_parser().parse_args(
                ["device-pull", "--root", temp_dir, "--mode", "lan_ready", "--check-command", "true",
                 "--base-url", "http://192.168.1.2:8765", *argv, "--artifacts-root", str(artifacts_root)]
            )
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "r")),
                patch.object(cli_main, "open_serial_port", fake_open),
                patch.object(controller_module, "open_serial_port", side_effect=AssertionError("second port")),
                patch.object(cli_main, "serve_directory", return_value=(Stoppable(), Stoppable())),
                patch("sys.stdout", io.StringIO()),
            ):
                exit_code = cli_main._dispatch_command(args)
            summary = json.loads((SessionManager(artifacts_root).latest_session_dir() / "summary.json").read_text("utf-8"))
        return exit_code, summary, port.commands_sent()

    def test_a_long_http_fallback_does_not_block_a_working_sd_helper(self) -> None:
        exit_code, summary, sent = self._sd_helper_pull(helper_exit=0, argv=["--pull-script-name", self._LONG_SCRIPT_NAME])
        self.assertNotEqual(summary["status"], "device_pull_refused")
        self.assertEqual(summary["selected_transfer_mode"], "sd_http_helper")
        self.assertTrue(any("autodbg-http-pull" in command for command in sent))

    def test_a_long_http_fallback_is_skipped_and_the_helper_failure_reported(self) -> None:
        exit_code, summary, sent = self._sd_helper_pull(helper_exit=1, argv=["--pull-script-name", self._LONG_SCRIPT_NAME])
        self.assertEqual(exit_code, 1)
        self.assertNotEqual(summary["status"], "device_pull_refused")
        self.assertIn("HTTP fallback pull command", summary["transfer_details"]["http_fallback_skipped"])
        self.assertFalse(any(self._LONG_SCRIPT_NAME in command for command in sent))


class RefusalReportingTest(unittest.TestCase):
    def test_structured_commands_are_measured_with_their_wrapper(self) -> None:
        command = "echo " + "x" * 800  # fits alone, not inside _build_structured_command
        self.assertIsNone(cli_main._planned_command_refusal(_controller(), [("c", command, False, "a")]))
        self.assertIsNotNone(cli_main._planned_command_refusal(_controller(), [("c", command, True, "a")]))

    def test_result_excerpt_shows_the_refusal(self) -> None:
        self.assertEqual(cli_main._result_excerpt({"output_lines": [], "error": "too long"}), "too long")
        self.assertEqual(cli_main._result_excerpt({"output_lines": ["fine"]}), "fine")

    def test_validation_finding_names_the_reason(self) -> None:
        profiles = copy.deepcopy(_load_lab_profiles())
        profiles.device.serial.max_line_bytes = 300
        with tempfile.TemporaryDirectory() as temp_dir:
            session = SessionManager(Path(temp_dir) / "artifacts", Path(temp_dir) / "r").create("lab", "check")
            collector = EvidenceCollector(session)
            collector.bootstrap(profiles=profiles, state_snapshot=cli_main.StateSnapshot(), workflow_name="check",
                                workflow_steps=[], plan_details={"control": {"mode": "test"}})
            results = cli_main._run_validation_commands(
                controller=_controller(max_line_bytes=300),
                collector=collector,
                commands=["echo " + "x" * 100],  # fits as is, not with the structured wrapper under 300
                timeout=2.0,
                serial_port=_FakeShellPort(),
                prefix="validation",
            )
        self.assertIn("300-byte line limit", results[0]["error"])
        spec = cli_main._evaluate_validation_spec(
            expect_markers=[],
            reject_markers=[],
            expected_version=None,
            observation=None,
            command_results=results,
        )
        self.assertTrue(any("300-byte line limit" in item["message"] for item in spec["findings"]), spec["findings"])

    def test_fetch_refusals_carry_fetch_advice(self) -> None:
        from autodbg.cli.transport import _fetch_remote_path_artifact

        for fetch in (_fetch_remote_file_artifact, _fetch_remote_path_artifact):
            with self.subTest(fetch=fetch.__name__), tempfile.TemporaryDirectory() as temp_dir:
                port = _FakeShellPort()
                session = SessionManager(Path(temp_dir) / "artifacts", Path(temp_dir) / "r").create("lab", "check")
                summary = fetch(
                    controller=_controller(max_line_bytes=200),
                    collector=EvidenceCollector(session),
                    remote_path="/var/log/messages",
                    timeout=2.0,
                    prefix="fetch",
                    serial_port=port,
                )
                self.assertIs(summary["sent"], False)
                self.assertIn("Fetch a shorter path", summary["error"])
                self.assertEqual(port.writes, [])

    def test_wlan_hint_only_for_the_builtin_bootstrap(self) -> None:
        from autodbg.cli.transport import _build_auto_wlan_bootstrap_commands

        wlan = _build_auto_wlan_bootstrap_commands(wifi_ssid="s", wifi_password="p", wifi_mode=None, network_dir=None)
        planned = cli_main._bootstrap_commands(["echo mine", *wlan])
        self.assertNotIn("--network-dir", planned[0][3])
        self.assertTrue(all("--network-dir" in advice for _, _, _, advice in planned[1:]))

    def test_lowered_limit_refusals_are_printed_by_collect_evidence(self) -> None:
        profiles = copy.deepcopy(_load_lab_profiles())
        profiles.device.serial.max_line_bytes = 300  # below the built-in evidence commands
        port = _FakeShellPort()

        @contextlib.contextmanager
        def fake_open(*args, **kwargs):
            yield port

        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            args = cli_main._build_parser().parse_args(["collect-evidence", "--artifacts-root", str(artifacts_root)])
            output = io.StringIO()
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "r")),
                patch.object(cli_main, "open_serial_port", fake_open),
                patch.object(controller_module, "open_serial_port", side_effect=AssertionError("second port")),
                patch("sys.stdout", output),
            ):
                exit_code = cli_main._dispatch_command(args)
            session_dir = SessionManager(artifacts_root).latest_session_dir()
            summary = json.loads((session_dir / "summary.json").read_text("utf-8"))
            events = [json.loads(line) for line in (session_dir / "events.jsonl").read_text("utf-8").splitlines() if line]
        refused = [line for line in output.getvalue().splitlines() if "300-byte line limit" in line]
        self.assertEqual(exit_code, 1)
        self.assertTrue(refused and all(line.startswith("[ERROR] ") for line in refused), output.getvalue())
        # The excerpts and events say why, not "(no output)" / "completed".
        self.assertTrue(any("300-byte line limit" in item["text"] for item in summary["result"]["key_excerpts"]))
        refused_events = [event for event in events if event.get("summary", "").startswith("Refused command ")]
        self.assertTrue(refused_events and all(event["severity"] == "error" for event in refused_events))

    def test_health_shows_why_a_check_was_not_run(self) -> None:
        profiles = copy.deepcopy(_load_lab_profiles())
        profiles.device.serial.max_line_bytes = 300

        @contextlib.contextmanager
        def fake_open(*args, **kwargs):
            yield _FakeShellPort()

        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            args = cli_main._build_parser().parse_args(["health", "--artifacts-root", str(artifacts_root)])
            output = io.StringIO()
            with (
                patch.object(cli_main, "_load_profiles_from_args", return_value=profiles),
                patch.object(cli_main, "_session_manager", return_value=SessionManager(artifacts_root, Path(temp_dir) / "r")),
                patch.object(cli_main, "open_serial_port", fake_open),
                patch.object(controller_module, "open_serial_port", side_effect=AssertionError("second port")),
                patch("sys.stdout", output),
            ):
                cli_main._dispatch_command(args)
        self.assertTrue(
            any("exit=None | " in line and "300-byte line limit" in line for line in output.getvalue().splitlines()),
            output.getvalue(),
        )


class NetworkDirResolverTest(unittest.TestCase):
    """The wlan bootstrap's search for the network scripts, run in a POSIX sh against a scratch tree."""

    _SH = shutil.which("sh") or (
        str(Path(shutil.which("git")).resolve().parents[1] / "bin" / "sh.exe") if shutil.which("git") else None
    )

    def _resolve(self, root: Path) -> tuple[int, str]:
        from autodbg.cli.transport import _build_network_dir_resolver

        script = _build_network_dir_resolver(None)
        self.assertNotIn("head", script)  # the V35S busybox has no head
        candidates = re.search(r"for d in (.*?); do", script).group(1).split()
        relocated = " ".join(f"{root.as_posix()}{candidate}" for candidate in candidates)  # never the host's own dirs
        script = script.replace(f"for d in {' '.join(candidates)}; do", f"for d in {relocated}; do")
        script = script.replace("find / ", f"find {(root / 'scan').as_posix()} ")
        # A POSIX find first: with Git's usr\bin\sh on PATH after System32, "find" would be Windows' find.exe.
        sh_dir = Path(self._SH).parent
        posix_dirs = [str(path) for path in (sh_dir, sh_dir.parent / "usr" / "bin") if path.is_dir()]
        env = {**os.environ, "PATH": os.pathsep.join(posix_dirs + [os.environ.get("PATH", "")])}
        done = subprocess.run([self._SH, "-c", script + ' && printf "%s" "$NET_DIR"'], capture_output=True, timeout=30, env=env)
        return done.returncode, done.stdout.decode()

    def _touch(self, directory: Path, *names: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            (directory / name).write_text("")

    def setUp(self) -> None:
        if not self._SH or not Path(self._SH).exists():
            self.skipTest("needs a POSIX sh")

    def test_first_candidate_with_both_scripts_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._touch(root / "opt/network", "wifi_cmd.sh")  # only one of the two scripts
            self._touch(root / "mnt/sdcard/network", "wifi_cmd.sh", "wlan_run.sh")
            self._touch(root / "mnt/sdcard/autodbg/network", "wifi_cmd.sh", "wlan_run.sh")
            self.assertEqual(self._resolve(root), (0, f"{root.as_posix()}/mnt/sdcard/network"))

    def test_falls_back_to_find(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._touch(root / "scan/app/network", "wifi_cmd.sh", "wlan_run.sh")
            self.assertEqual(self._resolve(root), (0, f"{(root / 'scan/app/network').as_posix()}"))

    def test_fails_when_nothing_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "scan").mkdir()
            self.assertNotEqual(self._resolve(root)[0], 0)


if __name__ == "__main__":
    unittest.main()
