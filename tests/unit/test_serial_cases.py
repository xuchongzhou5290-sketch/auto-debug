from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodbg.serial import runtime
from autodbg.serial.cases import begin_serial_case, capture_serial_case, end_serial_case


class SerialCasesTest(unittest.TestCase):
    def test_begin_and_end_serial_case_writes_slice_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                runtime.append_serial_trace_marker("COM19", "before case")
                started = begin_serial_case("COM19", "wifi case 001", title="WiFi reconnect")
                runtime.append_serial_trace_marker("COM19", "case body line")
                ended = end_serial_case("COM19", "wifi case 001", result="pass")

                self.assertTrue(started.metadata_path.exists())
                self.assertTrue(ended.trace_jsonl_path.exists())
                self.assertTrue(ended.trace_text_path.exists())
                self.assertTrue(ended.evidence_patch_path.exists())
                self.assertIn("CASE_START", ended.trace_text_path.read_text(encoding="utf-8"))
                self.assertIn("case body line", ended.trace_text_path.read_text(encoding="utf-8"))
                self.assertIn("Serial Evidence Patch", ended.evidence_patch_path.read_text(encoding="utf-8"))
                self.assertEqual(ended.metadata["result"], "pass")

    def test_capture_serial_case_can_focus_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                begin_serial_case("COM19", "panic_case", title="Panic")
                runtime.append_serial_trace_marker("COM19", "line before")
                runtime.append_serial_trace_marker("COM19", "panic happened")
                runtime.append_serial_trace_marker("COM19", "line after")
                end_serial_case("COM19", "panic_case", result="fail")
                captured = capture_serial_case("COM19", "panic_case", focus=["happened"], before=1, after=1)

                patch_text = captured.evidence_patch_path.read_text(encoding="utf-8")
                self.assertIn("line before", patch_text)
                self.assertIn("panic happened", patch_text)
                self.assertIn("line after", patch_text)
                self.assertEqual(captured.selected_lines, 3)

    def test_begin_serial_case_refuses_duplicate_active_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                begin_serial_case("COM19", "wifi_case_001", title="WiFi")
                with self.assertRaises(FileExistsError):
                    begin_serial_case("COM19", "wifi_case_001", title="WiFi again")


if __name__ == "__main__":
    unittest.main()
