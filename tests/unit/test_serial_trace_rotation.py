from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodbg.serial import runtime


@contextmanager
def _trace_env(*, max_bytes: int, keep: int = 30):
    with tempfile.TemporaryDirectory() as temp_dir, ExitStack() as stack:
        root = Path(temp_dir)
        stack.enter_context(patch.object(runtime, "_serial_trace_dir", return_value=root / "logs"))
        stack.enter_context(patch.object(runtime, "_serial_lock_dir", return_value=root / "locks"))
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "AUTO_DBG_SERIAL_TRACE_MAX_BYTES": str(max_bytes),
                    "AUTO_DBG_SERIAL_TRACE_KEEP": str(keep),
                },
            )
        )
        (root / "logs").mkdir()
        yield runtime.serial_trace_log_path("COM19")


def _payloads(paths: list[Path]) -> list[str]:
    payloads: list[str] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            payloads.append(json.loads(line)["payload"])
    return payloads


def _all_segments(trace_path: Path) -> list[Path]:
    return [*runtime.serial_trace_rotated_paths(trace_path), trace_path]


class SerialTraceRotationTest(unittest.TestCase):
    def test_port_lock_holder_rotates_the_trace_past_the_size_limit(self) -> None:
        with _trace_env(max_bytes=400) as trace_path:
            with runtime._acquire_port_lock("COM19"):
                for index in range(20):
                    runtime._append_trace_entry("COM19", "rx", f"line {index:02d}")

            rotated = runtime.serial_trace_rotated_paths(trace_path)
            line_size = len(trace_path.read_text(encoding="utf-8").splitlines()[-1]) + 1
            self.assertTrue(rotated)
            self.assertTrue(all(400 <= path.stat().st_size < 400 + line_size for path in rotated))
            self.assertLess(trace_path.stat().st_size, 400 + line_size)
            # nothing lost, nothing duplicated, still in order
            self.assertEqual(_payloads(_all_segments(trace_path)), [f"line {index:02d}" for index in range(20)])

    def test_writers_without_the_port_lock_never_rotate(self) -> None:
        with _trace_env(max_bytes=400) as trace_path:
            for index in range(20):
                runtime._append_trace_entry("COM19", "sys", f"CASE marker {index}")

            self.assertEqual(runtime.serial_trace_rotated_paths(trace_path), [])
            self.assertGreater(trace_path.stat().st_size, 400)

    def test_an_oversized_legacy_trace_is_rotated_by_the_first_owned_append(self) -> None:
        with _trace_env(max_bytes=1000) as trace_path:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_bytes(b"x" * 5000 + b"\n")

            with runtime._acquire_port_lock("COM19"):
                runtime._append_trace_entry("COM19", "sys", "BROKER_OPEN baudrate=115200")
                runtime._append_trace_entry("COM19", "sys", "SERIAL_CONNECTED baudrate=115200")

            rotated = runtime.serial_trace_rotated_paths(trace_path)
            self.assertEqual(len(rotated), 1)
            self.assertTrue(rotated[0].read_bytes().startswith(b"x" * 5000))
            self.assertEqual(
                _payloads([trace_path]),
                ["BROKER_OPEN baudrate=115200", "SERIAL_CONNECTED baudrate=115200"],
            )

    def test_only_the_newest_rotated_segments_are_kept(self) -> None:
        with _trace_env(max_bytes=150, keep=2) as trace_path:
            with runtime._acquire_port_lock("COM19"):
                for index in range(30):
                    runtime._append_trace_entry("COM19", "rx", f"line {index:02d}")

            rotated = runtime.serial_trace_rotated_paths(trace_path)
            self.assertEqual(len(rotated), 2)
            kept = _payloads(_all_segments(trace_path))
            # the newest history survives without a gap: deleting the newest segments would leave one before trace.jsonl
            self.assertEqual(kept, [f"line {index:02d}" for index in range(30 - len(kept), 30)])
            self.assertLess(len(kept), 30)

    def test_rotation_blocked_by_another_process_is_retried_later(self) -> None:
        clock = {"now": 1000.0}
        with _trace_env(max_bytes=200) as trace_path, patch.object(runtime.time, "monotonic", side_effect=lambda: clock["now"]):
            with runtime._acquire_port_lock("COM19"):
                with patch.object(runtime.os, "rename", side_effect=PermissionError(13, "file in use")):
                    for index in range(10):
                        runtime._append_trace_entry("COM19", "rx", f"blocked {index}")
                self.assertEqual(runtime.serial_trace_rotated_paths(trace_path), [])

                runtime._append_trace_entry("COM19", "rx", "still within the retry delay")
                self.assertEqual(runtime.serial_trace_rotated_paths(trace_path), [])

                clock["now"] += 60.0
                runtime._append_trace_entry("COM19", "rx", "after the retry delay")

            self.assertEqual(len(runtime.serial_trace_rotated_paths(trace_path)), 1)
            self.assertIn("after the retry delay", _payloads(_all_segments(trace_path)))

    def test_rotated_paths_are_ordered_by_rotation_time_then_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for name in (
                "trace.20261026-101500-1.jsonl",
                "trace.20261026-101500.jsonl",
                "trace.20261025-235959.jsonl",
                "trace.jsonl",
                "trace.backup.jsonl",
            ):
                (directory / name).write_text("", encoding="utf-8")

            ordered = runtime.serial_trace_rotated_paths(directory / "trace.jsonl")

        self.assertEqual(
            [path.name for path in ordered],
            ["trace.20261025-235959.jsonl", "trace.20261026-101500.jsonl", "trace.20261026-101500-1.jsonl"],
        )


if __name__ == "__main__":
    unittest.main()
