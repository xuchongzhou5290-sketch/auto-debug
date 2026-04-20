import unittest

from autodbg.control.controller import DeviceController
from autodbg.models.profile import (
    Credentials,
    DeviceProfile,
    ModelProfile,
    SerialSettings,
    TransportProfile,
)


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
        elif stripped.startswith("printf '__AUTODBG_") and "; ls /mnt/sdcard; " in stripped:
            begin_marker = stripped.split("printf '", 1)[1].split("\\n';", 1)[0]
            end_prefix = stripped.rsplit("printf '", 1)[1].split("%s", 1)[0]
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
        elif stripped.startswith("printf '__AUTODBG_") and "; ls /mnt/sdcard; " in stripped:
            begin_marker = stripped.split("printf '", 1)[1].split("\\n';", 1)[0]
            end_prefix = stripped.rsplit("printf '", 1)[1].split("%s", 1)[0]
            self.lines.append(begin_marker.encode("utf-8") + b"\n")
            self.lines.append(b"LeCam_debug\n")
            self.lines.append((end_prefix + "0").encode("utf-8") + b"\n")
        return len(data)


class DeviceControllerTest(unittest.TestCase):
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
