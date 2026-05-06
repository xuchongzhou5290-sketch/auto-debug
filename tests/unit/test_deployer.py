from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from autodbg.deploy.deployer import Deployer
from autodbg.models.profile import TaskProfile, TransportProfile


class DeployerTest(unittest.TestCase):
    def test_stage_to_sd_copies_and_verifies_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "probe.txt"
            source_path.write_text("autodbg-stage-probe\n", encoding="utf-8", newline="\n")
            sd_drive = root / "sdcard"
            sd_drive.mkdir(parents=True, exist_ok=True)

            deployer = Deployer(
                task_profile=TaskProfile(
                    task_type="startup_check",
                    description="demo",
                    deploy_strategy="manual",
                    success_template="default",
                    evidence_template="default",
                ),
                transport_profile=TransportProfile(
                    transport_id="network_serial_fallback",
                    deploy_channels=["sdcard"],
                ),
            )

            with patch("autodbg.deploy.deployer.get_drive_info") as mocked_drive_info:
                mocked_drive_info.return_value.drive_type = "removable"
                mocked_drive_info.return_value.root = "R:\\"
                result = deployer.stage_to_sd(
                    source_path=source_path,
                    sdcard_drive=str(sd_drive),
                    target_subdir=r"debug\autodbg",
                )

            target_path = Path(result.target_path)
            self.assertTrue(target_path.exists())
            self.assertTrue(result.verified)
            self.assertEqual(target_path.read_text(encoding="utf-8"), "autodbg-stage-probe\n")

    def test_stage_to_sd_rejects_non_removable_drive_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "probe.txt"
            source_path.write_text("autodbg-stage-probe\n", encoding="utf-8", newline="\n")
            sd_drive = root / "sdcard"
            sd_drive.mkdir(parents=True, exist_ok=True)

            deployer = Deployer(
                task_profile=TaskProfile(
                    task_type="startup_check",
                    description="demo",
                    deploy_strategy="manual",
                    success_template="default",
                    evidence_template="default",
                ),
                transport_profile=TransportProfile(
                    transport_id="network_serial_fallback",
                    deploy_channels=["sdcard"],
                ),
            )

            with patch("autodbg.deploy.deployer.get_drive_info") as mocked_drive_info:
                mocked_drive_info.return_value.drive_type = "fixed"
                mocked_drive_info.return_value.root = "E:\\"
                with self.assertRaisesRegex(RuntimeError, "Refusing to stage onto non-removable drive"):
                    deployer.stage_to_sd(
                        source_path=source_path,
                        sdcard_drive=str(sd_drive),
                        target_subdir=r"debug\autodbg",
                    )

    def test_stage_to_sd_rejects_target_subdir_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "probe.txt"
            source_path.write_text("autodbg-stage-probe\n", encoding="utf-8", newline="\n")
            sd_drive = root / "sdcard"
            sd_drive.mkdir(parents=True, exist_ok=True)

            deployer = Deployer(
                task_profile=TaskProfile(
                    task_type="startup_check",
                    description="demo",
                    deploy_strategy="manual",
                    success_template="default",
                    evidence_template="default",
                ),
                transport_profile=TransportProfile(
                    transport_id="network_serial_fallback",
                    deploy_channels=["sdcard"],
                ),
            )

            with patch("autodbg.deploy.deployer.get_drive_info") as mocked_drive_info:
                mocked_drive_info.return_value.drive_type = "removable"
                mocked_drive_info.return_value.root = "R:\\"
                with self.assertRaisesRegex(ValueError, "escapes the SD card root"):
                    deployer.stage_to_sd(
                        source_path=source_path,
                        sdcard_drive=str(sd_drive),
                        target_subdir=str(Path("..") / "outside"),
                    )

    def test_stage_to_sd_rejects_destination_name_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "probe.txt"
            source_path.write_text("autodbg-stage-probe\n", encoding="utf-8", newline="\n")
            sd_drive = root / "sdcard"
            sd_drive.mkdir(parents=True, exist_ok=True)

            deployer = Deployer(
                task_profile=TaskProfile(
                    task_type="startup_check",
                    description="demo",
                    deploy_strategy="manual",
                    success_template="default",
                    evidence_template="default",
                ),
                transport_profile=TransportProfile(
                    transport_id="network_serial_fallback",
                    deploy_channels=["sdcard"],
                ),
            )

            with patch("autodbg.deploy.deployer.get_drive_info") as mocked_drive_info:
                mocked_drive_info.return_value.drive_type = "removable"
                mocked_drive_info.return_value.root = "R:\\"
                with self.assertRaisesRegex(ValueError, "must be a filename"):
                    deployer.stage_to_sd(
                        source_path=source_path,
                        sdcard_drive=str(sd_drive),
                        destination_name=str(Path("nested") / "probe.txt"),
                    )


if __name__ == "__main__":
    unittest.main()
