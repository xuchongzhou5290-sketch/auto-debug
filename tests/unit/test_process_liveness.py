import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autodbg.utils import process


class PidIsRunningTest(unittest.TestCase):
    def test_non_positive_pid_is_not_running(self) -> None:
        self.assertFalse(process.pid_is_running(0))
        self.assertFalse(process.pid_is_running(-5))

    def test_current_process_is_running(self) -> None:
        self.assertTrue(process.pid_is_running(os.getpid()))

    def test_exited_child_is_not_running(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=30)
        self.assertFalse(process.pid_is_running(child.pid))

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_access_denied_counts_as_running(self) -> None:
        # e.g. the caller runs in a sandbox / with another token: the broker is alive, we just may not query it
        with patch.object(process, "_open_process", return_value=(None, 5)):
            self.assertTrue(process.pid_is_running(4242))

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_invalid_parameter_means_no_such_process(self) -> None:
        with patch.object(process, "_open_process", return_value=(None, 87)):
            self.assertFalse(process.pid_is_running(4242))

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_unknown_open_error_counts_as_running(self) -> None:
        with patch.object(process, "_open_process", return_value=(None, 1450)):
            self.assertTrue(process.pid_is_running(4242))

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_exit_code_query_failure_counts_as_running(self) -> None:
        with (
            patch.object(process, "_open_process", return_value=(1234, 0)),
            patch.object(process, "_get_exit_code", return_value=None),
            patch.object(process, "_kernel32", return_value=SimpleNamespace(CloseHandle=lambda _handle: True)),
        ):
            self.assertTrue(process.pid_is_running(4242))

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_exited_process_with_open_handle_is_not_running(self) -> None:
        with (
            patch.object(process, "_open_process", return_value=(1234, 0)),
            patch.object(process, "_get_exit_code", return_value=0),
            patch.object(process, "_kernel32", return_value=SimpleNamespace(CloseHandle=lambda _handle: True)),
        ):
            self.assertFalse(process.pid_is_running(4242))

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_pid_state_distinguishes_unknown_from_dead(self) -> None:
        with patch.object(process, "_open_process", return_value=(None, 5)):
            self.assertEqual(process.pid_state(4242), process.PID_UNKNOWN)
        with patch.object(process, "_open_process", return_value=(None, 87)):
            self.assertEqual(process.pid_state(4242), process.PID_DEAD)
        self.assertEqual(process.pid_state(os.getpid()), process.PID_ALIVE)

    @unittest.skipUnless(os.name == "nt", "Windows OpenProcess semantics")
    def test_windows_pid_beyond_32_bits_is_dead_not_truncated(self) -> None:
        # DWORD argtypes would silently truncate 2**32 + our pid to our own (alive) pid
        with patch.object(process, "_open_process", side_effect=AssertionError("must not reach OpenProcess")):
            self.assertFalse(process.pid_is_running(2**32 + os.getpid()))

    def test_posix_other_os_error_is_unknown(self) -> None:
        def fake_kill(_pid: int, _sig: int) -> None:
            raise OSError(22, "Invalid argument")

        with patch.object(process, "os", SimpleNamespace(name="posix", kill=fake_kill)):
            self.assertEqual(process.pid_state(4242), process.PID_UNKNOWN)
            self.assertTrue(process.pid_is_running(4242))

    def test_posix_permission_error_counts_as_running(self) -> None:
        def fake_kill(_pid: int, _sig: int) -> None:
            raise PermissionError(1, "Operation not permitted")

        with patch.object(process, "os", SimpleNamespace(name="posix", kill=fake_kill)):
            self.assertTrue(process.pid_is_running(4242))

    def test_posix_process_lookup_error_means_gone(self) -> None:
        def fake_kill(_pid: int, _sig: int) -> None:
            raise ProcessLookupError(3, "No such process")

        with patch.object(process, "os", SimpleNamespace(name="posix", kill=fake_kill)):
            self.assertFalse(process.pid_is_running(4242))


if __name__ == "__main__":
    unittest.main()
