"""COM1~COM9 (and CON/PRN/AUX/NUL/LPT1~9) are Windows device names even with an extension or as a directory:
"com7.json" opens the COM7 device, "COM7" cannot be a directory and "nul.json" swallows writes. The broker's registry,
port lock and trace directory for those ports get a "_port" suffix; every other port keeps its old file names.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autodbg.serial import broker, runtime


@contextmanager
def _dirs():
    """Private registry / lock / control-lock / trace roots (never the real %TEMP% ones live brokers use)."""
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        dirs = {name: root / name for name in ("brokers", "locks", "control", "trace")}
        for path in dirs.values():
            path.mkdir()
        with (
            patch.object(runtime, "_serial_broker_dir", return_value=dirs["brokers"]),
            patch.object(runtime, "_serial_lock_dir", return_value=dirs["locks"]),
            patch.object(runtime, "_serial_control_lock_dir", return_value=dirs["control"]),
            patch.object(runtime, "_serial_trace_dir", return_value=dirs["trace"]),
        ):
            yield dirs


class _FakeSerial:
    def __init__(self, *, port: str, baudrate: int, timeout: float) -> None:
        self.port = port
        self.closed = False
        self.written: list[bytes] = []

    def readline(self) -> bytes:
        time.sleep(0.05)
        return b""

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _is_dos_device(name: str) -> bool:
    """String-level check (ntdll RtlIsDosDeviceName_U): never opens a device, unlike trying to create the file."""
    import ctypes

    check = ctypes.WinDLL("ntdll").RtlIsDosDeviceName_U
    check.argtypes = [ctypes.c_wchar_p]
    check.restype = ctypes.c_ulong
    return check(name) != 0


class WindowsReservedPortNameTest(unittest.TestCase):
    def test_suffix_goes_after_the_stem(self) -> None:
        cases = {
            "com7": "com7_port",
            "COM9": "COM9_port",
            "lpt1": "lpt1_port",
            "nul": "nul_port",
            "conin$": "conin$_port",
            "com\u00b9": "com\u00b9_port",
            "COM7.json": "COM7_port.json",
            "com7.": "com7_port.",
            "nul ": "nul_port",
        }
        for name, expected in cases.items():
            with self.subTest(name):
                self.assertEqual(runtime._windows_safe_name(name), expected)

    def test_other_ports_keep_their_old_names(self) -> None:
        for name in ("com10", "COM31", "com19", "COM100", "com0", "_dev_ttyusb0", "ttyS0", "console", "comx", "lpt10"):
            with self.subTest(name):
                self.assertEqual(runtime._windows_safe_name(name), name)

    @unittest.skipUnless(os.name == "nt", "Windows device-name semantics")
    def test_old_names_were_devices_new_names_are_not(self) -> None:
        ports = [f"COM{i}" for i in range(1, 10)] + ["LPT1", "CON", "PRN", "AUX", "NUL", "CONIN$", "COM7."]
        with _dirs():
            for port in ports:
                with self.subTest(port):
                    self.assertTrue(_is_dos_device(f"{port.lower()}.json") or _is_dos_device(port.upper()))
                    for path in (
                        runtime.serial_broker_registry_path(port),
                        runtime._serial_lock_path(port),
                        runtime._serial_control_lock_path(port),
                        runtime.serial_trace_log_path(port).parent,
                    ):
                        self.assertFalse(_is_dos_device(path.name), str(path))

    def test_lock_paths_stay_inside_the_lock_directory(self) -> None:
        # used to be port.lower(): "\\.\COM7" pointed into the device namespace, "/dev/ttyUSB0" was an absolute path
        with _dirs() as dirs:
            for port in ("\\\\.\\COM7", "\\\\.\\COM10", "/dev/ttyUSB0", "COM7:"):
                with self.subTest(port):
                    self.assertEqual(runtime._serial_lock_path(port).parent, dirs["locks"])
                    self.assertEqual(runtime._serial_control_lock_path(port).parent, dirs["control"])

    def test_reserved_case_ids_work(self) -> None:
        from autodbg.serial import cases

        with _dirs() as dirs:
            for case_id in ("nul", "com7", "CON"):
                with self.subTest(case_id):
                    cases.begin_serial_case("COM10", case_id)
                    active = dirs["trace"] / "COM10" / "cases" / ".active" / f"{runtime._windows_safe_name(case_id)}.json"
                    self.assertTrue(active.is_file())
                    cases.end_serial_case("COM10", case_id, result="pass")
                    self.assertFalse(active.exists())

    def test_paths_for_reserved_and_regular_ports(self) -> None:
        with _dirs() as dirs:
            self.assertEqual(runtime.serial_broker_registry_path("COM7"), dirs["brokers"] / "com7_port.json")
            self.assertEqual(runtime._serial_lock_path("COM7"), dirs["locks"] / "com7_port.lock")
            self.assertEqual(runtime._serial_control_lock_path("COM7"), dirs["control"] / "com7_port.lock")
            self.assertEqual(runtime.serial_trace_log_path("COM7"), dirs["trace"] / "COM7_port" / "trace.jsonl")
            # unchanged for the ports that always worked, so other autodbg versions still find them
            self.assertEqual(runtime.serial_broker_registry_path("COM31"), dirs["brokers"] / "com31.json")
            self.assertEqual(runtime._serial_lock_path("COM31"), dirs["locks"] / "com31.lock")
            self.assertEqual(runtime._serial_control_lock_path("COM31"), dirs["control"] / "com31.lock")
            self.assertEqual(runtime.serial_trace_log_path("COM31"), dirs["trace"] / "COM31" / "trace.jsonl")

    def test_registry_round_trip_on_com7(self) -> None:
        with _dirs() as dirs:
            runtime.write_serial_broker_registry("COM7", host="127.0.0.1", tcp_port=6731, baudrate=115200)
            self.assertTrue((dirs["brokers"] / "com7_port.json").is_file())
            loaded = runtime.load_serial_broker_registry("COM7")
            self.assertIsNotNone(loaded)
            self.assertEqual((loaded.serial_port, loaded.pid), ("COM7", os.getpid()))
            self.assertEqual([r.serial_port for r in runtime.list_serial_broker_registries(serial_port="COM7")], ["COM7"])
            runtime.remove_serial_broker_registry("COM7", owner_pid=os.getpid())
            self.assertFalse((dirs["brokers"] / "com7_port.json").exists())

    def test_port_lock_and_trace_on_com7(self) -> None:
        with _dirs() as dirs:
            with runtime._acquire_port_lock("COM7"):
                self.assertEqual((dirs["locks"] / "com7_port.lock").read_text(encoding="ascii"), str(os.getpid()))
                with self.assertRaises(runtime.SerialPortBusyError):
                    with runtime._acquire_port_lock("com7"):
                        pass
            self.assertFalse((dirs["locks"] / "com7_port.lock").exists())
            runtime.append_serial_trace_marker("COM7", "MARK")
            self.assertIn("MARK", (dirs["trace"] / "COM7_port" / "trace.jsonl").read_text(encoding="utf-8"))

    def test_broker_starts_and_stops_on_com7(self) -> None:
        with _dirs() as dirs:
            instance = broker.SerialBroker(serial_port="COM7", baudrate=115200, owner="human-observe", protected=True)
            with patch("autodbg.serial.broker._import_serial", return_value=SimpleNamespace(Serial=_FakeSerial)):
                instance.start()
                try:
                    registry = runtime.load_serial_broker_registry("COM7")
                    self.assertIsNotNone(registry)
                    self.assertTrue(registry.protected)
                    self.assertTrue((dirs["locks"] / "com7_port.lock").is_file())
                    self.assertTrue(runtime._probe_serial_broker(registry))
                    self.assertTrue((dirs["trace"] / "COM7_port" / "trace.jsonl").is_file())
                finally:
                    instance.stop()
            self.assertFalse((dirs["brokers"] / "com7_port.json").exists())
            self.assertFalse((dirs["locks"] / "com7_port.lock").exists())
            self.assertEqual([t for t in threading.enumerate() if t.name.startswith("Thread") and not t.daemon], [])


if __name__ == "__main__":
    unittest.main()
