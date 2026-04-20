import unittest

from autodbg.workflows.evaluation import evaluate_health_checks, evaluate_startup_run


class EvaluationTest(unittest.TestCase):
    def test_evaluate_health_checks_returns_partial_pass_for_mmc_warnings(self) -> None:
        checks = [
            {"name": "appver", "exit_code": 0, "output_lines": ["2.0.0.276"]},
            {"name": "lecam_process", "exit_code": 0, "output_lines": ["609 root /opt/lecam/LeCam start"]},
            {"name": "mmc_devices", "exit_code": 0, "output_lines": ["mmcblk0", "mmcblk0p1"]},
            {"name": "mmc_partitions", "exit_code": 0, "output_lines": ["179 0 61071360 mmcblk0", "179 1 58503296 mmcblk0p1"]},
            {"name": "mmc_mount", "exit_code": 0, "output_lines": ["/dev/mmcblk0p1 on /mnt/sdcard type vfat (rw,...)"]},
            {"name": "sdcard_listing", "exit_code": 0, "output_lines": ["System Volume Information", "logprint"]},
            {"name": "sdcard_capacity", "exit_code": 0, "output_lines": ["Filesystem Size Used Avail Use% Mounted on", "/dev/mmcblk0p1 55.8G 480.0K 55.8G 0% /mnt/sdcard"]},
            {"name": "sdcard_write_probe", "exit_code": 0, "output_lines": ["autodbg_probe", "PROBE_OK"]},
            {
                "name": "mmc_dmesg_tail",
                "exit_code": 0,
                "output_lines": [
                    "FAT-fs (mmcblk0p1): Volume was not properly unmounted. Some data may be corrupt. Please run fsck.",
                    "mmcblk0: error -110 sending status command, retrying",
                ],
            },
        ]

        evaluation = evaluate_health_checks(checks, app_name="LeCam")

        self.assertEqual(evaluation["verdict"], "partial_pass")
        self.assertEqual(len(evaluation["findings"]), 2)
        self.assertEqual(evaluation["highlights"]["app_version"], "2.0.0.276")
        self.assertTrue(evaluation["highlights"]["sd_write_probe_ok"])

    def test_evaluate_health_checks_returns_fail_when_storage_is_missing(self) -> None:
        checks = [
            {"name": "appver", "exit_code": 0, "output_lines": ["2.0.0.276"]},
            {"name": "lecam_process", "exit_code": 0, "output_lines": ["609 root /opt/lecam/LeCam start"]},
            {"name": "mmc_devices", "exit_code": 1, "output_lines": []},
            {"name": "mmc_partitions", "exit_code": 1, "output_lines": []},
            {"name": "mmc_mount", "exit_code": 1, "output_lines": []},
            {"name": "sdcard_listing", "exit_code": 1, "output_lines": []},
            {"name": "sdcard_capacity", "exit_code": 1, "output_lines": []},
            {"name": "mmc_dmesg_tail", "exit_code": 0, "output_lines": []},
        ]

        evaluation = evaluate_health_checks(checks, app_name="LeCam")

        self.assertEqual(evaluation["verdict"], "fail")
        self.assertGreaterEqual(len(evaluation["findings"]), 5)

    def test_evaluate_startup_run_warns_when_app_ready_is_missing(self) -> None:
        checks = [
            {"name": "appver", "exit_code": 0, "output_lines": ["2.0.0.276"]},
            {"name": "lecam_process", "exit_code": 1, "output_lines": []},
            {"name": "mmc_mount", "exit_code": 0, "output_lines": ["/dev/mmcblk0p1 on /mnt/sdcard type vfat (rw,...)"]},
            {"name": "sdcard_listing", "exit_code": 0, "output_lines": ["System Volume Information", "logprint"]},
        ]
        observation = {
            "lines_captured": 8,
            "last_device_state": None,
            "marker_hits": [],
        }

        evaluation = evaluate_startup_run(checks, app_name="LeCam", observation=observation)

        self.assertEqual(evaluation["verdict"], "fail")
        self.assertEqual(len(evaluation["findings"]), 2)

    def test_evaluate_startup_run_passes_when_app_is_running_but_ready_marker_was_missed(self) -> None:
        checks = [
            {"name": "appver", "exit_code": 0, "output_lines": ["2.0.0.276"]},
            {"name": "lecam_process", "exit_code": 0, "output_lines": ["609 root /opt/lecam/LeCam start"]},
            {"name": "mmc_mount", "exit_code": 0, "output_lines": ["/dev/mmcblk0p1 on /mnt/sdcard type vfat (rw,...)"]},
            {"name": "sdcard_listing", "exit_code": 0, "output_lines": ["System Volume Information", "logprint"]},
        ]
        observation = {
            "lines_captured": 3,
            "last_device_state": None,
            "marker_hits": [],
        }

        evaluation = evaluate_startup_run(checks, app_name="LeCam", observation=observation)

        self.assertEqual(evaluation["verdict"], "pass")
        self.assertEqual(evaluation["findings"], [])
        self.assertTrue(evaluation["highlights"]["app_process_seen"])
        self.assertFalse(evaluation["highlights"]["app_ready_seen"])

    def test_evaluate_startup_run_fails_on_panic_marker(self) -> None:
        checks = [
            {"name": "appver", "exit_code": 0, "output_lines": ["2.0.0.276"]},
            {"name": "lecam_process", "exit_code": 0, "output_lines": ["609 root /opt/lecam/LeCam start"]},
            {"name": "mmc_mount", "exit_code": 0, "output_lines": ["/dev/mmcblk0p1 on /mnt/sdcard type vfat (rw,...)"]},
            {"name": "sdcard_listing", "exit_code": 0, "output_lines": ["System Volume Information", "logprint"]},
        ]
        observation = {
            "lines_captured": 12,
            "last_device_state": "panic_or_hang",
            "marker_hits": [{"tag": "panic", "line": "start coredump..."}],
        }

        evaluation = evaluate_startup_run(checks, app_name="LeCam", observation=observation)

        self.assertEqual(evaluation["verdict"], "fail")
        self.assertTrue(any(finding["check_name"] == "serial_observation" for finding in evaluation["findings"]))


if __name__ == "__main__":
    unittest.main()
