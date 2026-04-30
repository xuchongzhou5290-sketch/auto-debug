from pathlib import Path
import tempfile
import unittest

from autodbg.models.profile import ModelProfile, SerialSettings
from autodbg.serial.observer import SerialObserver


class FakeSerialPort:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]
        self.timeout = 0.1
        self.writes: list[str] = []

    def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data.decode("utf-8"))
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class SerialObserverTest(unittest.TestCase):
    def test_capture_detects_markers(self) -> None:
        observer = SerialObserver(
            serial_settings=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
                app_start_markers=["LeCam goto start..."],
                app_ready_markers=["LeCamCoreStart"],
                panic_markers=["start coredump..."],
            ),
        )

        fake_port = FakeSerialPort(
            [
                "U-Boot 2019.10.0",
                "closeli login:",
                "LeCamCoreStart",
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            result = observer.capture(
                seconds=0.1,
                log_path=Path(temp_dir) / "serial.log",
                serial_port=fake_port,
                max_lines=3,
            )

        self.assertEqual(result.lines_captured, 3)
        self.assertEqual(result.last_device_state, "app_ready")
        self.assertEqual(result.marker_hits[0].tag, "boot")
        self.assertEqual(result.marker_hits[0].device_state, "bootloader")
        self.assertEqual(result.marker_hits[-1].tag, "app_ready")
        self.assertEqual(result.marker_hits[-1].device_state, "app_ready")

    def test_capture_invokes_line_callback(self) -> None:
        observer = SerialObserver(
            serial_settings=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
                app_start_markers=["LeCam goto start..."],
                app_ready_markers=["LeCamCoreStart"],
                panic_markers=["start coredump..."],
            ),
        )
        fake_port = FakeSerialPort(["boot line", "LeCamCoreStart"])
        seen: list[tuple[str, str | None]] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            result = observer.capture(
                seconds=0.1,
                log_path=Path(temp_dir) / "serial.log",
                serial_port=fake_port,
                max_lines=2,
                line_callback=lambda line, hit: seen.append((line, hit.device_state if hit else None)),
            )

        self.assertFalse(result.interrupted)
        self.assertEqual(seen[0], ("boot line", None))
        self.assertEqual(seen[1], ("LeCamCoreStart", "app_ready"))

    def test_capture_builds_marker_windows_and_verdict(self) -> None:
        observer = SerialObserver(
            serial_settings=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
                success_markers=["fw verify ok"],
                fatal_markers=["hard fault"],
                marker_context_lines=1,
            ),
        )
        fake_port = FakeSerialPort(["boot line", "fw verify ok", "ready line", "HARD FAULT at pc", "after fault"])

        with tempfile.TemporaryDirectory() as temp_dir:
            result = observer.capture(
                seconds=0.1,
                log_path=Path(temp_dir) / "serial.log",
                serial_port=fake_port,
                max_lines=5,
            )

        self.assertEqual(result.marker_verdict, "fatal")
        self.assertEqual([window.kind for window in result.marker_windows], ["success", "fatal"])
        self.assertEqual(result.marker_windows[0].before, ["boot line"])
        self.assertEqual(result.marker_windows[0].after, ["ready line"])
        self.assertEqual(result.marker_windows[1].before, ["ready line"])
        self.assertEqual(result.marker_windows[1].after, ["after fault"])
        self.assertEqual(result.marker_hits[0].tag, "success")
        self.assertEqual(result.marker_hits[-1].tag, "fatal")
        self.assertEqual(result.to_dict()["marker_verdict"], "fatal")

    def test_capture_marks_interrupted_when_callback_stops(self) -> None:
        observer = SerialObserver(
            serial_settings=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
                app_start_markers=["LeCam goto start..."],
                app_ready_markers=["LeCamCoreStart"],
                panic_markers=["start coredump..."],
            ),
        )
        fake_port = FakeSerialPort(["boot line", "LeCamCoreStart"])
        callback_count = 0

        def stop_after_first_line(line: str, hit) -> None:
            nonlocal callback_count
            callback_count += 1
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as temp_dir:
            result = observer.capture(
                seconds=0.1,
                log_path=Path(temp_dir) / "serial.log",
                serial_port=fake_port,
                line_callback=stop_after_first_line,
            )

        self.assertTrue(result.interrupted)
        self.assertEqual(callback_count, 1)
        self.assertEqual(result.lines_captured, 1)

    def test_capture_can_send_startup_lines(self) -> None:
        observer = SerialObserver(
            serial_settings=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
                app_start_markers=["LeCam goto start..."],
                app_ready_markers=["LeCamCoreStart"],
                panic_markers=["start coredump..."],
            ),
        )
        fake_port = FakeSerialPort(["closeli login:"])

        with tempfile.TemporaryDirectory() as temp_dir:
            observer.capture(
                seconds=0.1,
                log_path=Path(temp_dir) / "serial.log",
                serial_port=fake_port,
                max_lines=1,
                startup_lines=[""],
            )

        self.assertEqual(fake_port.writes[0], "\n")

    def test_classify_line_accepts_root_prompt_with_changed_cwd(self) -> None:
        observer = SerialObserver(
            serial_settings=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
            ),
        )

        hit = observer.classify_line("[root@closeli:autodbg]# ")

        self.assertIsNotNone(hit)
        self.assertEqual(hit.tag, "shell")
        self.assertEqual(hit.device_state, "root_shell")


if __name__ == "__main__":
    unittest.main()
