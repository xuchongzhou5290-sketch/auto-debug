from __future__ import annotations

import copy
from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib
from typing import Any

from autodbg.models.profile import NetworkSettings, RunProfiles, StorageSettings


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_optional_path(raw: Any, *, base_dir: Path) -> Path | None:
    text = _clean_optional_string(raw)
    if text is None:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = Path(os.path.abspath(base_dir / path))
    return path


def _split_csv_env(raw: str | None) -> list[str]:
    if raw is None:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(slots=True)
class UserSerialSettings:
    port: str | None = None
    baudrate: int | None = None
    login_prompt: str | None = None
    shell_prompt: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserSerialSettings":
        return cls(
            port=_clean_optional_string(raw.get("port")),
            baudrate=int(raw["baudrate"]) if raw.get("baudrate") is not None else None,
            login_prompt=_clean_optional_string(raw.get("login_prompt")),
            shell_prompt=_clean_optional_string(raw.get("shell_prompt")),
        )


@dataclass(slots=True)
class UserNetworkSettings:
    host_ip: str | None = None
    preferred_interfaces: list[str] = field(default_factory=list)
    expected_ip: str | None = None
    pull_base_url: str | None = None
    pull_workspace: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    wifi_mode: str | None = None
    network_dir: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "UserNetworkSettings":
        return cls(
            host_ip=_clean_optional_string(raw.get("host_ip")),
            preferred_interfaces=[
                str(item).strip()
                for item in raw.get("preferred_interfaces", [])
                if str(item).strip()
            ],
            expected_ip=_clean_optional_string(raw.get("expected_ip")),
            pull_base_url=_clean_optional_string(raw.get("pull_base_url")),
            pull_workspace=_clean_optional_string(raw.get("pull_workspace")),
            wifi_ssid=_clean_optional_string(raw.get("wifi_ssid")),
            wifi_password=_clean_optional_string(raw.get("wifi_password")),
            wifi_mode=_clean_optional_string(raw.get("wifi_mode")),
            network_dir=_clean_optional_string(raw.get("network_dir")),
        )


@dataclass(slots=True)
class UserStorageSettings:
    sdcard_drive: str | None = None
    retrieved_root: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, base_dir: Path) -> "UserStorageSettings":
        return cls(
            sdcard_drive=_clean_optional_string(raw.get("sdcard_drive")),
            retrieved_root=_resolve_optional_path(raw.get("retrieved_root"), base_dir=base_dir),
        )


@dataclass(slots=True)
class UserSettings:
    serial: UserSerialSettings = field(default_factory=UserSerialSettings)
    network: UserNetworkSettings = field(default_factory=UserNetworkSettings)
    storage: UserStorageSettings = field(default_factory=UserStorageSettings)
    source_path: Path | None = None


def default_user_settings_path(project_root: Path) -> Path:
    return project_root / "config" / "user-settings.toml"


def load_user_settings(path: Path | None) -> UserSettings:
    if path is None:
        settings = UserSettings()
        return _apply_environment_overrides(settings, base_dir=Path.cwd())
    resolved_path = path.absolute()
    if not resolved_path.exists():
        settings = UserSettings(source_path=resolved_path)
        return _apply_environment_overrides(settings, base_dir=resolved_path.parent)
    with resolved_path.open("rb") as handle:
        raw = tomllib.load(handle)
    settings = UserSettings(
        serial=UserSerialSettings.from_dict(raw.get("serial", {})),
        network=UserNetworkSettings.from_dict(raw.get("network", {})),
        storage=UserStorageSettings.from_dict(raw.get("storage", {}), base_dir=resolved_path.parent),
        source_path=resolved_path,
    )
    return _apply_environment_overrides(settings, base_dir=resolved_path.parent)


