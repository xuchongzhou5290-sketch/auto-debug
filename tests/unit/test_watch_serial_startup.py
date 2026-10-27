import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import os
import time

from autodbg.cli.main import (
    _WatchStdinShellState,
    _apply_watch_trace_entry_to_tui_state,
    _command_watch_serial,
    _read_trace_since,
    _read_trace_tail,
    _refresh_watch_status_message,
    _set_watch_status_message,
    _trace_cursor,
)
from autodbg.serial import runtime
from autodbg.serial.broker import SerialBrokerStartError
from autodbg.serial.runtime import SerialBrokerRegistry, SerialTraceEntry


def _trace_line(payload: str, *, direction: str = "rx", port: str = "COM19") -> str:
    return json.dumps(
        {
            "timestamp": "2026-10-26T10:00:00.000",
            "port": port,
            "direction": direction,
            "payload": payload,
            "pid": 1234,
        },
        ensure_ascii=False,
    )


def _write_trace(path: Path, payloads: list[str]) -> None:
    path.write_text("".join(_trace_line(payload) + "\n" for payload in payloads), encoding="utf-8", newline="\n")


def _watch_args(**overrides) -> argparse.Namespace:
    values = {
        "serial_port": "COM19",
        "tail": 0,
        "follow": True,
        "show_system": False,
        "raw_live": True,
        "baudrate": 115200,
        "stdin_probe": False,
        "stdin_shell": False,
        "protect_human_session": False,
        "settings": Path(__file__).resolve().parents[2] / "config" / "user-settings.toml",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _printed(print_mock: Mock) -> list[str]:
    return [str(call.args[0]) if call.args else "" for call in print_mock.call_args_list]


_OBSERVE_REGISTRY = SerialBrokerRegistry(
    host="127.0.0.1",
    tcp_port=9001,
    pid=4321,
    serial_port="COM19",
    baudrate=115200,
    owner="human-observe",
    protected=True,
)


class ReadTraceTailTest(unittest.TestCase):
    def test_returns_last_entries_in_chronological_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, [f"line {index}" for index in range(10)])

            entries = _read_trace_tail(trace_path, 3)

        self.assertEqual([entry.payload for entry in entries], ["line 7", "line 8", "line 9"])

    def test_skips_blank_and_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            trace_path.write_text(
                "\n".join([_trace_line("a"), "", "not json", _trace_line("b"), _trace_line("c")]) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            entries = _read_trace_tail(trace_path, 3)

        self.assertEqual([entry.payload for entry in entries], ["a", "b", "c"])

    def test_decodes_multibyte_text_split_across_blocks(self) -> None:
        payloads = ["日期:10月21日 周三", "UI CMD(2) value(10月21日 周三)", "😀 emoji 行"]
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, payloads)

            entries = _read_trace_tail(trace_path, 3, block_size=7)

        self.assertEqual([entry.payload for entry in entries], payloads)

    def test_never_decodes_history_before_the_requested_lines(self) -> None:
        # a 1.7 GB trace used to be decoded as one string before anything was shown; the tail must only touch its end
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            with trace_path.open("wb") as handle:
                handle.write(b"\xff\xfe undecodable history \xff\n" * 5000)
                handle.write((_trace_line("recent 1") + "\n" + _trace_line("recent 2") + "\n").encode("utf-8"))

            entries = _read_trace_tail(trace_path, 2, block_size=256)

        self.assertEqual([entry.payload for entry in entries], ["recent 1", "recent 2"])

    def test_continues_into_rotated_segments_when_the_current_file_is_short(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            _write_trace(directory / "trace.20261025-090000.jsonl", ["oldest 1", "oldest 2"])
            _write_trace(directory / "trace.20261026-090000.jsonl", ["older 1", "older 2", "older 3"])
            _write_trace(directory / "trace.jsonl", ["BROKER_OPEN", "current"])

            entries = _read_trace_tail(directory / "trace.jsonl", 4)

        self.assertEqual([entry.payload for entry in entries], ["older 2", "older 3", "BROKER_OPEN", "current"])

    def test_missing_file_or_zero_count_returns_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            self.assertEqual(_read_trace_tail(trace_path, 5), [])
            _write_trace(trace_path, ["a"])
            self.assertEqual(_read_trace_tail(trace_path, 0), [])
            self.assertEqual([entry.payload for entry in _read_trace_tail(trace_path, 5)], ["a"])


class ReadTraceSinceTest(unittest.TestCase):
    def test_returns_only_complete_lines_after_the_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, ["old 1", "old 2"])
            cursor = _trace_cursor(trace_path)

            entries, cursor = _read_trace_since(trace_path, cursor)
            self.assertEqual(entries, [])

            partial = (_trace_line("new 1") + "\n").encode("utf-8")
            with trace_path.open("ab") as handle:
                handle.write(partial[:20])
            entries, cursor_after_partial = _read_trace_since(trace_path, cursor)
            self.assertEqual(entries, [])
            self.assertEqual(cursor_after_partial, cursor)

            with trace_path.open("ab") as handle:
                handle.write(partial[20:])
            entries, cursor = _read_trace_since(trace_path, cursor)

            self.assertEqual([entry.payload for entry in entries], ["new 1"])
            self.assertEqual(cursor, _trace_cursor(trace_path))

    def test_finishes_the_rotated_file_before_reading_the_new_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, ["old 1", "old 2"])
            cursor = _trace_cursor(trace_path)
            with trace_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(_trace_line("written just before the rotation") + "\n")
            trace_path.rename(Path(temp_dir) / "trace.20261026-100000.jsonl")
            _write_trace(trace_path, ["first in the new file"])

            entries, cursor = _read_trace_since(trace_path, cursor)

            self.assertEqual(
                [entry.payload for entry in entries],
                ["written just before the rotation", "first in the new file"],
            )
            self.assertEqual(cursor, _trace_cursor(trace_path))

    def test_restarts_from_the_beginning_when_the_file_shrank(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, ["fresh after rotation"])

            entries, cursor = _read_trace_since(trace_path, (None, 10_000))

            self.assertEqual([entry.payload for entry in entries], ["fresh after rotation"])
            self.assertEqual(cursor, _trace_cursor(trace_path))

    def test_a_missing_file_keeps_the_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            entries, cursor = _read_trace_since(Path(temp_dir) / "trace.jsonl", ("1:2", 300))

        self.assertEqual((entries, cursor), ([], ("1:2", 300)))


