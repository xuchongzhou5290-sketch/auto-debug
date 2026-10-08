import re
import unittest

from autodbg.control.controller import DeviceController
from autodbg.models.profile import (
    Credentials,
    DeviceProfile,
    ModelProfile,
    SerialSettings,
    TransportProfile,
)


_MARKER_RE = re.compile(r"__AUTODBG_[0-9A-F]{8}__")


class FakeSerialPort:
    def __init__(self) -> None:
        self.timeout = 0.1
        self.lines: list[bytes] = [b"closeli login:\n"]
        self.writes: list[str] = []

    def readline(self) -> bytes:
        if self.lines:
            return self.lines.pop(0)
        return b""

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8")
        self.writes.append(text)
        stripped = text.strip()
        if stripped == "root":
            self.lines.append(b"[root@closeli:~]#\n")
        elif _MARKER_RE.search(stripped) and "; ls /mnt/sdcard; " in stripped:
            marker = _MARKER_RE.search(stripped).group(0)
            begin_marker = f"{marker}_BEGIN"
            end_prefix = f"{marker}_END:"
            self.lines.append(begin_marker.encode("utf-8") + b"\n")
            self.lines.append(b"LeCam_debug\n")
            self.lines.append(b"core-LeCamCore-607-1775822008\n")
            self.lines.append((end_prefix + "0").encode("utf-8") + b"\n")
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeSerialPortChangedCwd(FakeSerialPort):
    def write(self, data: bytes) -> int:
        text = data.decode("utf-8")
        self.writes.append(text)
        stripped = text.strip()
        if stripped == "root":
            self.lines.append(b"[root@closeli:autodbg]#\n")
        elif _MARKER_RE.search(stripped) and "; ls /mnt/sdcard; " in stripped:
            marker = _MARKER_RE.search(stripped).group(0)
            begin_marker = f"{marker}_BEGIN"
            end_prefix = f"{marker}_END:"
            self.lines.append(begin_marker.encode("utf-8") + b"\n")
            self.lines.append(b"LeCam_debug\n")
            self.lines.append((end_prefix + "0").encode("utf-8") + b"\n")
        return len(data)


class FakeSerialPortWrappedEcho(FakeSerialPort):
    """Echoes the typed line the way the device's busybox line editor does: after the prompt, broken every 80 columns."""

    PROMPT = "[root@closeli:~]# "

    def write(self, data: bytes) -> int:
        text = data.decode("utf-8")
        self.writes.append(text)
        stripped = text.strip()
        if stripped == "root":
            self.lines.append(self.PROMPT.rstrip().encode("utf-8") + b"\n")
        elif _MARKER_RE.search(stripped):
            echo = self.PROMPT + stripped
            self.lines.extend((echo[i : i + 80] + "\r\n").encode("utf-8") for i in range(0, len(echo), 80))
            marker = _MARKER_RE.search(stripped).group(0)
            self.lines.append(f"{marker}_BEGIN\r\n".encode("utf-8"))
            self.lines.append(b"LeCam_debug\r\n")
            self.lines.append(f"{marker}_END:0\r\n".encode("utf-8"))
        return len(data)


def _controller() -> DeviceController:
    return DeviceController(
        device_profile=DeviceProfile(
            device_id="av130n-lab",
            model_id="ak-av130n-ucm55me2",
            serial=SerialSettings(
                port="COM16",
                baudrate=115200,
                login_prompt="closeli login:",
                shell_prompt="[root@closeli:~]#",
            ),
            credentials=Credentials(username="root"),
        ),
        model_profile=ModelProfile(model_id="ak-av130n-ucm55me2", platform="AK_AV130N", app_name="LeCam"),
        transport_profile=TransportProfile(transport_id="network_serial_fallback", control_channels=["serial"]),
    )


class DeviceControllerTest(unittest.TestCase):
    def test_wrapped_echo_of_the_command_is_not_taken_for_its_end_marker(self) -> None:
        # Command lengths cover every column the end marker's echo can start at; with the marker typed literally,
        # one length in 80 put "<marker>_END:%s\n' $?" at the start of an echo row and the result was exit code None.
        for length in range(40, 200):
            with self.subTest(length=length):
                command = "ls /mnt/sdcard/" + "x" * (length - len("ls /mnt/sdcard/"))
                result = _controller().execute(command, serial_port=FakeSerialPortWrappedEcho(), timeout=2.0)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.output_lines, ["LeCam_debug"])

    def test_execute_over_fake_serial(self) -> None:
        controller = DeviceController(
            device_profile=DeviceProfile(
                device_id="av130n-lab",
                model_id="ak-av130n-ucm55me2",
                serial=SerialSettings(
                    port="COM16",
                    baudrate=115200,
                    login_prompt="closeli login:",
                    shell_prompt="[root@closeli:~]#",
                ),
                credentials=Credentials(username="root"),
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
            ),
            transport_profile=TransportProfile(
                transport_id="network_serial_fallback",
                control_channels=["serial"],
            ),
        )

        result = controller.execute("ls /mnt/sdcard", serial_port=FakeSerialPort(), timeout=2.0)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("LeCam_debug", result.output_lines)
        self.assertIn("core-LeCamCore-607-1775822008", result.output_lines)

    def test_execute_accepts_shell_prompt_with_changed_cwd(self) -> None:
        controller = DeviceController(
            device_profile=DeviceProfile(
                device_id="av130n-lab",
                model_id="ak-av130n-ucm55me2",
                serial=SerialSettings(
                    port="COM16",
                    baudrate=115200,
                    login_prompt="closeli login:",
                    shell_prompt="[root@closeli:~]#",
                ),
                credentials=Credentials(username="root"),
            ),
            model_profile=ModelProfile(
                model_id="ak-av130n-ucm55me2",
                platform="AK_AV130N",
                app_name="LeCam",
            ),
            transport_profile=TransportProfile(
                transport_id="network_serial_fallback",
                control_channels=["serial"],
            ),
        )

        result = controller.execute("ls /mnt/sdcard", serial_port=FakeSerialPortChangedCwd(), timeout=2.0)

        self.assertEqual(result.exit_code, 0)
        self.assertIn("LeCam_debug", result.output_lines)


if __name__ == "__main__":
    unittest.main()
