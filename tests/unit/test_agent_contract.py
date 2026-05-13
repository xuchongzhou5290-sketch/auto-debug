import argparse
from contextlib import redirect_stdout
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodbg.agent.contract import build_agent_intake_plan, build_agent_invocation, build_agent_tool_manifest, execute_agent_request
from autodbg.cli.main import _build_parser, _command_agent_call
from autodbg.session.manager import SessionManager


class AgentContractTest(unittest.TestCase):
    def test_build_agent_invocation_translates_request_to_cli_and_env(self) -> None:
        project_root = Path("C:/repo/auto-debug")
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
                    "artifacts_root": "C:/repo/auto-debug/artifacts-custom",
                },
                "options": {
                    "skip_evidence": True,
                    "observe_seconds": 1.5,
                },
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[:5], ["run", "--artifacts-root", "C:\\repo\\auto-debug\\artifacts-custom", "--serial-port", "COM19"])
        self.assertIn("--skip-evidence", invocation.argv)
        self.assertIn("--observe-seconds", invocation.argv)
        self.assertEqual(invocation.env_updates["AUTO_DBG_DEVICE_PASSWORD"], "secret")
        self.assertEqual(invocation.env_updates["AUTO_DBG_WIFI_SSID"], "demo-ssid")
        self.assertEqual(invocation.env_updates["AUTO_DBG_WIFI_PASSWORD"], "demo-pass")

    def test_build_agent_invocation_resolves_loop_and_relative_paths(self) -> None:
        project_root = Path("C:/repo/auto-debug")
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
            "C:\\repo\\auto-debug\\profiles\\devices\\av130n-lab.toml",
        )
        self.assertEqual(
            invocation.argv[invocation.argv.index("--prev-session") + 1],
            "C:\\repo\\auto-debug\\artifacts\\20260420\\demo-prev",
        )
        self.assertEqual(invocation.argv[invocation.argv.index("--iteration") + 1], "2")
        self.assertEqual(invocation.argv[invocation.argv.index("--max-iterations") + 1], "6")
        self.assertEqual(invocation.argv[invocation.argv.index("--attempt-note") + 1], "retry after patch")
        self.assertEqual(invocation.artifacts_root, Path("C:/repo/auto-debug/artifacts-custom"))

    def test_build_agent_invocation_supports_record_intervention(self) -> None:
        project_root = Path("C:/repo/auto-debug")
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
            "C:\\repo\\auto-debug\\artifacts\\20260420\\demo-session",
        )
        self.assertEqual(
            invocation.argv[invocation.argv.index("--file") + 1],
            "C:\\repo\\auto-debug\\src\\autodbg\\cli\\main.py",
        )
        self.assertIsNone(invocation.artifacts_root)

    def test_build_agent_invocation_supports_deploy_verify_artifact_context(self) -> None:
        project_root = Path("C:/repo/auto-debug")
        invocation = build_agent_invocation(
            {
                "action": "deploy-verify",
                "connection": {"serial_port": "COM19", "device_password": "secret"},
                "options": {
                    "artifact": "payloads/APP.bin",
                    "post_pull_command": "lanupg /mnt/sdcard/APP.bin",
                    "expect_marker": "APP_READY",
                    "changed_file": ["src/app/main.c"],
                },
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[0], "deploy-verify")
        self.assertIn("--artifact", invocation.argv)
        self.assertEqual(
            invocation.argv[invocation.argv.index("--artifact") + 1],
            "C:\\repo\\auto-debug\\payloads\\APP.bin",
        )
        self.assertIn("--post-pull-command", invocation.argv)
        self.assertIn("--expect-marker", invocation.argv)
        self.assertEqual(
            invocation.argv[invocation.argv.index("--changed-file") + 1],
            "C:\\repo\\auto-debug\\src\\app\\main.c",
        )
        self.assertEqual(invocation.env_updates["AUTO_DBG_DEVICE_PASSWORD"], "secret")

    def test_build_agent_invocation_supports_sd_http_helper_cross_compile(self) -> None:
        project_root = Path("C:/repo/auto-debug")
        invocation = build_agent_invocation(
            {
                "action": "build-sd-http-helper",
                "options": {
                    "cc": "arm-linux-gnueabihf-gcc",
                    "output": "artifacts/autodbg-http-pull",
                    "cflag": ["-Wall"],
                    "static": False,
                },
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[0], "build-sd-http-helper")
        self.assertIn("--cc", invocation.argv)
        self.assertEqual(invocation.argv[invocation.argv.index("--output") + 1], "C:\\repo\\auto-debug\\artifacts\\autodbg-http-pull")
        self.assertIn("--cflag=-Wall", invocation.argv)
        self.assertIn("--no-static", invocation.argv)
        self.assertIsNone(invocation.artifacts_root)

    def test_build_agent_invocation_supports_quickstart_from_connection(self) -> None:
        project_root = Path("C:/repo/auto-debug")
        invocation = build_agent_invocation(
            {
                "action": "quickstart",
                "connection": {
                    "serial_port": "COM19",
                    "baudrate": 115200,
                    "device_password": "secret",
                    "sdcard_drive": "E:",
                },
                "options": {"goal": "健康检查"},
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[0], "quickstart")
        self.assertIn("--serial-port", invocation.argv)
        self.assertEqual(invocation.argv[invocation.argv.index("--serial-port") + 1], "COM19")
        self.assertIn("--device-password-known", invocation.argv)
        self.assertIn("--sdcard-drive", invocation.argv)
        self.assertNotIn("secret", invocation.argv)

    def test_build_agent_invocation_supports_quickstart_debug_firmware_method(self) -> None:
        project_root = Path("C:/repo/auto-debug")
        invocation = build_agent_invocation(
            {
                "action": "quickstart",
                "options": {
                    "goal": "拉取新包",
                    "debug_firmware_method": "sd_http_helper",
                    "helper_cc": "arm-linux-gnueabihf-gcc",
                    "artifact": "payloads/APP.bin",
                },
            },
            project_root=project_root,
        )

        self.assertEqual(invocation.argv[0], "quickstart")
        self.assertIn("--debug-firmware-method", invocation.argv)
        self.assertEqual(invocation.argv[invocation.argv.index("--debug-firmware-method") + 1], "sd_http_helper")
        self.assertIn("--helper-cc", invocation.argv)
        self.assertIn("--artifact", invocation.argv)

    def test_agent_manifest_includes_serial_collaboration_guidance(self) -> None:
        manifest = build_agent_tool_manifest(project_root=Path("C:/repo/auto-debug"))

        self.assertEqual(manifest["intake_protocol"]["mcp_tool"], "autodbg_prepare")
        self.assertTrue(any("missing_required" in rule for rule in manifest["intake_protocol"]["question_policy"]))
        collaboration = manifest["serial_collaboration"]
        self.assertEqual(collaboration["ai_entrypoint"], "watch-serial")
        self.assertIn("observe-serial", collaboration["human_entrypoints"])
        self.assertEqual(collaboration["shared_owner"], "raw-live broker")
        self.assertTrue(
            any("live serial visibility" in rule for rule in collaboration["rules"])
        )
        self.assertTrue(
            any("operator still needs the shared serial view" in rule for rule in manifest["operating_rules"])
        )
        action_names = {action["name"] for action in manifest["actions"]}
        self.assertIn("deploy-verify", action_names)
        self.assertIn("build-sd-http-helper", action_names)
        self.assertIn("quickstart", action_names)
        self.assertIn("deploy-verify", manifest["required_inputs"]["conditional"]["device_password"])
        self.assertTrue(
            any("debug_firmware_method=firmware_command" in rule for rule in manifest["operating_rules"])
        )
        self.assertTrue(
            any("debug_firmware_method=sd_http_helper" in rule for rule in manifest["operating_rules"])
        )
        workflow_names = {workflow["name"] for workflow in manifest["recommended_workflows"]}
        self.assertIn("sd_helper_package_pull", workflow_names)

    def test_build_agent_intake_plan_reports_missing_required_fields(self) -> None:
        with patch.dict(os.environ, {"AUTO_DBG_SERIAL_PORT": "", "AUTO_DBG_DEVICE_PASSWORD": ""}):
            plan = build_agent_intake_plan({"action": "exec"}, project_root=Path("C:/repo/auto-debug"))

        self.assertFalse(plan["ready"])
        missing_fields = [item["field"] for item in plan["missing_required"]]
        self.assertEqual(missing_fields, ["serial_port", "device_password", "shell_command"])
        self.assertTrue(plan["should_ask_user"])
        self.assertLessEqual(len(plan["user_questions"]), 3)
        self.assertEqual(plan["suggested_request"]["action"], "exec")

    def test_build_agent_intake_plan_infers_action_from_goal(self) -> None:
        with patch.dict(os.environ, {"AUTO_DBG_SERIAL_PORT": ""}):
            plan = build_agent_intake_plan({"goal": "我想观察串口日志"}, project_root=Path("C:/repo/auto-debug"))

        self.assertEqual(plan["action"], "watch-serial")
        self.assertEqual(plan["inferred_action"], "watch-serial")
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["missing_required"][0]["field"], "serial_port")

    def test_build_agent_intake_plan_infers_quickstart_from_new_user_goal(self) -> None:
        plan = build_agent_intake_plan({"goal": "我是小白，想开始使用"}, project_root=Path("C:/repo/auto-debug"))

        self.assertEqual(plan["action"], "quickstart")
        self.assertEqual(plan["inferred_action"], "quickstart")
        self.assertTrue(plan["ready"])
        self.assertFalse(plan["should_ask_user"])

    def test_build_agent_intake_plan_infers_deploy_verify_from_closed_loop_goal(self) -> None:
        with patch.dict(os.environ, {"AUTO_DBG_SERIAL_PORT": "", "AUTO_DBG_DEVICE_PASSWORD": ""}):
            plan = build_agent_intake_plan({"goal": "构建部署后闭环验证修复"}, project_root=Path("C:/repo/auto-debug"))

        self.assertEqual(plan["action"], "deploy-verify")
        self.assertEqual(plan["inferred_action"], "deploy-verify")
        missing_fields = [item["field"] for item in plan["missing_required"]]
        self.assertEqual(missing_fields, ["serial_port", "device_password"])

    def test_build_agent_intake_plan_infers_sd_http_helper_build_goal(self) -> None:
        plan = build_agent_intake_plan({"goal": "交叉编译 autodbg-http-pull"}, project_root=Path("C:/repo/auto-debug"))

        self.assertEqual(plan["action"], "build-sd-http-helper")
        self.assertEqual(plan["inferred_action"], "build-sd-http-helper")
        self.assertEqual([item["field"] for item in plan["missing_required"]], ["cc"])

    def test_build_agent_intake_plan_infers_new_package_pull_goal(self) -> None:
        plan = build_agent_intake_plan({"goal": "拉取新包"}, project_root=Path("C:/repo/auto-debug"))

        self.assertEqual(plan["action"], "quickstart")
        self.assertEqual(plan["inferred_action"], "quickstart")
        self.assertTrue(plan["ready"])

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