class TraceHistoryAcrossRotationTest(unittest.TestCase):
    def test_tail_does_not_count_a_just_rotated_file_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            _write_trace(directory / "trace.20261025-090000.jsonl", ["older 1", "older 2"])
            _write_trace(directory / "trace.jsonl", ["current 1", "current 2"])
            # what the reader sees when the broker renames trace.jsonl right after it was read: same file, new name
            os.link(directory / "trace.jsonl", directory / "trace.20261026-090000.jsonl")

            entries = _read_trace_tail(directory / "trace.jsonl", 4)

        self.assertEqual([entry.payload for entry in entries], ["older 1", "older 2", "current 1", "current 2"])

    def test_tail_zero_without_follow_prints_the_rotated_history_too(self) -> None:
        args = _watch_args(raw_live=False, follow=False, tail=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(Path(temp_dir) / "trace.20261025-090000.jsonl", ["oldest"])
            _write_trace(Path(temp_dir) / "trace.20261026-090000.jsonl", ["older"])
            _write_trace(trace_path, ["current"])
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                patch("builtins.print") as print_mock,
            ):
                _command_watch_serial(args)

        printed = [line for line in _printed(print_mock) if line.startswith("[RX")]
        self.assertEqual([line.split("] ", 1)[1] for line in printed], ["oldest", "older", "current"])


