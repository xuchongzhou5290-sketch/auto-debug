import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodbg.cli.main import (
    _build_parser,
    _command_describe_agent_tool,
    _command_install_home_plugin,
    _command_install_local_tool,
    _command_record_intervention,
    _build_watch_tui_rows,
    _build_existing_network_check,
    _build_health_checks,
    _build_auto_wlan_bootstrap_commands,
    _build_collect_evidence_commands,
    _build_sd_http_helper_compile_command,
    _build_quickstart_action_plan,
    _build_structured_command,
    _default_collect_evidence_files,
    _build_fetch_file_command,
    _build_fetch_path_command,
    _build_network_dir_resolver,
    _build_remote_pull_command,
    _build_serial_bundle_commands,
    _build_sd_http_helper_command,
    _build_transfer_probe_command,
    _command_run,
    _command_serial_broker,
    _command_watch_serial,
    _consume_watch_stdin_probe,
    _consume_watch_stdin_shell,
    _default_watch_status,
    _page_watch_history,
    _refresh_watch_status_message,
    _run_watch_auto_login,
    _set_watch_status_message,
    _WatchStdinShellState,
    _watch_shell_read_chars,
    _default_payload_root,
    _default_base_url,
    _format_live_tag,
    _format_trace_entry,
    _format_output_excerpt,
    _host_from_base_url,
    _line_matches_focus_terms,
    _normalize_focus_terms,
    _parse_transfer_capabilities,
    _parse_fetch_output_lines,
    _print_watch_trace_entry,
    _extract_structured_output_lines,
    _evaluate_validation_spec,
    _load_profiles_from_args,
    _read_trace_entries,
    _resolve_watch_start_index,
    _resolve_bootstrap_commands,
    _resolve_connectivity_checks,
    _resolve_pull_base_url,
    _resolve_pull_workspace,
    _select_transfer_mode,
)
from autodbg.agent import build_agent_tool_manifest
from autodbg.profiles.loader import load_run_profiles
from autodbg.control.controller import LoginResult
from autodbg.evidence.collector import EvidenceCollector
from autodbg.serial.observer import MarkerHit, ObservationResult
from autodbg.serial.runtime import SerialBrokerRegistry, SerialTraceEntry
from autodbg.session.manager import SessionManager
from autodbg.state.machine import StateSnapshot


class CliMainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[2]
        cls.profiles = load_run_profiles(
            root / "profiles" / "devices" / "av130n-lab.toml",
            root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            root / "profiles" / "tasks" / "startup-check.toml",
            root / "profiles" / "transports" / "network-serial-fallback.toml",
        )

    def test_build_health_checks_includes_sd_probe_by_default(self) -> None:
        checks = _build_health_checks(include_sd_write_probe=True)
        names = [name for name, _ in checks]
        self.assertIn("sdcard_write_probe", names)
        self.assertEqual(names[-1], "mmc_dmesg_tail")

    def test_build_health_checks_can_skip_sd_probe(self) -> None:
        checks = _build_health_checks(include_sd_write_probe=False)
        names = [name for name, _ in checks]
        self.assertNotIn("sdcard_write_probe", names)
        self.assertIn("mmc_mount", names)

    def test_command_install_home_plugin_prints_summary(self) -> None:
        args = argparse.Namespace(
            project_root=Path("C:/repo/auto-debug"),
            home_root=Path("C:/Temp/home"),
            plugin_name="embedded-device-auto-debug-mcp",
        )
        fake_result = {
            "plugin_dir": Path("C:/Temp/home/plugins/embedded-device-auto-debug-mcp"),
            "plugin_manifest": Path("C:/Temp/home/plugins/embedded-device-auto-debug-mcp/.codex-plugin/plugin.json"),
            "mcp_config": Path("C:/Temp/home/plugins/embedded-device-auto-debug-mcp/.mcp.json"),
            "marketplace": Path("C:/Temp/home/.agents/plugins/marketplace.json"),
        }

        with patch("autodbg.cli.main.install_home_plugin", return_value=fake_result):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = _command_install_home_plugin(args)

        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("Home plugin installed", output)
        self.assertIn("Marketplace", output)

    def test_command_install_local_tool_prints_summary(self) -> None:
        args = argparse.Namespace(
            project_root=Path("C:/repo/auto-debug"),
            install_root=Path("C:/Users/demo/AppData/Local/Programs/auto-debug"),
            skip_venv=False,
            skip_local_settings=False,
        )
        fake_result = {
            "install_root": Path("C:/Users/demo/AppData/Local/Programs/auto-debug"),
            "bin_dir": Path("C:/Users/demo/AppData/Local/Programs/auto-debug/bin"),
            "autodbg_cmd": Path("C:/Users/demo/AppData/Local/Programs/auto-debug/bin/autodbg.cmd"),
            "observe_cmd": Path("C:/Users/demo/AppData/Local/Programs/auto-debug/bin/observe-serial.cmd"),
        }

        with patch("autodbg.cli.main.install_local_tool", return_value=fake_result):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = _command_install_local_tool(args)

        self.assertEqual(exit_code, 0)
        output = buffer.getvalue()
        self.assertIn("Local tool installed", output)
        self.assertIn("Observe wrapper", output)

    def test_command_record_intervention_updates_session_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            session = SessionManager(artifacts_root).create("av130n-lab", "startup_check")
            collector = EvidenceCollector(session)
            collector.bootstrap(
                profiles=self.profiles,
                state_snapshot=StateSnapshot(),
                workflow_name="startup_check",
                workflow_steps=[],
                action_name="run",
                loop_context={"goal": "Reach app_ready", "iteration": 1},
                plan_details={"control": {"mode": "demo"}},
            )
            args = argparse.Namespace(
                session_dir=session.session_paths.root,
                kind="ai_patch",
                summary="Adjust startup timeout",
                details="Increase the observation window before the next run.",
                file=["src/autodbg/cli/main.py"],
                git_commit="abc1234",
                expected_effect="Next run should capture more startup logs.",
                related_session=None,
                metadata_json='{"author":"codex"}',
            )

            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = _command_record_intervention(args)

            summary = json.loads((session.session_paths.root / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["interventions"]["count"], 1)
        self.assertEqual(summary["interventions"]["latest"]["metadata"]["author"], "codex")
        self.assertIn("Intervention recorded", buffer.getvalue())

    def test_format_output_excerpt_truncates_and_counts_extra_lines(self) -> None:
        excerpt = _format_output_excerpt(
            [
                "/dev/mmcblk0p1 on /mnt/sdcard type vfat (rw,sync,relatime,fmask=0022,dmask=0022)",
                "line-two",
            ],
            max_length=48,
        )
        self.assertTrue(excerpt.endswith("(+1 lines)"))
        self.assertIn("...", excerpt)

    def test_normalize_focus_terms_discards_blanks(self) -> None:
        self.assertEqual(_normalize_focus_terms([" VQE ", "", " mmc "]), ["vqe", "mmc"])

    def test_line_matches_focus_terms_is_case_insensitive(self) -> None:
        self.assertTrue(_line_matches_focus_terms("[VQE] entry...", ["vqe"]))
        self.assertFalse(_line_matches_focus_terms("LeCam ready", ["panic"]))

    def test_format_live_tag_uses_friendly_labels(self) -> None:
        self.assertEqual(_format_live_tag(None), "SERIAL")
        self.assertEqual(
            _format_live_tag(MarkerHit(marker="closeli login:", line="closeli login:", tag="login")),
            "LOGIN",
        )
        self.assertEqual(
            _format_live_tag(MarkerHit(marker="panic", line="panic", tag="panic")),
            "PANIC",
        )

    def test_resolve_pull_workspace_prefers_profile_workspace(self) -> None:
        self.assertEqual(_resolve_pull_workspace(self.profiles, None), "/mnt/sdcard/autodbg")
        self.assertEqual(_resolve_pull_workspace(self.profiles, "/tmp/custom"), "/tmp/custom")

    def test_resolve_pull_base_url_prefers_override_then_profile_then_default(self) -> None:
        self.assertEqual(
            _resolve_pull_base_url(self.profiles, "http://10.0.0.2:9000/", bind="0.0.0.0", port=8765),
            "http://10.0.0.2:9000",
        )
        self.assertEqual(
            _resolve_pull_base_url(self.profiles, None, bind="127.0.0.1", port=8765),
            "http://127.0.0.1:8765",
        )

    def test_build_existing_network_check_uses_interface_priority(self) -> None:
        command = _build_existing_network_check(self.profiles)
        self.assertIn("eth0", command)
        self.assertIn("wlan0", command)
        self.assertIn("AUTODBG_ACTIVE_IFACE", command)
        self.assertNotIn("exit 0", command)

    def test_build_structured_command_prefixes_output_without_losing_exit_status(self) -> None:
        command = _build_structured_command("ls /opt/lecam")
        self.assertIn("__AUTODBG_CMD__", command)
        self.assertIn("exit \"$AUTODBG_RC\"", command)
        self.assertIn("{ ls /opt/lecam; }", command)

    def test_extract_structured_output_lines_prefers_prefixed_lines(self) -> None:
        self.assertEqual(
            _extract_structured_output_lines(
                [
                    "2026 noisy business log",
                    "__AUTODBG_CMD__line-one",
                    "__AUTODBG_CMD__line-two",
                ]
            ),
            ["line-one", "line-two"],
        )

    def test_resolve_connectivity_checks_prefers_existing_link_and_ping_host(self) -> None:
        with patch("autodbg.cli.main.detect_host_ipv4", return_value="192.168.1.2"):
            commands = _resolve_connectivity_checks(
                self.profiles,
                [],
                mode="lan_ready",
                base_url=None,
            )
        self.assertGreaterEqual(len(commands), 2)
        self.assertIn("AUTODBG_ACTIVE_IFACE", commands[0])
        self.assertEqual(commands[1], "ping -c 1 '192.168.1.2'")

    def test_build_remote_pull_command_embeds_workspace_and_script_url(self) -> None:
        command = _build_remote_pull_command(
            "http://127.0.0.1:8765",
            workspace="/mnt/sdcard/autodbg",
            script_name="autodbg-pull.sh",
        )
        self.assertIn("curl -fsSL 'http://127.0.0.1:8765/autodbg-pull.sh'", command)
        self.assertIn("WORKSPACE='/mnt/sdcard/autodbg' sh 'autodbg-pull.sh'", command)

    def test_build_sd_http_helper_command_executes_staged_helper(self) -> None:
        command = _build_sd_http_helper_command(
            "http://127.0.0.1:8765/",
            workspace="/mnt/sdcard/autodbg",
            helper_path="/mnt/sdcard/autodbg/autodbg-http-pull",
            list_name="autodbg-files.txt",
        )

        self.assertIn("chmod +x '/mnt/sdcard/autodbg/autodbg-http-pull'", command)
        self.assertIn(
            "'/mnt/sdcard/autodbg/autodbg-http-pull' 'http://127.0.0.1:8765' '/mnt/sdcard/autodbg' 'autodbg-files.txt'",
            command,
        )

    def test_build_transfer_probe_command_checks_downloader_and_bundle_tools(self) -> None:
        command = _build_transfer_probe_command("/mnt/sdcard/autodbg/autodbg-http-pull")
        self.assertIn("AUTODBG_DOWNLOADER=", command)
        self.assertIn("AUTODBG_BASE64=", command)
        self.assertIn("AUTODBG_TAR=", command)
        self.assertIn("AUTODBG_SD_HTTP_HELPER=", command)
        self.assertIn("[ -f '/mnt/sdcard/autodbg/autodbg-http-pull' ]", command)

    def test_build_fetch_file_command_wraps_file_check_and_base64(self) -> None:
        command = _build_fetch_file_command("/mnt/sdcard/autodbg/hello.txt")
        self.assertIn("AUTODBG_FETCH_MISSING /mnt/sdcard/autodbg/hello.txt", command)
        self.assertIn("AUTODBG_META__MODE=file", command)
        self.assertIn("AUTODBG_B64__", command)
        self.assertIn("base64 < '/mnt/sdcard/autodbg/hello.txt'", command)

    def test_build_fetch_path_command_supports_file_and_directory(self) -> None:
        command = _build_fetch_path_command("/mnt/sdcard/autodbg")
        self.assertIn("AUTODBG_META__MODE=file", command)
        self.assertIn("AUTODBG_META__MODE=tar", command)
        self.assertIn("tar -cf - '/mnt/sdcard/autodbg'", command)

    def test_parse_fetch_output_lines_extracts_metadata_and_payload(self) -> None:
        metadata, payload_lines = _parse_fetch_output_lines(
            [
                "__AUTODBG_META__MODE=file",
                "__AUTODBG_META__SOURCE=/etc/wlanname",
                "__AUTODBG_B64__dXNiMAo=",
                "2026 noisy line",
            ]
        )
        self.assertEqual(metadata["mode"], "file")
        self.assertEqual(metadata["source"], "/etc/wlanname")
        self.assertEqual(payload_lines, ["dXNiMAo="])

    def test_build_collect_evidence_commands_includes_app_and_artifact_paths(self) -> None:
        commands = _build_collect_evidence_commands(self.profiles)
        names = [name for name, _ in commands]
        self.assertIn("uname", names)
        self.assertIn("app_process", names)
        self.assertIn("artifact_path_1", names)
        self.assertTrue(any("grep -F 'LeCam'" in command for _, command in commands))
        self.assertTrue(any("AUTODBG_PROCESS_MISSING LeCam" in command for _, command in commands))
        self.assertTrue(any("AUTODBG_MISSING_PATH /mnt/sdcard" in command for _, command in commands))

    def test_default_collect_evidence_files_includes_wlanname(self) -> None:
        self.assertEqual(_default_collect_evidence_files(self.profiles), ["/etc/wlanname"])

    def test_parse_transfer_capabilities_reads_expected_flags(self) -> None:
        capabilities = _parse_transfer_capabilities(
            [
                "AUTODBG_DOWNLOADER=none",
                "AUTODBG_BASE64=yes",
                "AUTODBG_TAR=yes",
                "AUTODBG_SD_HTTP_HELPER=yes",
            ]
        )
        self.assertEqual(capabilities["downloader"], "none")
        self.assertTrue(capabilities["has_base64"])
        self.assertTrue(capabilities["has_tar"])
        self.assertEqual(capabilities["sd_http_helper"], "yes")

    def test_select_transfer_mode_prefers_sd_helper_then_http_then_serial_bundle(self) -> None:
        self.assertEqual(
            _select_transfer_mode(
                "auto",
                capabilities={"downloader": "curl", "has_base64": True, "has_tar": True, "sd_http_helper": "yes"},
                network_mode="lan_ready",
            ),
            "sd_http_helper",
        )
        self.assertEqual(
            _select_transfer_mode(
                "auto",
                capabilities={"downloader": "curl", "has_base64": False, "has_tar": False, "sd_http_helper": "no"},
                network_mode="lan_ready",
            ),
            "http",
        )
        self.assertEqual(
            _select_transfer_mode(
                "auto",
                capabilities={"downloader": "none", "has_base64": True, "has_tar": True, "sd_http_helper": "no"},
                network_mode="lan_ready",
            ),
            "serial_bundle",
        )

    def test_select_transfer_mode_can_force_sd_http_helper(self) -> None:
        self.assertEqual(
            _select_transfer_mode(
                "sd_http_helper",
                capabilities={"downloader": "none", "has_base64": False, "has_tar": False, "sd_http_helper": "yes"},
                network_mode="lan_ready",
            ),
            "sd_http_helper",
        )

    def test_build_sd_http_helper_compile_command_uses_cross_compiler(self) -> None:
        args = argparse.Namespace(
            cc="arm-linux-gnueabihf-gcc",
            source=Path("src/autodbg/assets/autodbg_http_pull.c"),
            output=Path("artifacts/autodbg-http-pull"),
            cflag=["-Wall"],
            static=False,
        )

        command, source, output = _build_sd_http_helper_compile_command(args)

        self.assertEqual(command[0], "arm-linux-gnueabihf-gcc")
        self.assertIn("-Os", command)
        self.assertIn("-Wall", command)
        self.assertNotIn("-static", command)
        self.assertEqual(source, Path(__file__).resolve().parents[2] / "src" / "autodbg" / "assets" / "autodbg_http_pull.c")
        self.assertEqual(output, Path(__file__).resolve().parents[2] / "artifacts" / "autodbg-http-pull")

    def test_select_transfer_mode_rejects_network_transfer_when_offline(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Offline mode cannot use network HTTP transfer"):
            _select_transfer_mode(
                "sd_http_helper",
                capabilities={"downloader": "none", "has_base64": True, "has_tar": True, "sd_http_helper": "yes"},
                network_mode="offline",
            )

    def test_build_serial_bundle_commands_emits_prepare_upload_and_finalize_steps(self) -> None:
        prepare_command, upload_commands, finalize_command = _build_serial_bundle_commands(
            base64_payload="QUJDRA==",
            workspace="/mnt/sdcard/autodbg",
            chunk_size=4,
            artifact_count=2,
        )
        self.assertIn("rm -f '.autodbg-bundle.tar.b64' '.autodbg-bundle.tar'", prepare_command)
        self.assertEqual(len(upload_commands), 2)
        self.assertIn("printf '%s' 'QUJD'", upload_commands[0])
        self.assertIn("AUTODBG_SERIAL_BUNDLE_OK", finalize_command)
        self.assertIn("files=2", finalize_command)

    def test_build_auto_wlan_bootstrap_commands_uses_wifi_cmd_and_wlan_run(self) -> None:
        commands = _build_auto_wlan_bootstrap_commands(
            wifi_ssid="xiaotudou",
            wifi_password="12345678",
            wifi_mode="WPA2",
            network_dir="/mnt/sdcard/network",
        )
        self.assertEqual(len(commands), 2)
        self.assertIn('sh "$NET_DIR/wlan_run.sh" start', commands[0])
        self.assertIn('sh "$NET_DIR/wifi_cmd.sh" connect \'xiaotudou\' \'12345678\' \'WPA2\'', commands[1])

    def test_resolve_bootstrap_commands_can_auto_build_wlan_script_commands(self) -> None:
        commands = _resolve_bootstrap_commands(
            self.profiles,
            "wlan_script",
            [],
            wifi_ssid=None,
            wifi_password=None,
            wifi_mode=None,
            network_dir="/mnt/sdcard/network",
        )
        self.assertEqual(len(commands), 2)
        self.assertIn("/mnt/sdcard/network", commands[0])

    def test_default_base_url_uses_detected_host_ip(self) -> None:
        with patch("autodbg.cli.main.detect_host_ipv4", return_value="192.168.1.10"):
            self.assertEqual(_default_base_url("0.0.0.0", 8765), "http://192.168.1.10:8765")

    def test_default_payload_root_prefers_pull_probe_then_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            payloads_root = project_root / "payloads"
            payloads_root.mkdir(parents=True, exist_ok=True)
            with patch("autodbg.cli.main._project_root", return_value=project_root):
                self.assertEqual(_default_payload_root(), payloads_root)
            (payloads_root / "pull-probe").mkdir(parents=True, exist_ok=True)
            with patch("autodbg.cli.main._project_root", return_value=project_root):
                self.assertEqual(_default_payload_root(), payloads_root / "pull-probe")

    def test_build_parser_allows_run_without_explicit_profile_paths(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["run"])
        self.assertIsNone(args.device)
        self.assertIsNone(args.model)
        self.assertIsNone(args.task)
        self.assertIsNone(args.transport)

    def test_build_parser_accepts_quickstart_args(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "quickstart",
                "--goal",
                "健康检查",
                "--serial-port",
                "COM19",
                "--device-password-known",
                "--sdcard-drive",
                "E:",
            ]
        )

        self.assertEqual(args.command, "quickstart")
        self.assertEqual(args.goal, "健康检查")
        self.assertEqual(args.serial_port, "COM19")
        self.assertTrue(args.device_password_known)
        self.assertEqual(args.sdcard_drive, "E:")

    def test_build_quickstart_action_plan_guides_health_check(self) -> None:
        args = argparse.Namespace(
            goal="健康检查",
            serial_port="COM19",
            baudrate=115200,
            device_password_known=True,
            sdcard_drive=None,
            helper_cc=None,
            artifact=None,
        )

        plan = _build_quickstart_action_plan(args, ports=[])

        self.assertTrue(plan["ready"])
        self.assertEqual(plan["goal"], "health")
        self.assertEqual(plan["questions"], [])
        self.assertEqual(plan["next_requests"][0]["action"], "health")
        self.assertEqual(plan["next_requests"][0]["connection"]["serial_port"], "COM19")
        self.assertEqual(plan["next_requests"][0]["connection"]["device_password"], "<provided-secret>")

    def test_build_quickstart_action_plan_limits_questions_for_unknown_goal(self) -> None:
        args = argparse.Namespace(
            goal=None,
            serial_port=None,
            baudrate=None,
            device_password_known=False,
            sdcard_drive=None,
            helper_cc=None,
            artifact=None,
        )

        plan = _build_quickstart_action_plan(args, ports=[])

        self.assertFalse(plan["ready"])
        self.assertEqual(plan["goal"], "unknown")
        self.assertLessEqual(len(plan["questions"]), 3)
        self.assertEqual(plan["next_requests"][0]["action"], "ports")

    def test_build_parser_accepts_deploy_verify_closed_loop_args(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "deploy-verify",
                "--build-command",
                "make app",
                "--artifact",
                "payloads/APP.bin",
                "--post-pull-command",
                "lanupg /mnt/sdcard/APP.bin",
                "--reboot-command",
                "reboot",
                "--expect-marker",
                "LeCam ready",
                "--reject-marker",
                "panic",
                "--expected-version",
                "2.0.0.300",
            ]
        )

        self.assertEqual(args.command, "deploy-verify")
        self.assertEqual(args.build_command, "make app")
        self.assertEqual(args.artifact, Path("payloads/APP.bin"))
        self.assertEqual(args.post_pull_command, ["lanupg /mnt/sdcard/APP.bin"])
        self.assertEqual(args.reboot_command, "reboot")
        self.assertEqual(args.expect_marker, ["LeCam ready"])
        self.assertEqual(args.reject_marker, ["panic"])
        self.assertEqual(args.expected_version, "2.0.0.300")

    def test_build_parser_accepts_sd_http_helper_build_args(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "build-sd-http-helper",
                "--cc",
                "arm-linux-gnueabihf-gcc",
                "--output",
                "artifacts/autodbg-http-pull",
                "--cflag=-Wall",
                "--no-static",
            ]
        )

        self.assertEqual(args.command, "build-sd-http-helper")
        self.assertEqual(args.cc, "arm-linux-gnueabihf-gcc")
        self.assertEqual(args.output, Path("artifacts/autodbg-http-pull"))
        self.assertEqual(args.cflag, ["-Wall"])
        self.assertFalse(args.static)

    def test_evaluate_validation_spec_requires_expected_markers_and_rejects_bad_markers(self) -> None:
        validation = _evaluate_validation_spec(
            expect_markers=["ready"],
            reject_markers=["panic"],
            expected_version="2.0.0.300",
            observation={"last_lines": ["boot complete", "panic: demo"]},
            command_results=[
                {
                    "command": "cat /opt/appver.txt",
                    "exit_code": 0,
                    "output_lines": ["2.0.0.299"],
                }
            ],
        )

        self.assertEqual(validation["verdict"], "fail")
        check_names = [finding["check_name"] for finding in validation["findings"]]
        self.assertIn("expect_marker", check_names)
        self.assertIn("reject_marker", check_names)
        self.assertIn("expected_version", check_names)

    def test_load_profiles_from_args_uses_defaults_manifest(self) -> None:
        root = Path(__file__).resolve().parents[2]
        args = argparse.Namespace(
            device=None,
            model=None,
            task=None,
            transport=None,
            profiles_defaults=root / "profiles" / "defaults.toml",
            settings=root / "config" / "missing-user-settings.toml",
            serial_port="COM88",
            baudrate=None,
        )
        with patch("autodbg.cli.main._project_root", return_value=root):
            profiles = _load_profiles_from_args(args)
        self.assertEqual(profiles.device.device_id, "av130n-lab")
        self.assertEqual(profiles.device.serial.port, "COM88")

    def test_host_from_base_url_extracts_hostname(self) -> None:
        self.assertEqual(_host_from_base_url("http://192.168.1.10:8765/path"), "192.168.1.10")

    def test_command_run_collects_default_evidence_and_updates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            args = argparse.Namespace(
                device=Path("device.toml"),
                model=Path("model.toml"),
                task=Path("task.toml"),
                transport=Path("transport.toml"),
                artifacts_root=artifacts_root,
                settings=Path(temp_dir) / "user-settings.toml",
                serial_port=None,
                baudrate=None,
                observe_seconds=1.0,
                skip_evidence=False,
                evidence_timeout=12.5,
                validation_command=[],
                expect_marker=[],
                reject_marker=[],
                expected_version=None,
                git_commit=None,
                changed_file=[],
                expected_effect=None,
            )
            serial_handle = object()
            baseline_results = [
                {"name": "appver", "command": "cat /opt/appver.txt", "exit_code": 0, "output_lines": ["2.0.0.276"]},
                {
                    "name": "lecam_process",
                    "command": "ps | grep LeCam",
                    "exit_code": 0,
                    "output_lines": ["/opt/lecam/LeCam start"],
                },
                {
                    "name": "mmc_mount",
                    "command": "mount | grep mmc",
                    "exit_code": 0,
                    "output_lines": ["/dev/mmcblk0p1 on /mnt/sdcard type vfat"],
                },
                {"name": "sdcard_listing", "command": "ls /mnt/sdcard", "exit_code": 0, "output_lines": ["logprint"]},
            ]
            evidence_commands = _build_collect_evidence_commands(self.profiles)
            evidence_results = [
                {"name": name, "command": command, "exit_code": 0, "output_lines": [f"{name}-ok"]}
                for name, command in evidence_commands
            ]
            fetched_file = {
                "remote_path": "/etc/wlanname",
                "status": "ok",
                "artifact_path": "retrieved/20260416/run-evidence-file-1/etc/wlanname",
            }
            observation = ObservationResult(
                lines_captured=3,
                last_lines=["LeCam ready"],
                marker_hits=[
                    MarkerHit(
                        marker="LeCam ready",
                        line="LeCam ready",
                        tag="app_ready",
                        device_state="app_ready",
                    )
                ],
                last_device_state="app_ready",
            )

            with (
                patch("autodbg.cli.main._load_profiles_from_args", return_value=self.profiles),
                patch("autodbg.cli.main.SerialObserver.capture", return_value=observation),
                patch("autodbg.cli.main.open_serial_port", return_value=contextlib.nullcontext(serial_handle)),
                patch("autodbg.cli.main._run_baseline_check", side_effect=baseline_results) as baseline_mock,
                patch("autodbg.cli.main._execute_named_command", side_effect=evidence_results) as evidence_mock,
                patch("autodbg.cli.main._fetch_remote_file_artifact", return_value=fetched_file) as fetch_mock,
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_run(args)

            self.assertEqual(exit_code, 0)
            session_dir = SessionManager(artifacts_root).latest_session_dir()
            summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "run_completed")
            self.assertEqual(len(summary["run_results"]["baseline_checks"]), 4)
            self.assertEqual(len(summary["evidence_results"]["command_results"]), len(evidence_commands))
            self.assertEqual(summary["evidence_results"]["file_results"], [fetched_file])
            self.assertEqual(summary["evaluation"]["verdict"], "pass")
            self.assertEqual(summary["state"]["device_state"], "app_ready")
            fetch_mock.assert_called_once()
            self.assertEqual(fetch_mock.call_args.kwargs["remote_path"], "/etc/wlanname")
            self.assertIs(fetch_mock.call_args.kwargs["serial_port"], serial_handle)
            self.assertTrue(all(call.kwargs["serial_port"] is serial_handle for call in baseline_mock.call_args_list))
            self.assertTrue(all(call.kwargs["serial_port"] is serial_handle for call in evidence_mock.call_args_list))
            self.assertTrue(any("[DONE] Evidence commands:" in call.args[0] for call in print_mock.call_args_list))
            self.assertTrue(any("[DONE] Evidence files:" in call.args[0] for call in print_mock.call_args_list))

    def test_read_trace_entries_and_format_trace_entry(self) -> None:
        trace_dir = Path(__file__).resolve().parent
        trace_path = trace_dir / "sample-trace.jsonl"
        trace_path.write_text(
            '{"timestamp":"2026-04-16T03:30:00","port":"COM19","direction":"tx","payload":"root","pid":1234}\n',
            encoding="utf-8",
            newline="\n",
        )
        try:
            entries = _read_trace_entries(trace_path)
        finally:
            trace_path.unlink(missing_ok=True)
        self.assertEqual(len(entries), 1)
        self.assertEqual(_format_trace_entry(entries[0]), "[TX 03:30:00] root")

    def test_resolve_watch_start_index_skips_existing_lines_when_tail_zero(self) -> None:
        self.assertEqual(
            _resolve_watch_start_index(last_count=12, entry_count=15, tail=0, replay_existing=False),
            12,
        )
        self.assertEqual(
            _resolve_watch_start_index(last_count=0, entry_count=12, tail=3, replay_existing=True),
            9,
        )
        self.assertEqual(
            _resolve_watch_start_index(last_count=0, entry_count=1, tail=0, replay_existing=False),
            0,
        )
        self.assertEqual(
            _resolve_watch_start_index(last_count=7, entry_count=7, tail=0, replay_existing=False),
            7,
        )

    def test_command_watch_serial_raw_live_follow_warns_about_holding_port(self) -> None:
        args = argparse.Namespace(
            serial_port="COM19",
            tail=0,
            follow=True,
            show_system=False,
            raw_live=True,
            baudrate=115200,
            stdin_probe=False,
            stdin_shell=False,
            settings=Path(__file__).resolve().parents[2] / "config" / "user-settings.toml",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "com19.jsonl"
            broker_mock = unittest.mock.Mock()
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main._read_trace_entries", return_value=[]),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                patch("autodbg.cli.main.SerialBroker", return_value=broker_mock),
                patch("autodbg.cli.main.time.sleep", side_effect=KeyboardInterrupt),
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        broker_mock.start.assert_called_once()
        broker_mock.stop.assert_called_once()
        self.assertTrue(any("connected successfully" in call.args[0] for call in print_mock.call_args_list))
        self.assertTrue(
            any("keeps the physical serial port open" in call.args[0] for call in print_mock.call_args_list)
        )
        self.assertTrue(
            any("serial-broker stop --serial-port COM19" in call.args[0] for call in print_mock.call_args_list)
        )

    def test_command_watch_serial_can_use_serial_settings_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as settings_dir:
            settings_path = Path(settings_dir) / "user-settings.toml"
            settings_path.write_text(
                "\n".join(["[serial]", 'port = "COM19"', "baudrate = 115200", ""]),
                encoding="utf-8",
                newline="\n",
            )
            args = argparse.Namespace(
                serial_port=None,
                tail=0,
                follow=True,
                show_system=False,
                raw_live=True,
                baudrate=None,
                stdin_probe=False,
                stdin_shell=False,
                settings=settings_path,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                trace_path = Path(temp_dir) / "com19.jsonl"
                broker_mock = unittest.mock.Mock()
                with (
                    patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path) as trace_path_mock,
                    patch("autodbg.cli.main._read_trace_entries", return_value=[]),
                    patch("autodbg.cli.main.load_serial_broker_registry", return_value=None),
                    patch("autodbg.cli.main.SerialBroker", return_value=broker_mock) as broker_ctor,
                    patch("autodbg.cli.main.time.sleep", side_effect=KeyboardInterrupt),
                    patch("builtins.print") as print_mock,
                ):
                    exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        trace_path_mock.assert_called_once_with("COM19")
        broker_ctor.assert_called_once_with(serial_port="COM19", baudrate=115200)
        self.assertTrue(any("Watching shared serial trace for COM19" in call.args[0] for call in print_mock.call_args_list))

    def test_command_watch_serial_prefers_live_broker_stream_when_available(self) -> None:
        args = argparse.Namespace(
            serial_port="COM19",
            tail=0,
            follow=True,
            show_system=False,
            raw_live=False,
            baudrate=115200,
            stdin_probe=True,
            stdin_shell=False,
            settings=Path(__file__).resolve().parents[2] / "config" / "user-settings.toml",
        )
        registry = SerialBrokerRegistry(
            host="127.0.0.1",
            tcp_port=9001,
            pid=4321,
            serial_port="COM19",
            baudrate=115200,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "com19.jsonl"
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main._read_trace_entries", return_value=[]),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=registry),
                patch("autodbg.cli.main._follow_trace_via_broker", side_effect=KeyboardInterrupt) as follow_mock,
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        follow_mock.assert_called_once_with(
            args,
            registry,
            serial_port="COM19",
            baudrate=115200,
            show_system=False,
            stdin_probe=True,
            stdin_shell=False,
            initial_entries=[],
        )
        self.assertTrue(any("Streaming live broker events" in call.args[0] for call in print_mock.call_args_list))
        self.assertTrue(any("Press Enter in this window" in call.args[0] for call in print_mock.call_args_list))

    def test_command_watch_serial_enables_stdin_shell_with_live_broker(self) -> None:
        args = argparse.Namespace(
            serial_port="COM19",
            tail=0,
            follow=True,
            show_system=False,
            raw_live=False,
            baudrate=115200,
            stdin_probe=False,
            stdin_shell=True,
            device=None,
            model=None,
            task=None,
            transport=None,
            profiles_defaults=Path(__file__).resolve().parents[2] / "profiles" / "defaults.toml",
            settings=Path(__file__).resolve().parents[2] / "config" / "user-settings.toml",
        )
        registry = SerialBrokerRegistry(
            host="127.0.0.1",
            tcp_port=9001,
            pid=4321,
            serial_port="COM19",
            baudrate=115200,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            trace_path = Path(temp_dir) / "com19.jsonl"
            with (
                patch("autodbg.cli.main.serial_trace_log_path", return_value=trace_path),
                patch("autodbg.cli.main._read_trace_entries", return_value=[]),
                patch("autodbg.cli.main.load_serial_broker_registry", return_value=registry),
                patch("autodbg.cli.main._follow_trace_via_broker", side_effect=KeyboardInterrupt) as follow_mock,
                patch("builtins.print") as print_mock,
            ):
                exit_code = _command_watch_serial(args)

        self.assertEqual(exit_code, 0)
        follow_mock.assert_called_once_with(
            args,
            registry,
            serial_port="COM19",
            baudrate=115200,
            show_system=False,
            stdin_probe=False,
            stdin_shell=True,
            initial_entries=[],
        )
        self.assertTrue(any("Interactive serial shell enabled on COM19" in call.args[0] for call in print_mock.call_args_list))

    def test_consume_watch_stdin_probe_sends_newline_when_enter_is_pressed(self) -> None:
        sender = unittest.mock.Mock(return_value=(True, "Sent newline probe to COM19"))

        with patch("builtins.print") as print_mock:
            _consume_watch_stdin_probe(
                serial_port="COM19",
                baudrate=115200,
                enabled=True,
                key_reader=lambda: True,
                probe_sender=sender,
            )

        sender.assert_called_once_with("COM19", 115200)
        self.assertTrue(any("Sent newline probe to COM19" in call.args[0] for call in print_mock.call_args_list))

    def test_consume_watch_stdin_probe_does_nothing_when_not_triggered(self) -> None:
        sender = unittest.mock.Mock(return_value=(True, "unused"))

        _consume_watch_stdin_probe(
            serial_port="COM19",
            baudrate=115200,
            enabled=True,
            key_reader=lambda: False,
            probe_sender=sender,
        )

        sender.assert_not_called()

    def test_consume_watch_stdin_shell_sends_full_command_on_enter(self) -> None:
        sender = unittest.mock.Mock(return_value=(True, "Sent command to COM19: ls /"))
        state = _WatchStdinShellState()

        _consume_watch_stdin_shell(
            serial_port="COM19",
            baudrate=115200,
            enabled=True,
            state=state,
            char_reader=lambda: ["l", "s", " ", "/", "\r"],
            text_sender=sender,
            writer=io.StringIO(),
            printer=unittest.mock.Mock(),
        )

        sender.assert_called_once_with("COM19", 115200, "ls /\n")
        self.assertEqual(state.buffer, "")

    def test_consume_watch_stdin_shell_uses_empty_enter_as_newline_probe(self) -> None:
        sender = unittest.mock.Mock(return_value=(True, "Sent newline probe to COM19"))
        state = _WatchStdinShellState()

        _consume_watch_stdin_shell(
            serial_port="COM19",
            baudrate=115200,
            enabled=True,
            state=state,
            char_reader=lambda: ["\r"],
            text_sender=sender,
            writer=io.StringIO(),
            printer=unittest.mock.Mock(),
        )

        sender.assert_called_once_with("COM19", 115200, "\n")
        self.assertEqual(state.status_message, "Sent newline probe to COM19")

    def test_build_watch_tui_rows_returns_fixed_screen_layout(self) -> None:
        state = _WatchStdinShellState()
        state.status_message = "COM19 connected successfully @ 115200"
        state.recent_lines = [
            "[RX 01:02:03.000] line-one",
            "[TX 01:02:03.100] ls /",
        ]

        rows, cursor_col, cursor_row = _build_watch_tui_rows(
            state,
            serial_port="COM19",
            width=64,
            height=8,
            baudrate=115200,
        )

        self.assertEqual(len(rows), 8)
        self.assertTrue(all(len(row) == 64 for row in rows))
        self.assertTrue(rows[0].startswith("[AUTO-DEBUG SERIAL TUI] COM19 @ 115200"))
        self.assertIn("Ctrl+L=login", rows[0])
        self.assertIn("PgUp/", rows[0])
        self.assertNotIn("F2", rows[0])
        self.assertEqual(rows[-2].rstrip(), "Status: COM19 connected successfully @ 115200")
        self.assertEqual(rows[-1].rstrip(), "[INPUT COM19]")
        self.assertEqual(cursor_row, 7)
        self.assertGreaterEqual(cursor_col, len("[INPUT COM19] "))

    def test_build_watch_tui_rows_marks_history_offset_in_status_line(self) -> None:
        state = _WatchStdinShellState()
        state.scroll_offset = 3
        state.recent_lines = [f"[RX 01:02:03.{index:03d}] line-{index}" for index in range(10)]

        rows, _cursor_col, _cursor_row = _build_watch_tui_rows(
            state,
            serial_port="COM19",
            width=72,
            height=8,
            baudrate=115200,
        )

        self.assertIn("[history +3]", rows[-2])

    def test_page_watch_history_moves_between_live_edge_and_history(self) -> None:
        state = _WatchStdinShellState()
        state.recent_lines = [f"line-{index}" for index in range(12)]

        moved_up = _page_watch_history(state, height=8, direction="page_up")
        self.assertTrue(moved_up)
        self.assertEqual(state.scroll_offset, 3)

        moved_down = _page_watch_history(state, height=8, direction="page_down")
        self.assertTrue(moved_down)
        self.assertEqual(state.scroll_offset, 0)

    def test_watch_shell_read_chars_maps_extended_history_keys(self) -> None:
        class _FakeMsvcrt:
            def __init__(self, keys: list[str]) -> None:
                self._keys = list(keys)

            def kbhit(self) -> bool:
                return bool(self._keys)

            def getwch(self) -> str:
                return self._keys.pop(0)

        fake_msvcrt = _FakeMsvcrt(["\xe0", "I", "\x00", "Q", "\xe0", "G", "\xe0", "O", "x"])
        with patch.dict(sys.modules, {"msvcrt": fake_msvcrt}, clear=False):
            chars = _watch_shell_read_chars()

        self.assertEqual(
            chars,
            ["__PAGE_UP__", "__PAGE_DOWN__", "__SCROLL_TOP__", "__SCROLL_BOTTOM__", "x"],
        )

    def test_build_agent_tool_manifest_contains_core_actions(self) -> None:
        manifest = build_agent_tool_manifest(project_root=Path(__file__).resolve().parents[2])
        action_names = {item["name"] for item in manifest["actions"]}

        self.assertIn("run", action_names)
        self.assertIn("watch-serial", action_names)
        self.assertIn("device-pull", action_names)
        self.assertEqual(manifest["tool"]["name"], "embedded-device-auto-debug")

    def test_command_describe_agent_tool_outputs_json_manifest(self) -> None:
        args = argparse.Namespace(format="json", pretty=True)
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = _command_describe_agent_tool(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["tool"]["name"], "embedded-device-auto-debug")
        self.assertIn("actions", payload)

    def test_refresh_watch_status_message_resets_transient_state_back_to_default(self) -> None:
        state = _WatchStdinShellState()
        _set_watch_status_message(state, "Sent newline probe to COM19", transient_seconds=2.0)

        changed = _refresh_watch_status_message(state, now=state.status_expires_at + 0.1)

        self.assertTrue(changed)
        self.assertEqual(state.status_message, _default_watch_status())
        self.assertIsNone(state.status_expires_at)

    def test_consume_watch_stdin_shell_triggers_auto_login_hotkey(self) -> None:
        sender = unittest.mock.Mock()
        login_action = unittest.mock.Mock(return_value=(True, "Auto login reached the root shell on COM19"))
        state = _WatchStdinShellState()

        _consume_watch_stdin_shell(
            serial_port="COM19",
            baudrate=115200,
            enabled=True,
            state=state,
            char_reader=lambda: ["__AUTO_LOGIN__"],
            text_sender=sender,
            login_action=login_action,
            writer=io.StringIO(),
            printer=unittest.mock.Mock(),
        )

        sender.assert_not_called()
        login_action.assert_called_once_with()
        self.assertEqual(state.status_message, "Auto login reached the root shell on COM19")

    def test_run_watch_auto_login_prompts_for_password_after_failed_login(self) -> None:
        args = argparse.Namespace(
            serial_port="COM19",
            baudrate=115200,
            device=None,
            model=None,
            task=None,
            transport=None,
            profiles_defaults=Path(__file__).resolve().parents[2] / "profiles" / "defaults.toml",
            settings=Path(__file__).resolve().parents[2] / "config" / "user-settings.toml",
        )
        controller = unittest.mock.Mock()
        controller.device_profile = self.profiles.device
        controller.login.side_effect = [
            LoginResult(False, False, True, ["closeli login:", "Password:"]),
            LoginResult(True, True, True, ["[root@closeli:~]#"]),
        ]

        def fake_prompt_password(**_kwargs):
            os.environ["AUTO_DBG_DEVICE_PASSWORD"] = "new-secret"
            return "new-secret"

        with (
            patch("autodbg.cli.main._load_profiles_from_args", return_value=self.profiles),
            patch("autodbg.cli.main.DeviceController", return_value=controller),
            patch("autodbg.cli.main._prompt_watch_password", side_effect=fake_prompt_password),
            patch.dict(os.environ, {}, clear=True),
        ):
            statuses: list[str] = []
            ok, message = _run_watch_auto_login(
                args,
                serial_port="COM19",
                status_writer=statuses.append,
                writer=io.StringIO(),
            )
            self.assertEqual(os.environ.get("AUTO_DBG_DEVICE_PASSWORD"), "new-secret")

        self.assertTrue(ok)
        self.assertEqual(message, "Auto login reached the root shell on COM19")
        self.assertEqual(controller.login.call_count, 2)
        self.assertTrue(any("Login failed; enter the password" in message for message in statuses))

    def test_run_watch_auto_login_restores_original_password_env_after_failed_retries(self) -> None:
        args = argparse.Namespace(
            serial_port="COM19",
            baudrate=115200,
            device=None,
            model=None,
            task=None,
            transport=None,
            profiles_defaults=Path(__file__).resolve().parents[2] / "profiles" / "defaults.toml",
            settings=Path(__file__).resolve().parents[2] / "config" / "user-settings.toml",
        )
        controller = unittest.mock.Mock()
        controller.device_profile = self.profiles.device
        controller.login.side_effect = [
            LoginResult(False, False, True, ["closeli login:", "Password:"]),
            LoginResult(False, False, True, ["closeli login:", "Password:"]),
            LoginResult(False, False, True, ["closeli login:", "Password:"]),
            LoginResult(False, False, True, ["closeli login:", "Password:"]),
        ]

        with (
            patch("autodbg.cli.main._load_profiles_from_args", return_value=self.profiles),
            patch("autodbg.cli.main.DeviceController", return_value=controller),
            patch("autodbg.cli.main._prompt_watch_password", side_effect=["bad-1", "bad-2", "bad-3"]),
            patch.dict(os.environ, {"AUTO_DBG_DEVICE_PASSWORD": "old-secret"}, clear=False),
        ):
            statuses: list[str] = []
            ok, message = _run_watch_auto_login(
                args,
                serial_port="COM19",
                status_writer=statuses.append,
                writer=io.StringIO(),
            )
            self.assertEqual(os.environ.get("AUTO_DBG_DEVICE_PASSWORD"), "old-secret")

        self.assertFalse(ok)
        self.assertIn("failed after 3 password attempts", message)
        self.assertTrue(any("Retry password attempt 2/3" in message for message in statuses))

    def test_print_watch_trace_entry_formats_serial_connected_status(self) -> None:
        entry = SerialTraceEntry(
            timestamp="2026-04-19T21:55:00.123",
            port="COM19",
            direction="sys",
            payload="SERIAL_CONNECTED baudrate=115200",
            pid=1234,
        )

        with patch("builtins.print") as print_mock:
            _print_watch_trace_entry(entry, show_system=False)

        self.assertTrue(any("COM19 connected successfully @ 115200" in call.args[0] for call in print_mock.call_args_list))

    def test_print_watch_trace_entry_formats_serial_unavailable_status(self) -> None:
        entry = SerialTraceEntry(
            timestamp="2026-04-19T21:55:00.123",
            port="COM19",
            direction="sys",
            payload="SERIAL_UNAVAILABLE error=Permission denied",
            pid=1234,
        )

        with patch("builtins.print") as print_mock:
            _print_watch_trace_entry(entry, show_system=False)

        self.assertTrue(any("COM19 is unavailable: Permission denied" in call.args[0] for call in print_mock.call_args_list))
        self.assertTrue(any("Press Enter to retry reconnecting COM19." in call.args[0] for call in print_mock.call_args_list))

    def test_command_serial_broker_list_and_stop(self) -> None:
        registry = SerialBrokerRegistry(
            host="127.0.0.1",
            tcp_port=9001,
            pid=4321,
            serial_port="COM19",
            baudrate=115200,
        )

        list_args = argparse.Namespace(command="serial-broker", broker_command="list", serial_port=None)
        stop_args = argparse.Namespace(command="serial-broker", broker_command="stop", serial_port="COM19", all=False)

        with (
            patch("autodbg.cli.main.list_serial_broker_registries", return_value=[registry]),
            patch("autodbg.cli.main.stop_serial_broker", return_value=registry) as stop_mock,
            patch("builtins.print") as print_mock,
        ):
            self.assertEqual(_command_serial_broker(list_args), 0)
            self.assertEqual(_command_serial_broker(stop_args), 0)

        stop_mock.assert_called_once_with("COM19")
        self.assertTrue(any("Active raw serial brokers: 1" in call.args[0] for call in print_mock.call_args_list))
        self.assertTrue(any("Stopped raw serial broker on COM19" in call.args[0] for call in print_mock.call_args_list))


if __name__ == "__main__":
    unittest.main()
