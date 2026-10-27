import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodbg.serial import runtime
from autodbg.serial.cases import _case_trace_entries, begin_serial_case, capture_serial_case, end_serial_case


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

    def test_begin_serial_case_does_not_scan_the_existing_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                trace_path = runtime.serial_trace_log_path("COM19")
                # history that cannot even be decoded: counting its lines used to be required to begin a case
                trace_path.write_bytes(b"\xff\xfe old history\n" * 1000)
                history_size = trace_path.stat().st_size
                started = begin_serial_case("COM19", "offset_case")
                runtime.append_serial_trace_marker("COM19", "inside the case")
                ended = end_serial_case("COM19", "offset_case", result="pass")

                self.assertEqual(started.metadata["start_offset"], history_size)
                self.assertEqual(ended.metadata["end_offset"], trace_path.stat().st_size)
                case_text = ended.trace_text_path.read_text(encoding="utf-8")
                self.assertIn("CASE_START", case_text)
                self.assertIn("inside the case", case_text)
                self.assertIn("CASE_END", case_text)
                self.assertNotIn("old history", case_text)

    def test_case_slice_spans_a_trace_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(runtime, "_serial_trace_dir", return_value=root / "logs"),
                patch.object(runtime, "_serial_lock_dir", return_value=root / "locks"),
                patch.dict(runtime.os.environ, {"AUTO_DBG_SERIAL_TRACE_MAX_BYTES": "600"}),
            ):
                runtime.append_serial_trace_marker("COM19", "before case")
                begin_serial_case("COM19", "rotating_case")
                with runtime._acquire_port_lock("COM19"):
                    for index in range(12):
                        runtime._append_trace_entry("COM19", "rx", f"case line {index:02d}")
                ended = end_serial_case("COM19", "rotating_case", result="pass")

                self.assertTrue(runtime.serial_trace_rotated_paths(runtime.serial_trace_log_path("COM19")))
                case_lines = ended.trace_text_path.read_text(encoding="utf-8").splitlines()
                self.assertNotIn("before case", "\n".join(case_lines))
                self.assertIn("CASE_START", case_lines[0])
                self.assertIn("CASE_END", case_lines[-1])
                self.assertEqual(
                    [line.split(" RX ", 1)[1] for line in case_lines if " RX " in line],
                    [f"case line {index:02d}" for index in range(12)],
                )

    def test_end_serial_case_still_reads_cases_begun_with_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                runtime.append_serial_trace_marker("COM19", "before legacy case")
                started = begin_serial_case("COM19", "legacy_case")
                # metadata written by an autodbg from before byte offsets: a line index instead
                metadata = json.loads(started.metadata_path.read_text(encoding="utf-8"))
                for key in ("start_offset", "trace_file_id"):
                    metadata.pop(key, None)
                metadata["start_line"] = 1
                started.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                runtime.append_serial_trace_marker("COM19", "legacy body")
                ended = end_serial_case("COM19", "legacy_case", result="pass")

                case_text = ended.trace_text_path.read_text(encoding="utf-8")
                self.assertNotIn("before legacy case", case_text)
                self.assertIn("CASE_START", case_text)
                self.assertIn("legacy body", case_text)

    def test_legacy_case_survives_the_first_rotation_by_an_upgraded_broker(self) -> None:
        # a case begun by the old autodbg (line numbers), then observe-serial restarted with the rotating broker
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(runtime, "_serial_trace_dir", return_value=root / "logs"),
                patch.object(runtime, "_serial_lock_dir", return_value=root / "locks"),
                patch.dict(runtime.os.environ, {"AUTO_DBG_SERIAL_TRACE_MAX_BYTES": "600"}),
            ):
                for index in range(6):
                    runtime.append_serial_trace_marker("COM35", f"history {index}")
                started = begin_serial_case("COM35", "legacy_rotation")
                metadata = json.loads(started.metadata_path.read_text(encoding="utf-8"))
                for key in ("start_offset", "trace_file_id"):
                    metadata.pop(key, None)
                metadata["start_line"] = 6
                metadata["started_at"] = "2000-01-01T00:00:00"
                started.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                runtime.append_serial_trace_marker("COM35", "before restart")
                with runtime._acquire_port_lock("COM35"):
                    runtime._append_trace_entry("COM35", "sys", "BROKER_OPEN baudrate=115200")
                    runtime._append_trace_entry("COM35", "rx", "after restart")
                ended = end_serial_case("COM35", "legacy_rotation", result="pass")

                self.assertTrue(runtime.serial_trace_rotated_paths(runtime.serial_trace_log_path("COM35")))
                case_text = ended.trace_text_path.read_text(encoding="utf-8")
                self.assertNotIn("history", case_text)
                for expected in ("CASE_START", "before restart", "after restart", "CASE_END"):
                    self.assertIn(expected, case_text)

    def _rotating_env(self, root: Path):
        return (
            patch.object(runtime, "_serial_trace_dir", return_value=root / "logs"),
            patch.object(runtime, "_serial_lock_dir", return_value=root / "locks"),
            patch.dict(runtime.os.environ, {"AUTO_DBG_SERIAL_TRACE_MAX_BYTES": "600"}),
        )

    def test_case_slice_stops_at_its_end_even_if_the_trace_rotates_right_after(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self._rotating_env(root)
            with env[0], env[1], env[2]:
                started = begin_serial_case("COM19", "end_race")
                runtime.append_serial_trace_marker("COM19", "case body")
                trace_path = runtime.serial_trace_log_path("COM19")
                end_offset, end_file_id = runtime.serial_trace_stat(trace_path)
                # the broker rotates and keeps writing between the end snapshot and the read
                with runtime._acquire_port_lock("COM19"):
                    for index in range(8):
                        runtime._append_trace_entry("COM19", "rx", f"after the case {index}")
                metadata = json.loads(started.metadata_path.read_text(encoding="utf-8"))

                entries = _case_trace_entries(metadata, end_offset=end_offset, end_file_id=end_file_id)

                payloads = [entry.payload for entry in entries]
                self.assertTrue(runtime.serial_trace_rotated_paths(trace_path))
                self.assertIn("case body", payloads)
                self.assertFalse(any(payload.startswith("after the case") for payload in payloads))

    def test_an_identity_match_that_cannot_hold_the_start_offset_is_not_trusted(self) -> None:
        # e.g. a FAT volume reusing the rotated file's id for the new trace.jsonl
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self._rotating_env(root)
            with env[0], env[1], env[2]:
                for index in range(4):
                    runtime.append_serial_trace_marker("COM19", f"history {index}")
                started = begin_serial_case("COM19", "id_reuse")
                with runtime._acquire_port_lock("COM19"):
                    for index in range(8):
                        runtime._append_trace_entry("COM19", "rx", f"case line {index}")
                trace_path = runtime.serial_trace_log_path("COM19")
                metadata = json.loads(started.metadata_path.read_text(encoding="utf-8"))
                metadata["trace_file_id"] = runtime.serial_trace_file_id(trace_path)
                started.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                ended = end_serial_case("COM19", "id_reuse", result="pass")

                case_text = ended.trace_text_path.read_text(encoding="utf-8")
                self.assertIn("CASE_START", case_text)
                self.assertIn("case line 0", case_text)
                self.assertNotIn("history", case_text)

    def test_a_legacy_ended_case_without_its_copy_keeps_its_line_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                runtime.append_serial_trace_marker("COM19", "before")
                started = begin_serial_case("COM19", "legacy_ended")
                runtime.append_serial_trace_marker("COM19", "inside")
                ended = end_serial_case("COM19", "legacy_ended", result="pass")
                runtime.append_serial_trace_marker("COM19", "long after the case")
                metadata = json.loads(ended.metadata_path.read_text(encoding="utf-8"))
                for key in ("start_offset", "end_offset", "trace_file_id", "end_file_id", "trace_jsonl_path"):
                    metadata.pop(key, None)
                metadata.update({"start_line": 1, "end_line": 4})
                ended.metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

                captured = capture_serial_case("COM19", "legacy_ended")

                patch_text = captured.evidence_patch_path.read_text(encoding="utf-8")
                self.assertIn("inside", patch_text)
                self.assertNotIn("long after the case", patch_text)
                self.assertNotIn("] SYS before", patch_text)

    def test_begin_serial_case_refuses_duplicate_active_case(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(runtime, "_serial_trace_dir", return_value=Path(temp_dir)):
                begin_serial_case("COM19", "wifi_case_001", title="WiFi")
                with self.assertRaises(FileExistsError):
                    begin_serial_case("COM19", "wifi_case_001", title="WiFi again")


if __name__ == "__main__":
    unittest.main()
