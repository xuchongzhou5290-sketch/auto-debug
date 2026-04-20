from pathlib import Path
import unittest
from unittest.mock import patch

from autodbg.config import apply_user_settings, load_user_settings
from autodbg.profiles.loader import load_profile_defaults, load_run_profiles, resolve_run_profile_paths


class ProfileLoaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[2]

    def test_load_run_profiles(self) -> None:
        profiles = load_run_profiles(
            self.root / "profiles" / "devices" / "av130n-lab.toml",
            self.root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            self.root / "profiles" / "tasks" / "startup-check.toml",
            self.root / "profiles" / "transports" / "network-serial-fallback.toml",
        )

        self.assertEqual(profiles.device.device_id, "av130n-lab")
        self.assertEqual(profiles.device.network.bootstrap_commands, [])
        self.assertEqual(profiles.device.network.connectivity_checks, [])
        self.assertEqual(profiles.device.network.pull_workspace, "/mnt/sdcard/autodbg")
        self.assertEqual(profiles.device.network.wifi_ssid, "xiaotudou")
        self.assertEqual(profiles.device.network.wifi_password, "12345678")
        self.assertEqual(profiles.device.network.wifi_mode, "WPA2")
        self.assertEqual(profiles.device.network.network_dir, "/opt/network")
        self.assertEqual(profiles.model.app_name, "LeCam")
        self.assertEqual(profiles.task.task_type, "startup_check")
        self.assertEqual(profiles.transport.transport_id, "network_serial_fallback")

    def test_user_settings_overlay_updates_common_local_fields(self) -> None:
        profiles = load_run_profiles(
            self.root / "profiles" / "devices" / "av130n-lab.toml",
            self.root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            self.root / "profiles" / "tasks" / "startup-check.toml",
            self.root / "profiles" / "transports" / "network-serial-fallback.toml",
        )
        settings = load_user_settings(self.root / "config" / "user-settings.toml")
        effective = apply_user_settings(profiles, settings)

        self.assertEqual(effective.device.serial.port, "COM19")
        self.assertEqual(effective.device.serial.baudrate, 115200)
        self.assertEqual(
            effective.device.network.preferred_interfaces,
            ["eth0", "wlan0", "usb0", "wlan1", "ra0", "apcli0"],
        )
        self.assertEqual(effective.device.network.pull_workspace, "/mnt/sdcard/autodbg")
        self.assertEqual(
            Path(effective.device.storage.retrieved_root).resolve(),
            (self.root / "retrieved").resolve(),
        )

    def test_profile_defaults_manifest_resolves_expected_profile_paths(self) -> None:
        defaults = load_profile_defaults(self.root / "profiles" / "defaults.toml")
        self.assertEqual(defaults["device"], (self.root / "profiles" / "devices" / "av130n-lab.toml").resolve())
        self.assertEqual(
            resolve_run_profile_paths(
                self.root,
                device_path=None,
                model_path=None,
                task_path=None,
                transport_path=None,
                defaults_path=self.root / "profiles" / "defaults.toml",
            ),
            (
                (self.root / "profiles" / "devices" / "av130n-lab.toml").resolve(),
                (self.root / "profiles" / "models" / "ak-av130n-ucm55me2.toml").resolve(),
                (self.root / "profiles" / "tasks" / "startup-check.toml").resolve(),
                (self.root / "profiles" / "transports" / "network-serial-fallback.toml").resolve(),
            ),
        )

    def test_environment_overrides_apply_without_editing_settings_file(self) -> None:
        profiles = load_run_profiles(
            self.root / "profiles" / "devices" / "av130n-lab.toml",
            self.root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            self.root / "profiles" / "tasks" / "startup-check.toml",
            self.root / "profiles" / "transports" / "network-serial-fallback.toml",
        )
        settings_path = self.root / "config" / "env-only-settings.toml"
        with patch.dict(
            "os.environ",
            {
                "AUTO_DBG_SERIAL_PORT": "COM77",
                "AUTO_DBG_PREFERRED_INTERFACES": "usb0,wlan0",
                "AUTO_DBG_WIFI_SSID": "demo-ssid",
                "AUTO_DBG_WIFI_PASSWORD": "demo-pass",
                "AUTO_DBG_SDCARD_DRIVE": "F:\\",
                "AUTO_DBG_RETRIEVED_ROOT": "../retrieved-env",
            },
            clear=False,
        ):
            settings = load_user_settings(settings_path)
        effective = apply_user_settings(profiles, settings)

        self.assertEqual(effective.device.serial.port, "COM77")
        self.assertEqual(effective.device.network.preferred_interfaces, ["usb0", "wlan0"])
        self.assertEqual(effective.device.network.wifi_ssid, "demo-ssid")
        self.assertEqual(effective.device.network.wifi_password, "demo-pass")
        self.assertEqual(effective.device.storage.sdcard_drive, "F:\\")
        self.assertEqual(
            Path(effective.device.storage.retrieved_root).resolve(),
            (self.root / "retrieved-env").resolve(),
        )

    def test_run_profiles_to_dict_redacts_sensitive_fields(self) -> None:
        profiles = load_run_profiles(
            self.root / "profiles" / "devices" / "av130n-lab.toml",
            self.root / "profiles" / "models" / "ak-av130n-ucm55me2.toml",
            self.root / "profiles" / "tasks" / "startup-check.toml",
            self.root / "profiles" / "transports" / "network-serial-fallback.toml",
        )
        profiles.device.credentials.password = "root-secret"
        profiles.device.network.wifi_password = "wifi-secret"

        payload = profiles.to_dict()

        self.assertEqual(payload["device"]["credentials"]["password"], "[redacted]")
        self.assertEqual(payload["device"]["network"]["wifi_password"], "[redacted]")


if __name__ == "__main__":
    unittest.main()
