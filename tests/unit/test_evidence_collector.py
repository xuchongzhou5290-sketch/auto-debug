from pathlib import Path
import json
import tempfile
import unittest

from autodbg.evidence.collector import EvidenceCollector
from autodbg.models.profile import DeviceProfile, ModelProfile, RunProfiles, SerialSettings, TaskProfile, TransportProfile
from autodbg.models.session import SessionContext, SessionPaths
from autodbg.state.machine import StateSnapshot


class EvidenceCollectorTest(unittest.TestCase):
    def test_write_binary_artifact_registers_manifest_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "session"
            root.mkdir(parents=True, exist_ok=True)
            session = SessionContext.create(
                session_id="demo-session",
                device_id="av130n-lab",
                task_type="startup_check",
                session_paths=SessionPaths(
                    root=root,
                    core_dir=root / "core",
                    deploy_dir=root / "deploy",
                    logs_dir=root / "logs",
                    retrieved_dir=Path(temp_dir) / "retrieved" / "demo-session",
                ),
            )
            for path in (
                session.session_paths.core_dir,
                session.session_paths.deploy_dir,
                session.session_paths.logs_dir,
                session.session_paths.retrieved_dir,
            ):
                path.mkdir(parents=True, exist_ok=True)

            collector = EvidenceCollector(session)
            collector.bootstrap(
                profiles=RunProfiles(
                    device=DeviceProfile(
                        device_id="av130n-lab",
                        model_id="ak-av130n-ucm55me2",
                        serial=SerialSettings(port="COM19"),
                    ),
                    model=ModelProfile(
                        model_id="ak-av130n-ucm55me2",
                        platform="AK_AV130N",
                        app_name="LeCam",
                    ),
                    task=TaskProfile(
                        task_type="startup_check",
                        description="demo",
                        deploy_strategy="manual",
                        success_template="default",
                        evidence_template="default",
                        manual_check_items=["Confirm whether PTZ behavior matches expectation."],
                    ),
                    transport=TransportProfile(
                        transport_id="network_serial_fallback",
                        control_channels=["serial"],
                    ),
                ),
                state_snapshot=StateSnapshot(),
                workflow_name="startup_check",
                workflow_steps=[],
                plan_details={"control": {"mode": "demo"}},
            )
            artifact_path = collector.write_retrieved_artifact("fetched/sample.bin", b"abc123", artifact_type="retrieved_file")
            manifest = json.loads(collector.manifest_path.read_text(encoding="utf-8"))
            artifact_exists = artifact_path.exists()
            artifact_bytes = artifact_path.read_bytes()
            fetched_registered = any(item["type"] == "retrieved_file" for item in manifest["artifacts"])

        self.assertTrue(artifact_exists)
        self.assertEqual(artifact_bytes, b"abc123")
        self.assertTrue(fetched_registered)
        self.assertIn("retrieved", artifact_path.parts)

    def test_bootstrap_and_update_summary_write_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "session"
            root.mkdir(parents=True, exist_ok=True)
            session = SessionContext.create(
                session_id="demo-session",
                device_id="av130n-lab",
                task_type="startup_check",
                session_paths=SessionPaths(
                    root=root,
                    core_dir=root / "core",
                    deploy_dir=root / "deploy",
                    logs_dir=root / "logs",
                    retrieved_dir=Path(temp_dir) / "retrieved" / "demo-session",
                ),
            )
            for path in (
                session.session_paths.core_dir,
                session.session_paths.deploy_dir,
                session.session_paths.logs_dir,
                session.session_paths.retrieved_dir,
            ):
                path.mkdir(parents=True, exist_ok=True)

            collector = EvidenceCollector(session)
            collector.bootstrap(
                profiles=RunProfiles(
                    device=DeviceProfile(
                        device_id="av130n-lab",
                        model_id="ak-av130n-ucm55me2",
                        serial=SerialSettings(port="COM19"),
                    ),
                    model=ModelProfile(
                        model_id="ak-av130n-ucm55me2",
                        platform="AK_AV130N",
                        app_name="LeCam",
                    ),
                    task=TaskProfile(
                        task_type="startup_check",
                        description="demo",
                        deploy_strategy="manual",
                        success_template="default",
                        evidence_template="default",
                        manual_check_items=["Confirm whether PTZ behavior matches expectation."],
                    ),
                    transport=TransportProfile(
                        transport_id="network_serial_fallback",
                        control_channels=["serial"],
                    ),
                ),
                state_snapshot=StateSnapshot(),
                workflow_name="startup_check",
                workflow_steps=[],
                plan_details={"control": {"mode": "demo"}},
            )
            collector.update_summary(
                {
                    "status": "health_completed",
                    "evaluation": {
                        "verdict": "partial_pass",
                        "summary": "1 warning(s) detected.",
                        "findings": [
                            {
                                "level": "warning",
                                "check_name": "mmc_dmesg_tail",
                                "message": "FAT filesystem reports the card was not cleanly unmounted.",
                            }
                        ],
                        "highlights": {"app_version": "2.0.0.276"},
                    },
                    "evidence_results": {
                        "command_results": [
                            {"name": "uname", "exit_code": 0, "output_lines": ["Linux demo"]},
                            {"name": "artifact_path_1", "exit_code": 1, "output_lines": []},
                        ],
                        "file_results": [{"remote_path": "/etc/wlanname", "status": "ok"}],
                    },
                }
            )

            report_text = collector.report_path.read_text(encoding="utf-8")
            manifest = json.loads(collector.manifest_path.read_text(encoding="utf-8"))

        self.assertIn("# Session Report", report_text)
        self.assertIn("`partial_pass`", report_text)
        self.assertIn("FAT filesystem reports the card was not cleanly unmounted.", report_text)
        self.assertIn("Confirm whether PTZ behavior matches expectation.", report_text)
        self.assertIn("## Evidence", report_text)
        self.assertIn("Command failures", report_text)
        self.assertTrue(any(item["type"] == "report" for item in manifest["artifacts"]))


if __name__ == "__main__":
    unittest.main()
