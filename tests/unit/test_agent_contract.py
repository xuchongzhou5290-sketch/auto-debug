import argparse
from contextlib import redirect_stdout
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodbg.agent.contract import build_agent_invocation, execute_agent_request
from autodbg.cli.main import _build_parser, _command_agent_call
from autodbg.session.manager import SessionManager


class AgentContractTest(unittest.TestCase):
    def test_build_agent_invocation_translates_request_to_cli_and_env(self) -> None:
        project_root = Path("X:/Auto-Debug")
        invocation = build_agent_invocation(
            {
                "action": "run",
                "connection": {
                    "serial_port": "COM19",
                    "baudrate": 115200,
                    "device_password": "secret",
                    "wifi_ssid": "demo-ssid",
                    "wifi_password": "demo-pass",
                },
                "profiles": {
                    "artifacts_root": "X:/Auto-Debug/artifacts-custom",
                },
                "options": {
                    "skip_evidence": True,
                    "observe_seconds": 1.5,
                },
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[:5], ["run", "--artifacts-root", "X:\\Auto-Debug\\artifacts-custom", "--serial-port", "COM19"])
        self.assertIn("--skip-evidence", invocation.argv)
        self.assertIn("--observe-seconds", invocation.argv)
        self.assertEqual(invocation.env_updates["AUTO_DBG_DEVICE_PASSWORD"], "secret")
        self.assertEqual(invocation.env_updates["AUTO_DBG_WIFI_SSID"], "demo-ssid")
        self.assertEqual(invocation.env_updates["AUTO_DBG_WIFI_PASSWORD"], "demo-pass")

    def test_build_agent_invocation_resolves_loop_and_relative_paths(self) -> None:
        project_root = Path("X:/Auto-Debug")
        invocation = build_agent_invocation(
            {
                "action": "run",
                "profiles": {
                    "device": "profiles/devices/av130n-lab.toml",
                    "artifacts_root": "artifacts-custom",
                },
                "loop": {
                    "goal_id": "startup-fix-001",
                    "goal": "Reach app_ready without panic",
                    "prev_session": "artifacts/20260420/demo-prev",
                    "iteration": 2,
                    "max_iterations": 6,
                    "attempt_note": "retry after patch",
                },
                "options": {
                    "observe_seconds": 1.5,
                },
            },
            project_root=project_root,
        )

        self.assertIn("--device", invocation.argv)
        self.assertEqual(
            invocation.argv[invocation.argv.index("--device") + 1],
            "X:\\Auto-Debug\\profiles\\devices\\av130n-lab.toml",
        )
        self.assertEqual(
            invocation.argv[invocation.argv.index("--prev-session") + 1],
            "X:\\Auto-Debug\\artifacts\\20260420\\demo-prev",
        )
        self.assertEqual(invocation.argv[invocation.argv.index("--iteration") + 1], "2")
        self.assertEqual(invocation.argv[invocation.argv.index("--max-iterations") + 1], "6")
        self.assertEqual(invocation.argv[invocation.argv.index("--attempt-note") + 1], "retry after patch")
        self.assertEqual(invocation.artifacts_root, Path("X:/Auto-Debug/artifacts-custom"))

    def test_build_agent_invocation_supports_record_intervention(self) -> None:
        project_root = Path("X:/Auto-Debug")
        invocation = build_agent_invocation(
            {
                "action": "record-intervention",
                "options": {
                    "session_dir": "artifacts/20260420/demo-session",
                    "kind": "ai_patch",
                    "summary": "Adjust retry window",
                    "details": "Increase observe timeout before the next run.",
                    "file": ["src/autodbg/cli/main.py"],
                    "git_commit": "abc1234",
                    "expected_effect": "Next iteration should capture enough boot logs.",
                },
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[0], "record-intervention")
        self.assertEqual(
            invocation.argv[invocation.argv.index("--session-dir") + 1],
            "X:\\Auto-Debug\\artifacts\\20260420\\demo-session",
        )
        self.assertEqual(
            invocation.argv[invocation.argv.index("--file") + 1],
            "X:\\Auto-Debug\\src\\autodbg\\cli\\main.py",
        )
        self.assertIsNone(invocation.artifacts_root)

    def test_command_agent_call_returns_structured_json_and_detects_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            request_path = project_root / "request.json"
            request_path.write_text(
                json.dumps(
                    {
                        "action": "run",
                        "connection": {
                            "serial_port": "COM19",
                            "device_password": "demo-secret",
                        },
                    }
                ),
                encoding="utf-8",
                newline="\n",
            )
            args = argparse.Namespace(request=str(request_path), pretty=False)

            def fake_dispatch(inner_args: argparse.Namespace) -> int:
                self.assertEqual(inner_args.command, "run")
                self.assertEqual(os.getenv("AUTO_DBG_DEVICE_PASSWORD"), "demo-secret")
                session = SessionManager(inner_args.artifacts_root).create("av130n-lab", "startup_check")
                (session.session_paths.root / "summary.json").write_text(
                    json.dumps({"status": "run_completed", "session": session.to_dict()}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                (session.session_paths.root / "report.md").write_text("# demo\n", encoding="utf-8", newline="\n")
                (session.session_paths.root / "artifacts_manifest.json").write_text(
                    json.dumps({"artifacts": []}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                print("[DONE] fake run")
                return 0

            with (
                patch("autodbg.cli.main._project_root", return_value=project_root),
                patch("autodbg.cli.main._dispatch_command", side_effect=fake_dispatch),
            ):
                stdout_buffer = io.StringIO()
                with redirect_stdout(stdout_buffer):
                    exit_code = _command_agent_call(args)

        self.assertEqual(exit_code, 0)
        response = json.loads(stdout_buffer.getvalue())
        self.assertTrue(response["ok"])
        self.assertEqual(response["action"], "run")
        self.assertEqual(response["exit_code"], 0)
        self.assertIsNotNone(response["session_dir"])
        self.assertEqual(response["summary"]["status"], "run_completed")
        self.assertIn("[DONE] fake run", response["stdout"])

    def test_command_agent_call_converts_invalid_request_to_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            request_path = project_root / "bad.json"
            request_path.write_text("{not-json}", encoding="utf-8", newline="\n")
            args = argparse.Namespace(request=str(request_path), pretty=False)

            with (
                patch("autodbg.cli.main._project_root", return_value=project_root),
            ):
                stdout_buffer = io.StringIO()
                with redirect_stdout(stdout_buffer):
                    exit_code = _command_agent_call(args)

        self.assertEqual(exit_code, 0)
        response = json.loads(stdout_buffer.getvalue())
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["type"], "JSONDecodeError")

    def test_execute_agent_request_returns_structured_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)

            def fake_dispatch(inner_args: argparse.Namespace) -> int:
                self.assertEqual(inner_args.command, "run")
                self.assertEqual(os.getenv("AUTO_DBG_DEVICE_PASSWORD"), "demo-secret")
                session = SessionManager(inner_args.artifacts_root).create("av130n-lab", "startup_check")
                (session.session_paths.root / "summary.json").write_text(
                    json.dumps({"status": "run_completed", "session": session.to_dict()}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                (session.session_paths.root / "report.md").write_text("# demo\n", encoding="utf-8", newline="\n")
                print("[DONE] execute agent request")
                return 0

            with patch("autodbg.cli.main._project_root", return_value=project_root):
                response = execute_agent_request(
                    {
                        "action": "run",
                        "connection": {
                            "serial_port": "COM19",
                            "device_password": "demo-secret",
                        },
                    },
                    project_root=project_root,
                    parser_builder=_build_parser,
                    dispatcher=fake_dispatch,
                )

        self.assertTrue(response["ok"])
        self.assertEqual(response["action"], "run")
        self.assertEqual(response["exit_code"], 0)
        self.assertIsNotNone(response["session_dir"])
        self.assertEqual(response["summary"]["status"], "run_completed")
        self.assertIn("[DONE] execute agent request", response["stdout"])


if __name__ == "__main__":
    unittest.main()