class WatchSerialStartupTest(unittest.TestCase):
    def test_observe_window_announces_and_opens_port_before_any_trace_io(self) -> None:
        events: list[str] = []
        args = _watch_args(protect_human_session=True, stdin_shell=True)
        broker_mock = Mock()
        broker_mock.start.side_effect = lambda: events.append("broker.start")

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, [f"history {index}" for index in range(50)])
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", side_effect=lambda _port: _OBSERVE_REGISTRY if broker_mock.start.called else None),
                patch("autodbg.cli.main.SerialBroker", return_value=broker_mock),
                patch("autodbg.cli.main._read_trace_entries", side_effect=lambda *_a, **_k: events.append("read_all") or []),
                patch("autodbg.cli.main._read_trace_tail", side_effect=lambda *_a, **_k: events.append("read_tail") or []),
                patch("autodbg.cli.main._follow_trace_via_broker", side_effect=KeyboardInterrupt) as follow_mock,
                patch("builtins.print", side_effect=lambda *a, **_k: events.append(f"print:{a[0] if a else ''}")),
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        self.assertNotIn("read_all", events)
        self.assertNotIn("read_tail", events)
        start_index = events.index("broker.start")
        self.assertTrue(any(event.startswith("print:[ACTIVE] Watching shared serial trace") for event in events[:start_index]))
        self.assertEqual(follow_mock.call_args.kwargs["initial_entries"], [])

    def test_follow_with_tail_seeds_only_the_requested_lines(self) -> None:
        args = _watch_args(raw_live=False, tail=2, stdin_shell=True)

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, [f"line {index}" for index in range(5)])
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=_OBSERVE_REGISTRY),
                patch("autodbg.cli.main._follow_trace_via_broker", side_effect=KeyboardInterrupt) as follow_mock,
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        seeded = follow_mock.call_args.kwargs["initial_entries"]
        self.assertEqual([entry.payload for entry in seeded], ["line 3", "line 4"])
        self.assertTrue(any("Replaying the latest 2 trace line(s)" in line for line in _printed(print_mock)))

    def test_tail_without_follow_prints_only_the_requested_lines(self) -> None:
        args = _watch_args(raw_live=False, follow=False, tail=2)

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, [f"line {index}" for index in range(5)])
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        printed = "\n".join(_printed(print_mock))
        self.assertNotIn("line 2", printed)
        self.assertIn("line 3", printed)
        self.assertIn("line 4", printed)

    def test_tail_zero_without_follow_still_prints_the_whole_history(self) -> None:
        args = _watch_args(raw_live=False, follow=False, tail=0)

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, ["first", "second", "third"])
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        printed = "\n".join(_printed(print_mock))
        for payload in ("first", "second", "third"):
            self.assertIn(payload, printed)

    def test_polling_fallback_prints_only_lines_appended_after_start(self) -> None:
        args = _watch_args(raw_live=True, follow=True, tail=0)
        broker_mock = Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            _write_trace(trace_path, ["old history 1", "old history 2"])
            sleeps = {"count": 0}

            def fake_sleep(_seconds: float) -> None:
                sleeps["count"] += 1
                if sleeps["count"] == 1:
                    with trace_path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(_trace_line("fresh line") + "\n")
                    return
                raise KeyboardInterrupt

            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                patch("autodbg.cli.main.SerialBroker", return_value=broker_mock),
                patch("autodbg.cli.main.time.sleep", side_effect=fake_sleep),
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        printed = "\n".join(_printed(print_mock))
        self.assertIn("fresh line", printed)
        self.assertNotIn("old history", printed)
        broker_mock.stop.assert_called_once()

    def test_observe_window_open_failure_is_recorded_for_the_launcher(self) -> None:
        args = _watch_args(serial_port="COM4", protect_human_session=True, stdin_shell=True)
        broker_mock = Mock()
        broker_mock.start.side_effect = SerialBrokerStartError(
            "Failed to open COM4: Cannot configure port, something went wrong. "
            "Original message: PermissionError(13, '连接到系统上的设备没有发挥作用。', None, 31)"
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "trace.jsonl"
            # the launcher hands the window a status file of its own through the environment
            status_file = Path(temp_dir) / "com4_port.launch-abc.status"
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                patch("autodbg.cli.main.SerialBroker", return_value=broker_mock),
                patch.dict(runtime.os.environ, {"AUTO_DBG_OBSERVE_STATUS_FILE": str(status_file)}),
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)
            recorded = runtime.read_observe_launch_status(status_file)

        self.assertEqual(exit_code, 1)
        broker_mock.stop.assert_called_once()
        self.assertIsNotNone(recorded)
        self.assertIn("没有发挥作用", recorded["error"])
        self.assertTrue(any("unplug/replug" in hint for hint in recorded["hints"]))
        self.assertTrue(any("unplug/replug" in line for line in _printed(print_mock)))


class WatchTuiLinkStatusTest(unittest.TestCase):
    @staticmethod
    def _sys_entry(payload: str) -> SerialTraceEntry:
        return SerialTraceEntry(timestamp="2026-10-26T10:00:00.000", port="COM19", direction="sys", payload=payload, pid=1)

    def test_lost_link_stays_visible_until_the_port_reconnects(self) -> None:
        state = _WatchStdinShellState()

        _apply_watch_trace_entry_to_tui_state(
            self._sys_entry("SERIAL_UNAVAILABLE error=ClearCommError failed"),
            show_system=False,
            state=state,
        )

        # the 4 s status flash used to vanish and leave a silent, frozen-looking screen
        self.assertIsNone(state.status_expires_at)
        self.assertIn("COM19 unavailable", state.status_message)
        self.assertIn("ClearCommError failed", state.recent_lines[-1])

        _apply_watch_trace_entry_to_tui_state(
            self._sys_entry("SERIAL_CONNECTED baudrate=115200"),
            show_system=False,
            state=state,
        )

        self.assertNotIn("unavailable", state.status_message)
        self.assertIn("COM19 connected", state.recent_lines[-1])

    def test_a_passing_message_falls_back_to_the_lost_link_status(self) -> None:
        state = _WatchStdinShellState()
        _apply_watch_trace_entry_to_tui_state(
            self._sys_entry("SERIAL_UNAVAILABLE error=ClearCommError failed"), show_system=False, state=state
        )

        # Enter / Ctrl+L / pause show their own message for a few seconds
        _set_watch_status_message(state, "Sent command to COM19: ls", transient_seconds=4.0)
        _refresh_watch_status_message(state, now=time.monotonic() + 10.0)
        self.assertIn("COM19 unavailable", state.status_message)

        _apply_watch_trace_entry_to_tui_state(self._sys_entry("SERIAL_CONNECTED baudrate=115200"), show_system=False, state=state)
        _refresh_watch_status_message(state, now=time.monotonic() + 10.0)
        self.assertNotIn("unavailable", state.status_message)

    def test_first_connection_does_not_add_a_reconnect_line(self) -> None:
        state = _WatchStdinShellState()

        _apply_watch_trace_entry_to_tui_state(
            self._sys_entry("SERIAL_CONNECTED baudrate=115200"),
            show_system=False,
            state=state,
        )

        self.assertEqual(state.recent_lines, [])


if __name__ == "__main__":
    unittest.main()