def apply_user_settings(profiles: RunProfiles, settings: UserSettings) -> RunProfiles:
    effective = copy.deepcopy(profiles)

    serial = settings.serial
    if serial.port:
        effective.device.serial.port = serial.port
    if serial.baudrate is not None:
        effective.device.serial.baudrate = serial.baudrate
    if serial.login_prompt:
        effective.device.serial.login_prompt = serial.login_prompt
    if serial.shell_prompt:
        effective.device.serial.shell_prompt = serial.shell_prompt

    if _settings_has_network_overrides(settings.network):
        if effective.device.network is None:
            effective.device.network = NetworkSettings()
        network = effective.device.network
        if settings.network.host_ip:
            network.host_ip = settings.network.host_ip
        if settings.network.preferred_interfaces:
            network.preferred_interfaces = list(settings.network.preferred_interfaces)
        if settings.network.expected_ip:
            network.expected_ip = settings.network.expected_ip
        if settings.network.pull_base_url:
            network.pull_base_url = settings.network.pull_base_url
        if settings.network.pull_workspace:
            network.pull_workspace = settings.network.pull_workspace
        if settings.network.wifi_ssid:
            network.wifi_ssid = settings.network.wifi_ssid
        if settings.network.wifi_password is not None:
            network.wifi_password = settings.network.wifi_password
        if settings.network.wifi_mode:
            network.wifi_mode = settings.network.wifi_mode
        if settings.network.network_dir:
            network.network_dir = settings.network.network_dir

    if _settings_has_storage_overrides(settings.storage):
        if effective.device.storage is None:
            effective.device.storage = StorageSettings()
        storage = effective.device.storage
        if settings.storage.sdcard_drive:
            storage.sdcard_drive = settings.storage.sdcard_drive
        if settings.storage.retrieved_root is not None:
            storage.retrieved_root = str(settings.storage.retrieved_root)

    return effective


def _settings_has_network_overrides(settings: UserNetworkSettings) -> bool:
    return any(
        (
            settings.host_ip,
            settings.preferred_interfaces,
            settings.expected_ip,
            settings.pull_base_url,
            settings.pull_workspace,
            settings.wifi_ssid,
            settings.wifi_password is not None,
            settings.wifi_mode,
            settings.network_dir,
        )
    )


def _settings_has_storage_overrides(settings: UserStorageSettings) -> bool:
    return bool(settings.sdcard_drive or settings.retrieved_root is not None)


def _apply_environment_overrides(settings: UserSettings, *, base_dir: Path) -> UserSettings:
    effective = copy.deepcopy(settings)

    serial_port = _clean_optional_string(os.getenv("AUTO_DBG_SERIAL_PORT"))
    if serial_port:
        effective.serial.port = serial_port
    serial_baudrate = _clean_optional_string(os.getenv("AUTO_DBG_SERIAL_BAUDRATE"))
    if serial_baudrate is not None:
        effective.serial.baudrate = int(serial_baudrate)
    login_prompt = _clean_optional_string(os.getenv("AUTO_DBG_LOGIN_PROMPT"))
    if login_prompt:
        effective.serial.login_prompt = login_prompt
    shell_prompt = _clean_optional_string(os.getenv("AUTO_DBG_SHELL_PROMPT"))
    if shell_prompt:
        effective.serial.shell_prompt = shell_prompt

    host_ip = _clean_optional_string(os.getenv("AUTO_DBG_HOST_IP"))
    if host_ip:
        effective.network.host_ip = host_ip
    preferred_interfaces = _split_csv_env(os.getenv("AUTO_DBG_PREFERRED_INTERFACES"))
    if preferred_interfaces:
        effective.network.preferred_interfaces = preferred_interfaces
    expected_ip = _clean_optional_string(os.getenv("AUTO_DBG_EXPECTED_IP"))
    if expected_ip:
        effective.network.expected_ip = expected_ip
    pull_base_url = _clean_optional_string(os.getenv("AUTO_DBG_PULL_BASE_URL"))
    if pull_base_url:
        effective.network.pull_base_url = pull_base_url
    pull_workspace = _clean_optional_string(os.getenv("AUTO_DBG_PULL_WORKSPACE"))
    if pull_workspace:
        effective.network.pull_workspace = pull_workspace
    wifi_ssid = _clean_optional_string(os.getenv("AUTO_DBG_WIFI_SSID"))
    if wifi_ssid:
        effective.network.wifi_ssid = wifi_ssid
    wifi_password = os.getenv("AUTO_DBG_WIFI_PASSWORD")
    if wifi_password is not None:
        effective.network.wifi_password = wifi_password
    wifi_mode = _clean_optional_string(os.getenv("AUTO_DBG_WIFI_MODE"))
    if wifi_mode:
        effective.network.wifi_mode = wifi_mode
    network_dir = _clean_optional_string(os.getenv("AUTO_DBG_NETWORK_DIR"))
    if network_dir:
        effective.network.network_dir = network_dir

    sdcard_drive = _clean_optional_string(os.getenv("AUTO_DBG_SDCARD_DRIVE"))
    if sdcard_drive:
        effective.storage.sdcard_drive = sdcard_drive
    retrieved_root = _resolve_optional_path(os.getenv("AUTO_DBG_RETRIEVED_ROOT"), base_dir=base_dir)
    if retrieved_root is not None:
        effective.storage.retrieved_root = retrieved_root

    return effective
