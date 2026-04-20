from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SerialSettings:
    port: str
    baudrate: int = 115200
    login_prompt: str = "login:"
    shell_prompt: str = "#"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SerialSettings":
        return cls(
            port=str(raw["port"]),
            baudrate=int(raw.get("baudrate", 115200)),
            login_prompt=str(raw.get("login_prompt", "login:")),
            shell_prompt=str(raw.get("shell_prompt", "#")),
        )


@dataclass(slots=True)
class Credentials:
    username: str
    password: str | None = None
    password_env: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Credentials":
        return cls(
            username=str(raw["username"]),
            password=raw.get("password"),
            password_env=raw.get("password_env"),
        )


@dataclass(slots=True)
class NetworkSettings:
    host_ip: str | None = None
    preferred_interfaces: list[str] = field(default_factory=list)
    expected_ip: str | None = None
    discovery_rule: str | None = None
    bootstrap_commands: list[str] = field(default_factory=list)
    connectivity_checks: list[str] = field(default_factory=list)
    pull_base_url: str | None = None
    pull_workspace: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None
    wifi_mode: str | None = None
    network_dir: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NetworkSettings":
        return cls(
            host_ip=raw.get("host_ip"),
            preferred_interfaces=[str(item) for item in raw.get("preferred_interfaces", [])],
            expected_ip=raw.get("expected_ip"),
            discovery_rule=raw.get("discovery_rule"),
            bootstrap_commands=[str(item) for item in raw.get("bootstrap_commands", [])],
            connectivity_checks=[str(item) for item in raw.get("connectivity_checks", [])],
            pull_base_url=raw.get("pull_base_url"),
            pull_workspace=raw.get("pull_workspace"),
            wifi_ssid=raw.get("wifi_ssid"),
            wifi_password=raw.get("wifi_password"),
            wifi_mode=raw.get("wifi_mode"),
            network_dir=raw.get("network_dir"),
        )


@dataclass(slots=True)
class StorageSettings:
    sdcard_drive: str | None = None
    retrieved_root: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StorageSettings":
        return cls(
            sdcard_drive=raw.get("sdcard_drive"),
            retrieved_root=raw.get("retrieved_root"),
        )


@dataclass(slots=True)
class DeviceProfile:
    device_id: str
    model_id: str
    serial: SerialSettings
    credentials: Credentials | None = None
    network: NetworkSettings | None = None
    storage: StorageSettings | None = None
    notes: str | None = None
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: Path) -> "DeviceProfile":
        return cls(
            device_id=str(raw["device_id"]),
            model_id=str(raw["model_id"]),
            serial=SerialSettings.from_dict(raw["serial"]),
            credentials=Credentials.from_dict(raw["credentials"]) if "credentials" in raw else None,
            network=NetworkSettings.from_dict(raw["network"]) if "network" in raw else None,
            storage=StorageSettings.from_dict(raw["storage"]) if "storage" in raw else None,
            notes=raw.get("notes"),
            source_path=source_path,
        )


@dataclass(slots=True)
class ModelProfile:
    model_id: str
    platform: str
    app_name: str
    app_start_markers: list[str] = field(default_factory=list)
    app_ready_markers: list[str] = field(default_factory=list)
    panic_markers: list[str] = field(default_factory=list)
    artifact_paths: list[str] = field(default_factory=list)
    debug_workspace: str = "/tmp/debug"
    supported_actions: list[str] = field(default_factory=list)
    log_control_strategy: str = "manual"
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: Path) -> "ModelProfile":
        return cls(
            model_id=str(raw["model_id"]),
            platform=str(raw["platform"]),
            app_name=str(raw["app_name"]),
            app_start_markers=[str(item) for item in raw.get("app_start_markers", [])],
            app_ready_markers=[str(item) for item in raw.get("app_ready_markers", [])],
            panic_markers=[str(item) for item in raw.get("panic_markers", [])],
            artifact_paths=[str(item) for item in raw.get("artifact_paths", [])],
            debug_workspace=str(raw.get("debug_workspace", "/tmp/debug")),
            supported_actions=[str(item) for item in raw.get("supported_actions", [])],
            log_control_strategy=str(raw.get("log_control_strategy", "manual")),
            source_path=source_path,
        )


@dataclass(slots=True)
class TaskProfile:
    task_type: str
    description: str
    deploy_strategy: str
    success_template: str
    evidence_template: str
    initial_workflow: str = "startup_check"
    manual_check_items: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: Path) -> "TaskProfile":
        return cls(
            task_type=str(raw["task_type"]),
            description=str(raw.get("description", "")),
            deploy_strategy=str(raw.get("deploy_strategy", "manual")),
            success_template=str(raw.get("success_template", "default")),
            evidence_template=str(raw.get("evidence_template", "default")),
            initial_workflow=str(raw.get("initial_workflow", "startup_check")),
            manual_check_items=[str(item) for item in raw.get("manual_check_items", [])],
            source_path=source_path,
        )


@dataclass(slots=True)
class TransportProfile:
    transport_id: str
    control_channels: list[str] = field(default_factory=list)
    deploy_channels: list[str] = field(default_factory=list)
    artifact_pull_channels: list[str] = field(default_factory=list)
    fallback_order: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any], source_path: Path) -> "TransportProfile":
        return cls(
            transport_id=str(raw["transport_id"]),
            control_channels=[str(item) for item in raw.get("control_channels", [])],
            deploy_channels=[str(item) for item in raw.get("deploy_channels", [])],
            artifact_pull_channels=[str(item) for item in raw.get("artifact_pull_channels", [])],
            fallback_order=[str(item) for item in raw.get("fallback_order", [])],
            source_path=source_path,
        )


@dataclass(slots=True)
class RunProfiles:
    device: DeviceProfile
    model: ModelProfile
    task: TaskProfile
    transport: TransportProfile

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for section in payload.values():
            section.pop("source_path", None)
        credentials = payload.get("device", {}).get("credentials")
        if isinstance(credentials, dict) and credentials.get("password") is not None:
            credentials["password"] = "[redacted]"
        network = payload.get("device", {}).get("network")
        if isinstance(network, dict) and network.get("wifi_password") is not None:
            network["wifi_password"] = "[redacted]"
        return payload
