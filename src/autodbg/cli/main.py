from __future__ import annotations

import base64
import argparse
import copy
import getpass
import io
import hashlib
import json
import os
import subprocess
import sys
from importlib import resources
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath
import socket
import time
from typing import Any
from urllib.parse import urlparse

from autodbg.agent import (
    AgentCallError,
    build_agent_error_response,
    build_agent_tool_manifest,
    build_agent_invocation,
    build_agent_response,
    execute_agent_request,
    list_session_dirs,
    load_agent_request,
    render_agent_tool_markdown,
    temporary_agent_environment,
)
from autodbg.config import apply_user_settings, default_user_settings_path, load_user_settings
from autodbg.control.controller import CommandResult, DeviceController, LoginRequiredError
from autodbg.deploy.deployer import Deployer
from autodbg.deploy import LOCAL_TOOL_NAME, install_local_tool
from autodbg.evidence.collector import EvidenceCollector
from autodbg.host.bundle import build_serial_bundle, split_base64_payload
from autodbg.host.artifact_server import (
    DEFAULT_HEALTH_NAME,
    ArtifactServerRegistry,
    artifact_server_log_path,
    artifact_server_pid_is_running,
    find_available_port,
    is_tcp_port_available,
    list_artifact_server_registries,
    load_artifact_server_registry,
    probe_http_url,
    remove_artifact_server_registry,
    serve_directory,
    stop_artifact_server,
    write_artifact_server_registry,
    write_manifest,
    write_pull_script,
    write_transfer_list,
)
from autodbg.host.network import detect_host_ipv4
from autodbg.host.storage import get_drive_info, list_host_drives
from autodbg.mcp.install import HOME_PLUGIN_NAME, install_home_plugin
from autodbg.models.session import SessionContext, SessionPaths
from autodbg.profiles.loader import (
    ProfileResolutionError,
    default_profile_defaults_path,
    load_device_profile,
    load_run_profiles,
    resolve_run_profile_paths,
)
from autodbg.serial.broker import SerialBroker
from autodbg.serial.observer import MarkerHit, SerialObserver
from autodbg.serial.runtime import (
    SerialSupportError,
    SerialTraceStreamClient,
    SerialTraceEntry,
    list_serial_ports,
    list_serial_broker_registries,
    load_serial_broker_registry,
    open_serial_port,
    serial_trace_log_path,
    stop_serial_broker,
)
from autodbg.session.contract import build_excerpt, build_result_contract
from autodbg.session.manager import SessionManager
from autodbg.state.machine import DeviceState, StateSnapshot, TaskState
from autodbg.workflows.evaluation import evaluate_health_checks, evaluate_startup_run
from autodbg.workflows.runner import WorkflowRunner

_FETCH_META_PREFIX = "__AUTODBG_META__"
_FETCH_B64_PREFIX = "__AUTODBG_B64__"
_STRUCTURED_OUTPUT_PREFIX = "__AUTODBG_CMD__"
_DEFAULT_PREFERRED_INTERFACES = ("eth0", "wlan0", "usb0", "wlan1", "ra0", "apcli0")
_RUN_DEFAULT_EVIDENCE_TIMEOUT = 30.0


def _project_root() -> Path:
    return Path(__file__).absolute().parents[3]


def _default_settings_path() -> Path:
    return default_user_settings_path(_project_root())


def _default_profile_defaults_path() -> Path:
    return default_profile_defaults_path(_project_root())


def _default_payload_root() -> Path:
    project_root = _project_root()
    preferred = project_root / "payloads" / "pull-probe"
    if preferred.is_dir():
        return preferred
    return project_root / "payloads"


def _load_user_settings_from_args(args: argparse.Namespace):
    cached = getattr(args, "_autodbg_user_settings", None)
    if cached is not None:
        return cached
    settings = load_user_settings(getattr(args, "settings", _default_settings_path()))
    setattr(args, "_autodbg_user_settings", settings)
    return settings


def _load_profiles_from_args(args: argparse.Namespace):
    device_path, model_path, task_path, transport_path = resolve_run_profile_paths(
        _project_root(),
        device_path=getattr(args, "device", None),
        model_path=getattr(args, "model", None),
        task_path=getattr(args, "task", None),
        transport_path=getattr(args, "transport", None),
        defaults_path=getattr(args, "profiles_defaults", _default_profile_defaults_path()),
    )
    profiles = load_run_profiles(device_path, model_path, task_path, transport_path)
    profiles = apply_user_settings(profiles, _load_user_settings_from_args(args))
    if getattr(args, "serial_port", None):
        profiles = copy.deepcopy(profiles)
        profiles.device.serial.port = args.serial_port
    if getattr(args, "baudrate", None):
        profiles = copy.deepcopy(profiles)
        profiles.device.serial.baudrate = args.baudrate
    return profiles


def _resolve_serial_connection_from_settings(
    args: argparse.Namespace,
    *,
    serial_port: str | None,
    baudrate: int | None,
    require_port: bool = True,
) -> tuple[str | None, int]:
    settings = _load_user_settings_from_args(args)
    resolved_port = serial_port or settings.serial.port
    resolved_baudrate = baudrate or settings.serial.baudrate or 115200
    if require_port and not resolved_port:
        raise ValueError(
            "No serial port was provided. Set AUTO_DBG_SERIAL_PORT, "
            "fill config/user-settings.toml, or pass --serial-port."
        )
    return resolved_port, resolved_baudrate


def _add_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        type=Path,
        help="Optional path to the device profile TOML; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Optional path to the model profile TOML; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--task",
        type=Path,
        help="Optional path to the task profile TOML; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--transport",
        type=Path,
        help="Optional path to the transport profile TOML; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--profiles-defaults",
        type=Path,
        default=_default_profile_defaults_path(),
        help="Manifest that defines the default device/model/task/transport profile paths",
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=_project_root() / "artifacts",
        help="Root directory for generated sessions",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=_default_settings_path(),
        help="Path to the shared user-settings TOML; missing files are ignored",
    )
    parser.add_argument("--serial-port", help="Override the serial port from the device profile")
    parser.add_argument("--baudrate", type=int, help="Override the serial baudrate from the device profile")


def _add_loop_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--goal-id", help="Stable identifier for the upper-level debug objective")
    parser.add_argument("--goal", help="Plain-text target for the current debug loop")
    parser.add_argument("--prev-session", type=Path, help="Previous session directory in the same multi-round debug loop")
    parser.add_argument("--iteration", type=int, help="Current debug iteration index; defaults to previous iteration + 1")
    parser.add_argument("--max-iterations", type=int, help="Optional loop budget recorded in the session summary")
    parser.add_argument("--attempt-note", help="Short note about what changed before this iteration")


def _add_watch_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        type=Path,
        help="Optional path to the device profile TOML used for watch hotkeys; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Optional path to the model profile TOML used for watch hotkeys; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--task",
        type=Path,
        help="Optional path to the task profile TOML used for watch hotkeys; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--transport",
        type=Path,
        help="Optional path to the transport profile TOML used for watch hotkeys; falls back to profiles/defaults.toml",
    )
    parser.add_argument(
        "--profiles-defaults",
        type=Path,
        default=_default_profile_defaults_path(),
        help="Manifest that defines the default device/model/task/transport profile paths",
    )


def _add_validation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--validation-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Device shell command that must exit 0 for target-specific validation (repeatable)",
    )
    parser.add_argument(
        "--expect-marker",
        action="append",
        default=[],
        metavar="TEXT",
        help="Serial output text that must appear in the observation window (repeatable)",
    )
    parser.add_argument(
        "--reject-marker",
        action="append",
        default=[],
        metavar="TEXT",
        help="Serial output text that must not appear in the observation window (repeatable)",
    )
    parser.add_argument("--expected-version", help="Version text that must appear in appver or validation output")


def _add_intervention_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--git-commit", help="Git commit or revision that produced the candidate build")
    parser.add_argument(
        "--changed-file",
        action="append",
        default=[],
        metavar="PATH",
        help="Source file changed in this intervention (repeatable)",
    )
    parser.add_argument("--expected-effect", help="Expected behavior change being verified")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodbg", description="Embedded device auto-debug scaffold")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Create a session and render the MVP workflow plan")
    _add_profile_arguments(run_parser)
    _add_loop_arguments(run_parser)
    run_parser.add_argument("--observe-seconds", type=float, default=3.0, help="Initial serial observation window")
    run_parser.add_argument(
        "--skip-evidence",
        action="store_true",
        help="Skip the default evidence bundle so run stops after baseline startup checks",
    )
    run_parser.add_argument(
        "--evidence-timeout",
        type=float,
        default=_RUN_DEFAULT_EVIDENCE_TIMEOUT,
        help="Timeout in seconds for each default evidence command during run",
    )
    _add_validation_arguments(run_parser)
    _add_intervention_arguments(run_parser)

    stage_sd_parser = subparsers.add_parser("stage-sd", help="Copy a local file into the configured SD card drive")
    _add_profile_arguments(stage_sd_parser)
    _add_loop_arguments(stage_sd_parser)
    stage_sd_parser.add_argument("--source", type=Path, required=True, help="Local file to stage onto the SD card")
    stage_sd_parser.add_argument(
        "--target-subdir",
        default=r"debug\autodbg",
        help="Relative subdirectory under the SD card drive used for staged files",
    )
    stage_sd_parser.add_argument("--dest-name", help="Optional destination filename on the SD card")
    stage_sd_parser.add_argument("--no-verify", action="store_true", help="Skip SHA256 verification after copy")
    stage_sd_parser.add_argument(
        "--allow-non-removable",
        action="store_true",
        help="Allow staging onto a drive that is not detected as removable",
    )

    storage_parser = subparsers.add_parser("storage", help="Inspect host drive letters and removable-drive candidates")
    storage_parser.add_argument("--device", type=Path, help="Optional device profile path used for configured-drive validation")
    storage_parser.add_argument(
        "--settings",
        type=Path,
        default=_default_settings_path(),
        help="Path to the shared user-settings TOML; missing files are ignored",
    )

    bootstrap_parser = subparsers.add_parser("bootstrap-network", help="Bring up device networking over serial shell")
    _add_profile_arguments(bootstrap_parser)
    _add_loop_arguments(bootstrap_parser)
    bootstrap_parser.add_argument(
        "--mode",
        choices=["lan_ready", "wlan_script", "offline"],
        default="lan_ready",
        help="How the device is expected to get online for this task",
    )
    bootstrap_parser.add_argument(
        "--bootstrap-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Shell command used to bootstrap networking (repeatable)",
    )
    bootstrap_parser.add_argument(
        "--check-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Shell command used to verify networking (repeatable)",
    )
    bootstrap_parser.add_argument("--wifi-ssid", help="WiFi SSID used when auto-building wlan_script bootstrap commands")
    bootstrap_parser.add_argument(
        "--wifi-password",
        help="WiFi password used when auto-building wlan_script bootstrap commands",
    )
    bootstrap_parser.add_argument(
        "--wifi-mode",
        help="WiFi auth mode such as WPA2, used when auto-building wlan_script bootstrap commands",
    )
    bootstrap_parser.add_argument(
        "--network-dir",
        help="Device-side directory that contains wlan_run.sh and wifi_cmd.sh",
    )
    bootstrap_parser.add_argument("--timeout", type=float, default=20.0, help="Timeout in seconds for each shell command")

    serve_parser = subparsers.add_parser("serve-artifacts", help="Serve a local artifact directory over HTTP for device pull")
    serve_parser.add_argument(
        "--root",
        type=Path,
        default=_default_payload_root(),
        help="Directory to expose over HTTP; defaults to the local payload root",
    )
    serve_parser.add_argument("--bind", default="0.0.0.0", help="Bind address for the local HTTP server")
    serve_parser.add_argument("--port", type=int, default=8765, help="TCP port for the local HTTP server")
    serve_parser.add_argument(
        "--settings",
        type=Path,
        default=_default_settings_path(),
        help="Path to the shared user-settings TOML; missing files are ignored",
    )
    serve_parser.add_argument("--base-url", help="Public base URL used when generating the manifest")
    serve_parser.add_argument(
        "--manifest-name",
        default="autodbg-manifest.json",
        help="Filename used for the generated artifact manifest",
    )
    serve_parser.add_argument(
        "--duration-seconds",
        type=float,
        help="Optional auto-stop duration for the local HTTP server",
    )
    serve_parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run the HTTP server in the foreground; default starts a background server and returns.",
    )
    serve_parser.add_argument(
        "--startup-timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for the background server health endpoint.",
    )
    serve_parser.add_argument(
        "--health-name",
        default=DEFAULT_HEALTH_NAME,
        help="Filename used for the local artifact server health endpoint.",
    )
    serve_parser.add_argument(
        "--no-auto-port",
        dest="auto_port",
        action="store_false",
        default=True,
        help="Fail instead of selecting the next free port when --port is occupied.",
    )
    serve_parser.add_argument(
        "--workspace",
        default="/mnt/sdcard/autodbg",
        help="Default device-side workspace embedded into the generated pull script",
    )
    serve_parser.add_argument(
        "--pull-script-name",
        default="autodbg-pull.sh",
        help="Filename used for the generated device pull script",
    )

    artifact_server_parser = subparsers.add_parser("artifact-server", help="Manage background artifact servers")
    artifact_server_subparsers = artifact_server_parser.add_subparsers(dest="artifact_server_command", required=True)
    artifact_server_list_parser = artifact_server_subparsers.add_parser("list", help="List registered artifact servers")
    artifact_server_list_parser.add_argument("--port", type=int, help="Filter by TCP port")
    artifact_server_stop_parser = artifact_server_subparsers.add_parser("stop", help="Stop a background artifact server")
    artifact_server_stop_parser.add_argument("--port", type=int, required=True, help="TCP port of the server to stop")

    quickstart_parser = subparsers.add_parser(
        "quickstart",
        help="Guide a first-time user to the next safe MCP action",
    )
    quickstart_parser.add_argument("--goal", help="User goal in plain language, such as watch serial, health check, deploy, or SD helper")
    quickstart_parser.add_argument("--serial-port", help="Known device serial port, for example COM19")
    quickstart_parser.add_argument("--baudrate", type=int, help="Known serial baudrate")
    quickstart_parser.add_argument(
        "--device-password-known",
        action="store_true",
        help="Mark that a device shell password is available without printing it",
    )
    quickstart_parser.add_argument("--sdcard-drive", help="Known host SD card drive, for example E:")
    quickstart_parser.add_argument("--helper-cc", help="Known target cross compiler for build-sd-http-helper")
    quickstart_parser.add_argument("--artifact", type=Path, help="Known local artifact path for deploy or transfer guidance")

    device_pull_parser = subparsers.add_parser(
        "device-pull",
        help="Serve local artifacts and trigger a device-side pull over the serial shell",
    )
    _add_profile_arguments(device_pull_parser)
    _add_loop_arguments(device_pull_parser)
    device_pull_parser.add_argument(
        "--root",
        type=Path,
        default=_default_payload_root(),
        help="Directory to expose over HTTP; defaults to the local payload root",
    )
    device_pull_parser.add_argument(
        "--mode",
        choices=["lan_ready", "wlan_script", "offline"],
        default="lan_ready",
        help="How the device is expected to get online for this task",
    )
    device_pull_parser.add_argument(
        "--bootstrap-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Shell command used to bootstrap networking before the pull (repeatable)",
    )
    device_pull_parser.add_argument(
        "--check-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Shell command used to verify networking before the pull (repeatable)",
    )
    device_pull_parser.add_argument("--wifi-ssid", help="WiFi SSID used when auto-building wlan_script bootstrap commands")
    device_pull_parser.add_argument(
        "--wifi-password",
        help="WiFi password used when auto-building wlan_script bootstrap commands",
    )
    device_pull_parser.add_argument(
        "--wifi-mode",
        help="WiFi auth mode such as WPA2, used when auto-building wlan_script bootstrap commands",
    )
    device_pull_parser.add_argument(
        "--network-dir",
        help="Device-side directory that contains wlan_run.sh and wifi_cmd.sh",
    )
    device_pull_parser.add_argument("--bind", default="0.0.0.0", help="Bind address for the local HTTP server")
    device_pull_parser.add_argument("--port", type=int, default=8765, help="TCP port for the local HTTP server")
    device_pull_parser.add_argument("--base-url", help="Public base URL used by the device to fetch artifacts")
    device_pull_parser.add_argument(
        "--workspace",
        help="Device-side workspace where the pull script should place files",
    )
    device_pull_parser.add_argument(
        "--manifest-name",
        default="autodbg-manifest.json",
        help="Filename used for the generated artifact manifest",
    )
    device_pull_parser.add_argument(
        "--pull-script-name",
        default="autodbg-pull.sh",
        help="Filename used for the generated device pull script",
    )
    device_pull_parser.add_argument(
        "--transfer-mode",
        choices=["auto", "http", "sd_http_helper", "serial_bundle"],
        default="auto",
        help="Prefer HTTP pull, SD-card HTTP helper, force serial bundle transfer, or auto-select based on device capabilities",
    )
    device_pull_parser.add_argument(
        "--sd-http-helper-path",
        default="/mnt/sdcard/autodbg/autodbg-http-pull",
        help="Device-side path to the SD-card HTTP helper executable used by --transfer-mode sd_http_helper",
    )
    device_pull_parser.add_argument(
        "--sd-http-list-name",
        default="autodbg-files.txt",
        help="Newline-delimited transfer list served to the SD-card HTTP helper",
    )
    device_pull_parser.add_argument(
        "--serial-bundle-chunk-size",
        type=int,
        default=768,
        help="Chunk size used when sending a base64-encoded tar bundle over the serial shell",
    )
    device_pull_parser.add_argument(
        "--list-command",
        help="Optional follow-up command that lists the pulled files for verification",
    )
    device_pull_parser.add_argument(
        "--post-pull-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Device shell command to run after the pull, such as upgrade/apply commands (repeatable)",
    )
    device_pull_parser.add_argument("--reboot-command", help="Device shell command that reboots or restarts the updated program")
    device_pull_parser.add_argument(
        "--post-observe-seconds",
        type=float,
        default=0.0,
        help="Serial observation window after post-pull/reboot commands",
    )
    _add_validation_arguments(device_pull_parser)
    _add_intervention_arguments(device_pull_parser)
    device_pull_parser.add_argument("--timeout", type=float, default=30.0, help="Timeout in seconds for each shell command")

    helper_build_parser = subparsers.add_parser(
        "build-sd-http-helper",
        help="Cross-compile the bundled SD-card HTTP helper for the target device",
    )
    helper_build_parser.add_argument("--cc", required=True, help="Target C compiler, for example arm-linux-gnueabihf-gcc")
    helper_build_parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="C source file for the helper; defaults to the packaged autodbg_http_pull.c",
    )
    helper_build_parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/autodbg-http-pull"),
        help="Output executable path",
    )
    helper_build_parser.add_argument(
        "--cflag",
        action="append",
        default=[],
        metavar="FLAG",
        help="Additional compiler flag; repeatable",
    )
    helper_build_parser.add_argument(
        "--static",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pass -static to the compiler by default; use --no-static when the target toolchain cannot link statically",
    )
    helper_build_parser.add_argument("--timeout", type=float, default=120.0, help="Compiler timeout in seconds")

    deploy_verify_parser = subparsers.add_parser(
        "deploy-verify",
        help="Run a closed debug loop: optional build, deploy/apply commands, observation, and target validation",
    )
    _add_profile_arguments(deploy_verify_parser)
    _add_loop_arguments(deploy_verify_parser)
    deploy_verify_parser.add_argument("--build-command", help="Host-side build command to run before deploying")
    deploy_verify_parser.add_argument("--artifact", type=Path, help="Local artifact produced by the build or selected for deployment")
    deploy_verify_parser.add_argument(
        "--post-pull-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Device shell deploy/apply command to run before verification (repeatable)",
    )
    deploy_verify_parser.add_argument("--reboot-command", help="Device shell command that reboots or restarts the updated program")
    deploy_verify_parser.add_argument(
        "--observe-seconds",
        type=float,
        default=10.0,
        help="Serial observation window after deployment/restart",
    )
    deploy_verify_parser.add_argument("--timeout", type=float, default=30.0, help="Timeout in seconds for each shell command")
    _add_validation_arguments(deploy_verify_parser)
    _add_intervention_arguments(deploy_verify_parser)

    observe_parser = subparsers.add_parser("observe", help="Capture serial output into a new session")
    _add_profile_arguments(observe_parser)
    _add_loop_arguments(observe_parser)
    observe_parser.add_argument("--seconds", type=float, default=10.0, help="How long to observe the serial port")
    observe_parser.add_argument("--live", action="store_true", help="Print serial lines in real time while capturing")
    observe_parser.add_argument("--follow", action="store_true", help="Keep watching until Ctrl+C")
    observe_parser.add_argument(
        "--markers-only",
        action="store_true",
        help="When used with --live, only print lines that hit known markers",
    )
    observe_parser.add_argument(
        "--focus",
        action="append",
        default=[],
        metavar="TEXT",
        help="When used with --live, also print non-marker lines that contain TEXT (repeatable)",
    )
    observe_parser.add_argument(
        "--poke-newline",
        action="store_true",
        help="Send one newline after opening the serial port to wake the current prompt",
    )

    exec_parser = subparsers.add_parser("exec", help="Login over serial and execute a shell command")
    _add_profile_arguments(exec_parser)
    _add_loop_arguments(exec_parser)
    exec_parser.add_argument("--shell-command", required=True, help="Command to execute once a shell prompt is reached")
    exec_parser.add_argument("--timeout", type=float, default=20.0, help="Command timeout in seconds")

    fetch_parser = subparsers.add_parser("fetch-file", help="Fetch a device-side file over the serial shell using base64")
    _add_profile_arguments(fetch_parser)
    _add_loop_arguments(fetch_parser)
    fetch_parser.add_argument("--remote-path", required=True, help="Absolute device-side file path to fetch")
    fetch_parser.add_argument("--output", type=Path, help="Optional local output path")
    fetch_parser.add_argument("--timeout", type=float, default=60.0, help="Fetch timeout in seconds")

    fetch_path_parser = subparsers.add_parser(
        "fetch-path",
        help="Fetch a device-side file or directory; directories are returned as tar streams",
    )
    _add_profile_arguments(fetch_path_parser)
    _add_loop_arguments(fetch_path_parser)
    fetch_path_parser.add_argument("--remote-path", required=True, help="Absolute device-side file or directory path to fetch")
    fetch_path_parser.add_argument("--output", type=Path, help="Optional local output path")
    fetch_path_parser.add_argument("--timeout", type=float, default=90.0, help="Fetch timeout in seconds")

    collect_parser = subparsers.add_parser(
        "collect-evidence",
        help="Run a default evidence command bundle and fetch selected small device files",
    )
    _add_profile_arguments(collect_parser)
    _add_loop_arguments(collect_parser)
    collect_parser.add_argument(
        "--shell-command",
        action="append",
        default=[],
        metavar="CMD",
        help="Additional shell command to capture as evidence (repeatable)",
    )
    collect_parser.add_argument(
        "--remote-file",
        action="append",
        default=[],
        metavar="PATH",
        help="Additional small device-side file to fetch with base64 (repeatable)",
    )
    collect_parser.add_argument(
        "--skip-defaults",
        action="store_true",
        help="Only run explicitly provided shell/file evidence items",
    )
    collect_parser.add_argument("--timeout", type=float, default=30.0, help="Timeout in seconds for each evidence action")

    health_parser = subparsers.add_parser("health", help="Run the serial health probe bundle against the device")
    _add_profile_arguments(health_parser)
    _add_loop_arguments(health_parser)
    health_parser.add_argument("--timeout", type=float, default=20.0, help="Timeout in seconds for each health probe")
    health_parser.add_argument(
        "--skip-sd-write-probe",
        action="store_true",
        help="Skip the temporary write/read/delete probe on /mnt/sdcard",
    )

    intervention_parser = subparsers.add_parser(
        "record-intervention",
        help="Append a structured intervention record to an existing session",
    )
    intervention_parser.add_argument("--session-dir", type=Path, required=True, help="Target session directory")
    intervention_parser.add_argument("--kind", required=True, help="Intervention type such as ai_patch, human_action, or config_change")
    intervention_parser.add_argument("--summary", required=True, help="One-line summary of the intervention")
    intervention_parser.add_argument("--details", default="", help="Optional detailed description")
    intervention_parser.add_argument("--file", action="append", default=[], help="Affected file path; may be repeated")
    intervention_parser.add_argument("--git-commit", help="Optional git commit hash associated with the intervention")
    intervention_parser.add_argument("--expected-effect", help="Expected effect before the next debug iteration")
    intervention_parser.add_argument("--related-session", help="Optional related session id or path")
    intervention_parser.add_argument("--metadata-json", help="Optional JSON object with extra machine-readable metadata")

    resume_parser = subparsers.add_parser("resume", help="Show the stored session summary")
    resume_parser.add_argument("--session-dir", type=Path, required=True, help="Path to the session directory")

    summary_parser = subparsers.add_parser("summary", help="Print summary.json for a session")
    summary_parser.add_argument("--session-dir", type=Path, required=True, help="Path to the session directory")

    report_parser = subparsers.add_parser("report", help="Print report.md for a session")
    report_target = report_parser.add_mutually_exclusive_group(required=True)
    report_target.add_argument("--session-dir", type=Path, help="Path to the session directory")
    report_target.add_argument("--latest", action="store_true", help="Use the latest session under artifacts root")
    report_parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=_project_root() / "artifacts",
        help="Root directory for generated sessions",
    )

    watch_parser = subparsers.add_parser(
        "watch-serial",
        help="Tail the shared serial TX/RX trace without opening the COM port",
    )
    _add_watch_profile_arguments(watch_parser)
    watch_parser.add_argument(
        "--serial-port",
        help="Serial port name, for example COM19; defaults to AUTO_DBG_SERIAL_PORT or config/user-settings.toml",
    )
    watch_parser.add_argument("--tail", type=int, default=20, help="How many existing trace lines to show first")
    watch_parser.add_argument("--follow", action="store_true", help="Keep following the shared serial trace until Ctrl+C")
    watch_parser.add_argument("--show-system", action="store_true", help="Include OPEN/CLOSE system trace lines")
    watch_parser.add_argument(
        "--raw-live",
        action="store_true",
        help="Start or attach to a local serial broker so the trace contains the full raw serial stream",
    )
    watch_parser.add_argument(
        "--baudrate",
        type=int,
        help="Baudrate used when starting the raw serial broker; defaults to settings or 115200",
    )
    watch_parser.add_argument(
        "--stdin-probe",
        action="store_true",
        help="When following a live broker, pressing Enter in this window sends a newline probe and reports the result",
    )
    watch_parser.add_argument(
        "--stdin-shell",
        action="store_true",
        help="When following a live broker, type shell commands in this window and press Enter to send them over serial",
    )
    watch_parser.add_argument(
        "--settings",
        type=Path,
        default=_default_settings_path(),
        help="Path to the shared user-settings TOML; missing files are ignored",
    )

    broker_parser = subparsers.add_parser("serial-broker", help="List or stop local raw serial brokers")
    broker_subparsers = broker_parser.add_subparsers(dest="broker_command", required=True)
    broker_list_parser = broker_subparsers.add_parser("list", help="List active raw serial brokers")
    broker_list_parser.add_argument("--serial-port", help="Optional serial port filter, for example COM19")
    broker_stop_parser = broker_subparsers.add_parser(
        "stop",
        help="Stop one or more raw serial brokers and release the physical COM port",
    )
    broker_stop_target = broker_stop_parser.add_mutually_exclusive_group(required=True)
    broker_stop_target.add_argument("--serial-port", help="Serial port name, for example COM19")
    broker_stop_target.add_argument("--all", action="store_true", help="Stop every active raw serial broker")

    agent_parser = subparsers.add_parser(
        "agent-call",
        help="Execute a structured JSON request for another AI agent and return structured JSON",
    )
    agent_parser.add_argument(
        "--request",
        default="-",
        help="Path to the agent request JSON file, or - to read the JSON request from stdin",
    )
    agent_parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON response")

    describe_agent_parser = subparsers.add_parser(
        "describe-agent-tool",
        help="Print an AI-oriented self-description of the embedded auto-debug tool",
    )
    describe_agent_parser.add_argument(
        "--format",
        choices=["json", "markdown"],
        default="json",
        help="Output format for the self-description",
    )
    describe_agent_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")

    install_home_plugin_parser = subparsers.add_parser(
        "install-home-plugin",
        help="Install or upgrade the home-local MCP plugin under the current user's home directory",
    )
    install_home_plugin_parser.add_argument(
        "--project-root",
        type=Path,
        default=_project_root(),
        help="Path to the independent auto-debug project root; defaults to the current project",
    )
    install_home_plugin_parser.add_argument(
        "--home-root",
        type=Path,
        default=Path.home(),
        help="Target home directory for plugin installation; defaults to the current user home",
    )
    install_home_plugin_parser.add_argument(
        "--plugin-name",
        default=HOME_PLUGIN_NAME,
        help=f"Plugin folder name; defaults to {HOME_PLUGIN_NAME}",
    )

    install_local_tool_parser = subparsers.add_parser(
        "install-local-tool",
        help="Deploy the tool into a local install root and generate direct-call wrappers",
    )
    install_local_tool_parser.add_argument(
        "--project-root",
        type=Path,
        default=_project_root(),
        help="Path to the source project root; defaults to the current project",
    )
    install_local_tool_parser.add_argument(
        "--install-root",
        type=Path,
        default=Path.home() / "AppData" / "Local" / "Programs" / LOCAL_TOOL_NAME,
        help="Target install root for the local tool deployment",
    )
    install_local_tool_parser.add_argument(
        "--skip-venv",
        action="store_true",
        help="Copy files without copying a source .venv; install-local-tool.ps1 uses this internally before creating a fresh runtime",
    )
    install_local_tool_parser.add_argument(
        "--skip-local-settings",
        action="store_true",
        help="Do not copy config/user-settings.toml even if it exists locally",
    )

    subparsers.add_parser("show-mvp", help="Print the MVP workflow document path and command entry")
    subparsers.add_parser("ports", help="List serial ports using pyserial")
    return parser


def _print_run_report(summary: dict[str, Any]) -> None:
    session = summary["session"]
    profiles = summary["profiles"]
    evaluation = summary.get("evaluation", {})
    evidence = summary.get("evidence_results", {})
    print("[ ooo.. ] 3/5 steps")
    print(f"[DONE] Session created: {session['session_id']}")
    print(f"[DONE] Device: {profiles['device']['device_id']} ({profiles['model']['model_id']})")
    print(f"[ACTIVE] Workflow: {summary['workflow']}")
    if evaluation:
        print(f"[DONE] Startup verdict: {evaluation.get('verdict', 'unknown')}")
        print(f"[ACTIVE] {evaluation.get('summary', '')}")
        findings = evaluation.get("findings", [])
        if findings:
            print("[TODO] Findings:")
            for finding in findings:
                print(f"  - {finding['level'].upper()} {finding['check_name']}: {finding['message']}")
    if evidence:
        command_results = evidence.get("command_results", [])
        file_results = evidence.get("file_results", [])
        command_failures = [item for item in command_results if item.get("exit_code") != 0]
        file_failures = [item for item in file_results if item.get("status") != "ok"]
        print(f"[DONE] Evidence commands: {len(command_results)}")
        if file_results:
            ok_files = sum(1 for item in file_results if item.get("status") == "ok")
            print(f"[DONE] Evidence files: {ok_files}/{len(file_results)}")
        if command_failures:
            print(f"[ERROR] Evidence command failures: {len(command_failures)}")
        if file_failures:
            print(f"[ERROR] Evidence file failures: {len(file_failures)}")
    manual_items = profiles.get("task", {}).get("manual_check_items", [])
    if manual_items:
        print("[TODO] Manual checks:")
        for item in manual_items:
            print(f"  - {item}")
    print("[TODO] Session directory:")
    print(f"  {session['session_paths']['root']}")


def _build_health_checks(*, include_sd_write_probe: bool) -> list[tuple[str, str]]:
    checks = [
        ("appver", "cat /opt/appver.txt"),
        ("lecam_process", "ps | grep LeCam"),
        ("mmc_devices", "ls /dev | grep mmc"),
        ("mmc_partitions", "cat /proc/partitions | grep mmc"),
        ("mmc_mount", "mount | grep mmc"),
        ("sdcard_listing", "ls /mnt/sdcard"),
        ("sdcard_capacity", "df -h /mnt/sdcard"),
    ]
    if include_sd_write_probe:
        checks.append(
            (
                "sdcard_write_probe",
                "echo autodbg_probe > /mnt/sdcard/autodbg_probe.txt && "
                "cat /mnt/sdcard/autodbg_probe.txt && "
                "rm -f /mnt/sdcard/autodbg_probe.txt && "
                "echo PROBE_OK",
            )
        )
    checks.append(("mmc_dmesg_tail", "dmesg | grep mmc | tail -n 20"))
    return checks


def _build_collect_evidence_commands(profiles) -> list[tuple[str, str]]:
    commands = [
        ("uname", "uname -a"),
        ("mounts", "mount"),
        ("disk_usage", "df -h"),
        ("ifconfig", "ifconfig -a"),
        (
            "app_process",
            f"ps | grep -F {_sh_single_quote(profiles.model.app_name)} | grep -v grep || "
            f"echo 'AUTODBG_PROCESS_MISSING {profiles.model.app_name}'",
        ),
        ("mmc_dmesg_tail", "dmesg | grep mmc | tail -n 50 || true"),
    ]
    for index, artifact_path in enumerate(profiles.model.artifact_paths, start=1):
        commands.append(
            (
                f"artifact_path_{index}",
                f"if [ -e {_sh_single_quote(artifact_path)} ]; then "
                f"ls -al {_sh_single_quote(artifact_path)}; "
                f"else echo 'AUTODBG_MISSING_PATH {artifact_path}'; fi",
            )
        )
    return commands


def _default_collect_evidence_files(profiles) -> list[str]:
    candidates = ["/etc/wlanname"]
    return list(dict.fromkeys(candidates))


def _execute_named_command(
    *,
    name: str,
    shell_command: str,
    controller: DeviceController,
    collector: EvidenceCollector,
    event_type: str,
    event_summary: str,
    artifact_prefix: str,
    timeout: float = 20.0,
    serial_port=None,
) -> dict[str, Any]:
    result = _execute_structured_command(
        controller=controller,
        shell_command=shell_command,
        timeout=timeout,
        serial_port=serial_port,
    )
    transcript = "\n".join(result.transcript) + ("\n" if result.transcript else "")
    output = "\n".join(result.output_lines) + ("\n" if result.output_lines else "")
    collector.write_text_artifact(f"logs/{artifact_prefix}-transcript.log", transcript)
    collector.write_text_artifact(f"logs/{artifact_prefix}-output.log", output)
    payload = {
        "name": name,
        "command": shell_command,
        "exit_code": result.exit_code,
        "output_lines": result.output_lines,
    }
    collector.append_event(
        event_type=event_type,
        source="workflow_runner",
        summary=event_summary,
        payload=payload,
    )
    return payload


def _run_baseline_check(
    *,
    name: str,
    shell_command: str,
    controller: DeviceController,
    collector: EvidenceCollector,
    timeout: float = 20.0,
    serial_port=None,
) -> dict[str, Any]:
    return _execute_named_command(
        name=name,
        shell_command=shell_command,
        controller=controller,
        collector=collector,
        event_type="baseline_check",
        event_summary=f"Baseline check completed: {name}",
        artifact_prefix=f"check-{name}",
        timeout=timeout,
        serial_port=serial_port,
    )


def _collect_evidence_bundle(
    *,
    profiles,
    controller: DeviceController,
    collector: EvidenceCollector,
    timeout: float,
    serial_port=None,
    command_items: list[tuple[str, str]] | None = None,
    remote_files: list[str] | None = None,
    command_prefix: str = "collect-evidence",
    file_prefix: str = "collect-evidence-file",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    command_plan = list(command_items) if command_items is not None else _build_collect_evidence_commands(profiles)
    file_plan = list(dict.fromkeys(remote_files)) if remote_files is not None else _default_collect_evidence_files(profiles)

    command_results: list[dict[str, Any]] = []
    file_results: list[dict[str, Any]] = []

    for name, shell_command in command_plan:
        command_results.append(
            _execute_named_command(
                name=name,
                shell_command=shell_command,
                controller=controller,
                collector=collector,
                event_type="evidence_command",
                event_summary=f"Evidence command completed: {name}",
                artifact_prefix=f"{command_prefix}-{name}",
                timeout=timeout,
                serial_port=serial_port,
            )
        )

    for index, remote_path in enumerate(file_plan, start=1):
        fetch_result = _fetch_remote_file_artifact(
            controller=controller,
            collector=collector,
            remote_path=remote_path,
            timeout=timeout,
            prefix=f"{file_prefix}-{index}",
            serial_port=serial_port,
        )
        file_results.append(fetch_result)
        collector.append_event(
            event_type="collect_evidence_file",
            source="device_controller",
            summary=f"Fetched evidence file: {remote_path}",
            payload=fetch_result,
            severity="warning" if fetch_result["status"] != "ok" else "info",
        )

    return command_results, file_results


def _format_output_excerpt(output_lines: list[str], *, max_length: int = 88) -> str:
    if not output_lines:
        return "(no output)"
    first_line = output_lines[0].strip()
    if len(first_line) > max_length:
        first_line = first_line[: max_length - 3] + "..."
    if len(output_lines) > 1:
        return f"{first_line} (+{len(output_lines) - 1} lines)"
    return first_line


def _hash_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _has_validation_spec(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "validation_command", None)
        or getattr(args, "expect_marker", None)
        or getattr(args, "reject_marker", None)
        or getattr(args, "expected_version", None)
    )


def _observation_text(observation: dict[str, Any] | None) -> str:
    if not isinstance(observation, dict):
        return ""
    parts: list[str] = []
    for key in ("last_lines", "marker_hits", "marker_windows"):
        value = observation.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    parts.extend(str(field) for field in item.values() if field is not None)
                else:
                    parts.append(str(item))
    return "\n".join(parts)


def _run_validation_commands(
    *,
    controller: DeviceController,
    collector: EvidenceCollector,
    commands: list[str],
    timeout: float,
    serial_port=None,
    prefix: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, command in enumerate(commands, start=1):
        result = _execute_structured_command(
            controller=controller,
            shell_command=command,
            timeout=timeout,
            serial_port=serial_port,
        )
        _write_command_artifacts(collector=collector, prefix=f"{prefix}-{index}", result=result)
        result_dict = result.to_dict()
        results.append(result_dict)
        collector.append_event(
            event_type="target_validation_command",
            source="device_controller",
            summary=f"Executed target validation command #{index}",
            payload=result_dict,
            severity="error" if result.exit_code != 0 else "info",
        )
    return results


def _evaluate_validation_spec(
    *,
    expect_markers: list[str] | None,
    reject_markers: list[str] | None,
    expected_version: str | None,
    observation: dict[str, Any] | None,
    command_results: list[dict[str, Any]] | None,
    app_version: str | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    observed_text = _observation_text(observation)
    command_text = "\n".join(
        "\n".join(str(line) for line in result.get("output_lines", []))
        for result in (command_results or [])
        if isinstance(result, dict)
    )
    search_text = "\n".join(value for value in [observed_text, command_text, app_version or ""] if value)

    for command_result in command_results or []:
        if command_result.get("exit_code") != 0:
            findings.append(
                {
                    "level": "error",
                    "check_name": "validation_command",
                    "message": f"Validation command failed: {command_result.get('command')}",
                }
            )
    for marker in expect_markers or []:
        if marker not in observed_text:
            findings.append(
                {
                    "level": "error",
                    "check_name": "expect_marker",
                    "message": f"Expected serial marker not observed: {marker}",
                }
            )
    for marker in reject_markers or []:
        if marker and marker in observed_text:
            findings.append(
                {
                    "level": "error",
                    "check_name": "reject_marker",
                    "message": f"Rejected serial marker observed: {marker}",
                }
            )
    if expected_version and expected_version not in search_text:
        findings.append(
            {
                "level": "error",
                "check_name": "expected_version",
                "message": f"Expected version was not confirmed: {expected_version}",
            }
        )

    return {
        "verdict": "fail" if findings else "pass",
        "summary": "Target-specific validation passed." if not findings else f"{len(findings)} target validation issue(s) found.",
        "checks": {
            "validation_commands": len(command_results or []),
            "expect_markers": list(expect_markers or []),
            "reject_markers": list(reject_markers or []),
            "expected_version": expected_version,
        },
        "findings": findings,
        "command_results": list(command_results or []),
    }


def _build_intervention_context(
    args: argparse.Namespace,
    *,
    artifact: Path | None = None,
    artifact_sha256: str | None = None,
    build_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if getattr(args, "git_commit", None):
        context["git_commit"] = args.git_commit
    changed_files = getattr(args, "changed_file", None) or []
    if changed_files:
        context["changed_files"] = [str(item) for item in changed_files]
    if getattr(args, "expected_effect", None):
        context["expected_effect"] = args.expected_effect
    if artifact is not None:
        context["artifact"] = str(artifact)
    if artifact_sha256:
        context["artifact_sha256"] = artifact_sha256
    if build_result is not None:
        context["build_result"] = build_result
    return context


def _validation_key_excerpts(validation_results: dict[str, Any]) -> list[dict[str, str]]:
    excerpts: list[dict[str, str]] = []
    for finding in validation_results.get("findings", [])[:3]:
        excerpts.append(
            build_excerpt(
                source="target_validation",
                label=str(finding.get("check_name", "validation")),
                text=str(finding.get("message", "")),
                severity=str(finding.get("level", "error")),
            )
        )
    return excerpts


def _build_marker_window_excerpts(observation: dict[str, Any], *, limit: int = 3) -> list[dict[str, str]]:
    windows = observation.get("marker_windows", [])
    if not isinstance(windows, list):
        return []
    ordered = sorted(
        [window for window in windows if isinstance(window, dict)],
        key=lambda window: (0 if window.get("kind") == "fatal" else 1, int(window.get("line_index", 0) or 0)),
    )
    excerpts: list[dict[str, str]] = []
    for window in ordered[:limit]:
        context_lines = [str(item) for item in window.get("before", [])]
        context_lines.append(str(window.get("line", "")))
        context_lines.extend(str(item) for item in window.get("after", []))
        kind = str(window.get("kind", "marker"))
        excerpts.append(
            build_excerpt(
                source="serial_marker",
                label=str(window.get("marker", kind)),
                text="\n".join(line for line in context_lines if line),
                severity="error" if kind == "fatal" else "info",
            )
        )
    return excerpts


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TiB"


def _write_command_artifacts(
    *,
    collector: EvidenceCollector,
    prefix: str,
    result,
) -> None:
    transcript = "\n".join(result.transcript) + ("\n" if result.transcript else "")
    output = "\n".join(result.output_lines) + ("\n" if result.output_lines else "")
    collector.write_text_artifact(f"logs/{prefix}-transcript.log", transcript)
    collector.write_text_artifact(f"logs/{prefix}-output.log", output)


from autodbg.cli.transport import (
    _artifact_health_url,
    _build_auto_wlan_bootstrap_commands,
    _build_existing_network_check,
    _build_fetch_file_command,
    _build_fetch_path_command,
    _build_network_dir_resolver,
    _build_remote_pull_command,
    _build_serial_bundle_commands,
    _build_sd_http_helper_command,
    _build_structured_command,
    _build_transfer_probe_command,
    _default_base_url,
    _execute_structured_command,
    _extract_structured_output_lines,
    _fetch_artifact_relative_path,
    _fetch_remote_file_artifact,
    _fetch_remote_path_artifact,
    _finalize_fetch_result,
    _host_from_base_url,
    _line_matches_focus_terms,
    _local_artifact_health_url,
    _normalize_focus_terms,
    _parse_fetch_output_lines,
    _parse_transfer_capabilities,
    _resolve_bootstrap_commands,
    _resolve_connectivity_checks,
    _resolve_host_ip,
    _resolve_preferred_interfaces,
    _resolve_pull_base_url,
    _resolve_pull_workspace,
    _resolve_wifi_settings,
    _select_transfer_mode,
    _sh_single_quote,
)


def _format_live_tag(hit: MarkerHit | None) -> str:
    if hit is None:
        return "SERIAL"
    tag_map = {
        "boot": "BOOT",
        "kernel": "KERNEL",
        "login": "LOGIN",
        "shell": "SHELL",
        "app_start": "APP_START",
        "app_ready": "APP_READY",
        "panic": "PANIC",
    }
    return tag_map.get(hit.tag, hit.tag.upper())


def _build_live_serial_printer(*, markers_only: bool, focus_terms: list[str]):
    normalized_focus_terms = _normalize_focus_terms(focus_terms)

    def _printer(line: str, hit: MarkerHit | None) -> None:
        if hit is None:
            if markers_only:
                return
            if not _line_matches_focus_terms(line, normalized_focus_terms):
                return
        stamp = time.strftime("%H:%M:%S")
        tag = _format_live_tag(hit)
        print(f"[{tag} {stamp}] {line}", flush=True)

    return _printer


def _format_trace_entry(entry: SerialTraceEntry) -> str:
    stamp = entry.timestamp.split("T")[-1]
    direction = entry.direction.upper()
    payload = entry.payload if entry.payload else "(empty line)"
    return f"[{direction} {stamp}] {payload}"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _session_manager(args: argparse.Namespace, profiles=None) -> SessionManager:
    retrieved_root: Path | None = None
    settings = _load_user_settings_from_args(args)
    if settings.storage.retrieved_root is not None:
        retrieved_root = settings.storage.retrieved_root
    if profiles is not None and profiles.device.storage is not None and profiles.device.storage.retrieved_root:
        retrieved_root = Path(profiles.device.storage.retrieved_root)
    return SessionManager(args.artifacts_root, retrieved_root=retrieved_root)


def _load_session_summary(session_dir: Path) -> dict[str, Any]:
    summary_path = Path(session_dir).absolute() / "summary.json"
    return json.loads(summary_path.read_text(encoding="utf-8"))


def _restore_session_context(session_dir: Path, summary: dict[str, Any]) -> SessionContext:
    session_data = summary.get("session")
    if not isinstance(session_data, dict):
        raise ValueError("Session summary is missing the session block.")

    paths_data = session_data.get("session_paths")
    if not isinstance(paths_data, dict):
        raise ValueError("Session summary is missing session.session_paths.")

    root = Path(paths_data.get("root") or session_dir).absolute()
    session_paths = SessionPaths(
        root=root,
        core_dir=Path(paths_data.get("core_dir") or root / "core").absolute(),
        deploy_dir=Path(paths_data.get("deploy_dir") or root / "deploy").absolute(),
        logs_dir=Path(paths_data.get("logs_dir") or root / "logs").absolute(),
        retrieved_dir=Path(paths_data.get("retrieved_dir") or root / "retrieved").absolute(),
    )
    return SessionContext(
        session_id=str(session_data.get("session_id") or root.name),
        created_at=str(session_data.get("created_at") or ""),
        device_id=str(session_data.get("device_id") or "unknown"),
        task_type=str(session_data.get("task_type") or "unknown"),
        session_paths=session_paths,
        metadata=dict(session_data.get("metadata") or {}),
    )


def _resolve_loop_context(args: argparse.Namespace, *, session_dir: Path, session_id: str) -> dict[str, Any]:
    parent_summary: dict[str, Any] | None = None
    parent_session_dir = getattr(args, "prev_session", None)
    if parent_session_dir is not None:
        parent_summary = _load_session_summary(parent_session_dir)

    parent_loop = parent_summary.get("loop", {}) if parent_summary else {}
    parent_session = parent_summary.get("session", {}) if parent_summary else {}
    parent_session_id = parent_session.get("session_id")
    iteration = getattr(args, "iteration", None)
    if iteration is None:
        if parent_loop.get("iteration") is not None:
            iteration = int(parent_loop["iteration"]) + 1
        else:
            iteration = 1

    max_iterations = getattr(args, "max_iterations", None)
    if max_iterations is None and parent_loop.get("max_iterations") is not None:
        max_iterations = int(parent_loop["max_iterations"])

    goal_id = getattr(args, "goal_id", None) or parent_loop.get("goal_id")
    goal = getattr(args, "goal", None) or parent_loop.get("goal")
    root_session_id = parent_loop.get("root_session_id") or parent_session_id or session_id

    return {
        "goal_id": goal_id,
        "goal": goal,
        "root_session_id": root_session_id,
        "parent_session_id": parent_session_id,
        "parent_session_dir": str(Path(parent_session_dir).absolute()) if parent_session_dir is not None else None,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "attempt_note": getattr(args, "attempt_note", None),
        "current_session_dir": str(Path(session_dir).absolute()),
    }


def _create_session_with_loop(args: argparse.Namespace, *, profiles, task_type: str):
    session = _session_manager(args, profiles).create(
        device_id=profiles.device.device_id,
        task_type=task_type,
    )
    loop_context = _resolve_loop_context(args, session_dir=session.session_paths.root, session_id=session.session_id)
    session.metadata["loop"] = loop_context
    return session, loop_context


def _collect_option_patch(args: argparse.Namespace, option_names: list[str]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    for option_name in option_names:
        if not hasattr(args, option_name):
            continue
        value = getattr(args, option_name)
        if value is None or value == "":
            continue
        if isinstance(value, Path):
            patch[option_name] = str(value)
        elif isinstance(value, list):
            if value:
                patch[option_name] = list(value)
        else:
            patch[option_name] = value
    return patch


def _dedupe_next_actions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("action", "")), str(item.get("reason", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _build_stage_sd_next_actions(*, failed: bool) -> list[dict[str, str]]:
    if failed:
        return [{"action": "stage-sd", "reason": "Retry SD staging after fixing the host drive path or media permissions."}]
    return []


def _build_run_next_actions(
    *,
    failure_stage: str | None,
    evaluation: dict[str, Any],
    has_evidence_failures: bool,
) -> list[dict[str, str]]:
    finding_names = {str(item.get("check_name", "")) for item in evaluation.get("findings", [])}
    actions: list[dict[str, str]] = []
    if failure_stage == "observe_serial" or "serial_observation" in finding_names:
        actions.append({"action": "observe", "reason": "Capture more serial context around the failing startup window."})
    if failure_stage == "establish_control":
        actions.append({"action": "run", "reason": "Retry after restoring serial login credentials or shell access."})
    if {"appver", "lecam_process"} & finding_names:
        actions.append({"action": "exec", "reason": "Inspect the application process or version directly on the device shell."})
    if any(name.startswith("mmc") or name.startswith("sdcard") for name in finding_names):
        actions.append({"action": "health", "reason": "Validate storage and mount health before the next startup attempt."})
    if has_evidence_failures or failure_stage == "collect_evidence":
        actions.append({"action": "collect-evidence", "reason": "Collect more diagnostics before the next startup iteration."})
    if failure_stage == "validation":
        actions.append({"action": "deploy-verify", "reason": "Re-run the build/deploy/observe/validate loop with the target validation criteria."})
    if not actions:
        actions.append({"action": "run", "reason": "Retry the startup workflow after the next code or configuration change."})
    return _dedupe_next_actions(actions)


def _build_health_next_actions(*, evaluation: dict[str, Any]) -> list[dict[str, str]]:
    finding_names = {str(item.get("check_name", "")) for item in evaluation.get("findings", [])}
    actions: list[dict[str, str]] = []
    if any(name.startswith("mmc") or name.startswith("sdcard") for name in finding_names):
        actions.append({"action": "collect-evidence", "reason": "Capture extra storage diagnostics for the warning or failure."})
    if "lecam_process" in finding_names or "appver" in finding_names:
        actions.append({"action": "run", "reason": "Re-run the startup workflow after addressing the reported app issue."})
    if not actions:
        actions.append({"action": "health", "reason": "Re-run health after the next intervention to verify the fix."})
    return _dedupe_next_actions(actions)


def _build_bootstrap_network_next_actions(
    *,
    blocked: bool = False,
    failed_checks: list[dict[str, Any]] | None = None,
    mode: str | None = None,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if blocked and mode == "offline":
        actions.append({"action": "stage-sd", "reason": "Use SD staging or another offline path when LAN bootstrap is intentionally unavailable."})
    if blocked:
        actions.append({"action": "bootstrap-network", "reason": "Retry after restoring device login access or changing the bootstrap mode."})
    if failed_checks:
        actions.append({"action": "bootstrap-network", "reason": "Retry after fixing the connectivity checks or WLAN bootstrap commands."})
        actions.append({"action": "observe", "reason": "Capture serial output while the device brings networking up."})
    if not actions:
        actions.append({"action": "bootstrap-network", "reason": "Retry network bootstrap after the next intervention."})
    return _dedupe_next_actions(actions)


def _build_device_pull_next_actions(
    *,
    blocked: bool = False,
    failed_checks: list[dict[str, Any]] | None = None,
    pull_failed: bool = False,
    list_failed: bool = False,
    mode: str | None = None,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if blocked and mode == "offline":
        actions.append({"action": "stage-sd", "reason": "Use SD staging when HTTP pull is unavailable in offline mode."})
    if failed_checks:
        actions.append({"action": "bootstrap-network", "reason": "Stabilize networking before retrying the device pull."})
    if pull_failed or list_failed or blocked:
        actions.append({"action": "device-pull", "reason": "Retry the artifact pull after fixing the transfer prerequisites."})
    if pull_failed:
        actions.append({"action": "serve-artifacts", "reason": "Validate the local artifact server or payload root before the next pull attempt."})
    if list_failed:
        actions.append({"action": "deploy-verify", "reason": "Run the closed deploy/observe/validate loop after transfer verification is repaired."})
    if not actions:
        actions.append({"action": "device-pull", "reason": "Retry device pull after the next intervention."})
    return _dedupe_next_actions(actions)


def _build_deploy_verify_next_actions(*, failure_stage: str | None) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if failure_stage in {"build", "prepare_artifact"}:
        actions.append({"action": "deploy-verify", "reason": "Retry after the host build or artifact path is fixed."})
    if failure_stage in {"manual_upgrade", "deploy"}:
        actions.append({"action": "device-pull", "reason": "Provide a concrete device-side upgrade command or transfer path, then retry deployment."})
    if failure_stage in {"observe_serial", "validation"}:
        actions.append({"action": "deploy-verify", "reason": "Retry the closed loop with the same artifact and validation criteria."})
    if not actions:
        actions.append({"action": "deploy-verify", "reason": "Retry the closed deploy/verify workflow after the next intervention."})
    return _dedupe_next_actions(actions)


from autodbg.cli.watch import (
    _WatchStdinShellState,
    _apply_watch_trace_entry_to_tui_state,
    _build_watch_tui_rows,
    _clear_watch_stdin_shell_prompt,
    _command_serial_broker,
    _command_watch_serial,
    _consume_watch_stdin_probe,
    _consume_watch_stdin_shell,
    _default_watch_status,
    _emit_watch_trace_entry,
    _fit_watch_tui_text,
    _follow_trace_via_broker,
    _page_watch_history,
    _print_trace_entries,
    _print_watch_trace_entry,
    _prompt_watch_password,
    _read_trace_entries,
    _refresh_watch_status_message,
    _resolve_watch_start_index,
    _restore_watch_password_env,
    _retry_watch_auto_login_with_password,
    _run_watch_auto_login,
    _send_watch_newline_probe,
    _send_watch_serial_text,
    _set_watch_stdin_shell_status,
    _set_watch_status_message,
    _watch_console_move_cursor,
    _watch_console_set_cursor_visible,
    _watch_console_viewport,
    _watch_console_write_text,
    _watch_probe_key_pressed,
    _watch_shell_login_action,
    _watch_shell_read_chars,
    _watch_terminal_columns,
    _watch_terminal_lines,
    _watch_tui_geometry,
    _watch_tui_log_rows,
)


def _print_health_report(summary: dict[str, Any]) -> None:
    checks: list[dict[str, Any]] = summary.get("health_checks", [])
    evaluation = summary.get("evaluation", {})
    passed = sum(1 for check in checks if check.get("exit_code") == 0)
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Health checks passed: {passed}/{len(checks)}")
    if evaluation:
        print(f"[DONE] Health verdict: {evaluation.get('verdict', 'unknown')}")
        print(f"[ACTIVE] {evaluation.get('summary', '')}")
    for check in checks:
        status = "DONE" if check.get("exit_code") == 0 else "ERROR"
        excerpt = _format_output_excerpt(check.get("output_lines", []))
        print(f"[{status}] {check['name']}: exit={check.get('exit_code')} | {excerpt}")
    findings = evaluation.get("findings", []) if evaluation else []
    if findings:
        print("[TODO] Findings:")
        for finding in findings:
            print(f"  - {finding['level'].upper()} {finding['check_name']}: {finding['message']}")
    print("[TODO] Session directory:")
    print(f"  {summary['session']['session_paths']['root']}")


def _command_run(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(args, profiles=profiles, task_type=profiles.task.task_type)

    state = StateSnapshot()
    state.transition_task(TaskState.PRECHECK, "Profiles loaded.")
    state.transition_device(DeviceState.OFFLINE, "Waiting for first serial sample.")

    observer_plan = SerialObserver(profiles.device.serial, profiles.model).plan()
    control_plan = DeviceController(profiles.device, profiles.model, profiles.transport).plan()
    deploy_plan = Deployer(profiles.task, profiles.transport).plan()
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()

    collector = EvidenceCollector(session)
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=workflow_name,
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="run",
        loop_context=loop_context,
        plan_details={
            "serial": {
                "port": observer_plan.port,
                "baudrate": observer_plan.baudrate,
                "markers": observer_plan.markers,
            },
            "control": {
                "preferred_channels": control_plan.preferred_channels,
                "login_prompt": control_plan.login_prompt,
                "shell_prompt": control_plan.shell_prompt,
                "app_name": control_plan.app_name,
            },
            "deploy": {
                "strategy": deploy_plan.strategy,
                "channels": deploy_plan.channels,
            },
        },
    )
    observer = SerialObserver(profiles.device.serial, profiles.model)
    controller = DeviceController(profiles.device, profiles.model, profiles.transport)

    run_results: dict[str, Any] = {}
    evidence_results = {"command_results": [], "file_results": []}
    validation_results: dict[str, Any] = _evaluate_validation_spec(
        expect_markers=[],
        reject_markers=[],
        expected_version=None,
        observation=None,
        command_results=[],
    )
    intervention_context = _build_intervention_context(args)
    state.transition_task(TaskState.WAITING_BOOT, f"Observing serial for {args.observe_seconds:.1f}s.")
    collector.update_summary({"state": state.to_dict()})

    try:
        observation = observer.capture(
            seconds=args.observe_seconds,
            log_path=session.session_paths.logs_dir / "serial.log",
        )
        collector.append_event(
            event_type="serial_observation",
            source="serial_observer",
            summary=f"Captured {observation.lines_captured} serial lines.",
            payload=observation.to_dict(),
        )
        run_results["observation"] = observation.to_dict()
        if observation.last_device_state:
            state.transition_device(DeviceState(observation.last_device_state), "Observed serial marker.")
    except Exception as exc:
        collector.append_event(
            event_type="serial_observation_failed",
            source="serial_observer",
            summary=f"Serial observation failed during run: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "run_observe_failed",
                "error": str(exc),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="run",
                    decision="continue",
                    failure_stage="observe_serial",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="serial_observer", label="observe_error", text=str(exc), severity="error")],
                    next_actions=_build_run_next_actions(
                        failure_stage="observe_serial",
                        evaluation={},
                        has_evidence_failures=False,
                    ),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        [
                            "observe_seconds",
                            "skip_evidence",
                            "evidence_timeout",
                            "validation_command",
                            "expect_marker",
                            "reject_marker",
                            "expected_version",
                            "git_commit",
                            "changed_file",
                            "expected_effect",
                        ],
                    ),
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Serial observe failed during run: {exc}")
        print("[TODO] Close any program that is already using this COM port, then retry.")
        return 1

    state.transition_task(TaskState.RUNNING_CHECKS, "Running baseline startup checks.")
    collector.update_summary({"state": state.to_dict()})

    baseline_checks = [
        ("appver", "cat /opt/appver.txt"),
        ("lecam_process", "ps | grep LeCam"),
        ("mmc_mount", "mount | grep mmc"),
        ("sdcard_listing", "ls /mnt/sdcard"),
    ]
    check_results: list[dict[str, Any]] = []
    evaluation: dict[str, Any] = {}
    run_stage = "baseline startup checks"
    try:
        with open_serial_port(
            profiles.device.serial.port,
            profiles.device.serial.baudrate,
            timeout=0.2,
        ) as serial_port:
            state.transition_device(DeviceState.ROOT_SHELL, "Startup run uses an interactive root shell.")
            collector.update_summary({"state": state.to_dict()})
            for name, shell_command in baseline_checks:
                check_results.append(
                    _run_baseline_check(
                        name=name,
                        shell_command=shell_command,
                        controller=controller,
                        collector=collector,
                        serial_port=serial_port,
                    )
                )
            run_results["baseline_checks"] = check_results
            evaluation = evaluate_startup_run(
                check_results,
                app_name=profiles.model.app_name,
                observation=run_results.get("observation"),
            )
            if evaluation.get("highlights", {}).get("app_process_seen"):
                state.transition_device(DeviceState.APP_READY, "Baseline checks confirmed the application process is running.")
            collector.update_summary(
                {
                    "run_results": run_results,
                    "evaluation": evaluation,
                    "state": state.to_dict(),
                }
            )
            if not args.skip_evidence:
                run_stage = "default evidence collection"
                state.transition_task(TaskState.COLLECTING_EVIDENCE, "Collecting the default evidence bundle.")
                collector.update_summary({"state": state.to_dict()})
                evidence_command_results, evidence_file_results = _collect_evidence_bundle(
                    profiles=profiles,
                    controller=controller,
                    collector=collector,
                    timeout=args.evidence_timeout,
                    serial_port=serial_port,
                    command_prefix="run-evidence",
                    file_prefix="run-evidence-file",
                )
                evidence_results = {
                    "command_results": evidence_command_results,
                    "file_results": evidence_file_results,
                }
            if _has_validation_spec(args):
                run_stage = "target validation"
                state.transition_task(TaskState.RUNNING_CHECKS, "Running target-specific validation.")
                validation_command_results = _run_validation_commands(
                    controller=controller,
                    collector=collector,
                    commands=list(args.validation_command),
                    timeout=args.evidence_timeout,
                    serial_port=serial_port,
                    prefix="run-validation",
                )
                validation_results = _evaluate_validation_spec(
                    expect_markers=list(args.expect_marker),
                    reject_markers=list(args.reject_marker),
                    expected_version=args.expected_version,
                    observation=run_results.get("observation"),
                    command_results=validation_command_results,
                    app_version=str(evaluation.get("highlights", {}).get("app_version") or ""),
                )
                collector.update_summary({"validation_results": validation_results})
    except LoginRequiredError as exc:
        collector.append_event(
            event_type="baseline_blocked",
            source="workflow_runner",
            summary=str(exc),
            severity="warning",
        )
        collector.update_summary(
            {
                "status": "run_blocked",
                "error": str(exc),
                "run_results": run_results,
                "baseline_checks": check_results,
                "evaluation": evaluation,
                "evidence_results": evidence_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="run",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    next_actions=_build_run_next_actions(
                        failure_stage="establish_control",
                        evaluation=evaluation,
                        has_evidence_failures=False,
                    ),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        [
                            "observe_seconds",
                            "skip_evidence",
                            "evidence_timeout",
                            "validation_command",
                            "expect_marker",
                            "reject_marker",
                            "expected_version",
                            "git_commit",
                            "changed_file",
                            "expected_effect",
                        ],
                    ),
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry the run command.")
        return 1
    except Exception as exc:
        collector.append_event(
            event_type="baseline_failed",
            source="workflow_runner",
            summary=f"Run failed during {run_stage}: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "run_failed",
                "error": str(exc),
                "run_results": run_results,
                "baseline_checks": check_results,
                "evaluation": evaluation,
                "evidence_results": evidence_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="run",
                    decision="continue",
                    failure_stage="collect_evidence" if run_stage == "default evidence collection" else "baseline_checks",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="workflow_runner", label=run_stage.replace(" ", "_"), text=str(exc), severity="error")],
                    next_actions=_build_run_next_actions(
                        failure_stage="collect_evidence" if run_stage == "default evidence collection" else "baseline_checks",
                        evaluation=evaluation,
                        has_evidence_failures=False,
                    ),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        [
                            "observe_seconds",
                            "skip_evidence",
                            "evidence_timeout",
                            "validation_command",
                            "expect_marker",
                            "reject_marker",
                            "expected_version",
                            "git_commit",
                            "changed_file",
                            "expected_effect",
                        ],
                    ),
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Run failed during {run_stage}: {exc}")
        print("[TODO] Check the latest session logs for the failing command.")
        return 1

    evidence_command_failures = [item for item in evidence_results["command_results"] if item.get("exit_code") != 0]
    evidence_file_failures = [item for item in evidence_results["file_results"] if item.get("status") != "ok"]
    has_evidence_failures = bool(evidence_command_failures or evidence_file_failures)
    evaluation_verdict = str(evaluation.get("verdict", ""))
    validation_verdict = str(validation_results.get("verdict", "pass"))
    result_key_excerpts: list[dict[str, str]] = _build_marker_window_excerpts(run_results.get("observation", {}))
    for finding in evaluation.get("findings", [])[:3]:
        result_key_excerpts.append(
            build_excerpt(
                source="evaluation",
                label=str(finding.get("check_name", "finding")),
                text=str(finding.get("message", "")),
                severity=str(finding.get("level", "info")),
            )
        )
    if evidence_command_failures:
        first_failure = evidence_command_failures[0]
        result_key_excerpts.append(
            build_excerpt(
                source="evidence_command",
                label=str(first_failure.get("name", "command_failure")),
                text=_format_output_excerpt(first_failure.get("output_lines", [])),
                severity="error",
            )
        )
    if evidence_file_failures:
        first_file_failure = evidence_file_failures[0]
        result_key_excerpts.append(
            build_excerpt(
                source="evidence_file",
                label=str(first_file_failure.get("remote_path", "file_failure")),
                text=str(first_file_failure.get("error") or first_file_failure.get("status") or "file fetch failed"),
                severity="error",
            )
        )
    result_key_excerpts.extend(_validation_key_excerpts(validation_results))
    result_decision = "success" if evaluation_verdict == "pass" and not has_evidence_failures and validation_verdict == "pass" else "continue"
    if result_decision == "success":
        result_failure_stage = None
    elif has_evidence_failures:
        result_failure_stage = "collect_evidence"
    else:
        result_failure_stage = "validation"
    state.transition_task(
        TaskState.FAILED if has_evidence_failures or evaluation_verdict == "fail" or validation_verdict == "fail" else TaskState.COMPLETED,
        "Run workflow finished.",
    )
    collector.update_summary(
        {
            "status": "run_partial" if has_evidence_failures or evaluation_verdict != "pass" or validation_verdict != "pass" else "run_completed",
            "run_results": run_results,
            "evaluation": evaluation,
            "evidence_results": evidence_results,
            "validation_results": validation_results,
            "intervention_context": intervention_context,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="run",
                decision=result_decision,
                failure_stage=result_failure_stage,
                retryable=False if result_decision == "success" else True,
                stop_reason=None
                if result_decision == "success"
                else validation_results.get("summary", "Target validation failed.")
                if result_failure_stage == "validation"
                else (evaluation.get("summary") or "Run completed without satisfying the target verdict."),
                key_excerpts=result_key_excerpts,
                next_actions=[] if result_decision == "success" else _build_run_next_actions(
                    failure_stage=result_failure_stage,
                    evaluation=evaluation,
                    has_evidence_failures=has_evidence_failures,
                ),
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(
                    args,
                    [
                        "observe_seconds",
                        "skip_evidence",
                        "evidence_timeout",
                        "validation_command",
                        "expect_marker",
                        "reject_marker",
                        "expected_version",
                        "git_commit",
                        "changed_file",
                        "expected_effect",
                    ],
                ),
                intervention_context=intervention_context,
            ),
        }
    )
    summary = json.loads((session.session_paths.root / "summary.json").read_text(encoding="utf-8"))
    _print_run_report(summary)
    print(f"[DONE] Run status: {summary.get('status', 'run_completed')}")
    print(f"[DONE] Baseline checks: {len(check_results)}")
    return 0 if result_decision == "success" else 1


def _command_stage_sd(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    if profiles.device.storage is None or not profiles.device.storage.sdcard_drive:
        print("[ oxx.. ] 2/5 steps")
        print("[ERROR] The selected device profile does not define storage.sdcard_drive.")
        print("[TODO] Add the host SD card drive path to config/user-settings.toml or the device profile, then retry.")
        return 1

    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_stage_sd",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.DEPLOYING, f"Staging {args.source.name} to SD card.")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_stage_sd",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="stage-sd",
        loop_context=loop_context,
        plan_details={
            "deploy": {
                "mode": "stage_sd",
                "source": str(args.source),
                "target_subdir": args.target_subdir,
                "dest_name": args.dest_name,
                "verify": not args.no_verify,
                "allow_non_removable": args.allow_non_removable,
                "sdcard_drive": profiles.device.storage.sdcard_drive,
            }
        },
    )

    deployer = Deployer(profiles.task, profiles.transport)
    try:
        result = deployer.stage_to_sd(
            source_path=args.source,
            sdcard_drive=profiles.device.storage.sdcard_drive,
            target_subdir=args.target_subdir,
            destination_name=args.dest_name,
            verify=not args.no_verify,
            allow_non_removable=args.allow_non_removable,
        )
    except Exception as exc:
        state.transition_task(TaskState.FAILED, "SD card staging failed.")
        collector.append_event(
            event_type="stage_sd_failed",
            source="deployer",
            summary=f"Failed to stage file to SD card: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "stage_sd_failed",
                "error": str(exc),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="stage-sd",
                    decision="continue",
                    failure_stage="deploy_artifacts",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="deployer", label="stage_sd_error", text=str(exc), severity="error")],
                    next_actions=_build_stage_sd_next_actions(failed=True),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["source", "target_subdir", "dest_name", "no_verify", "allow_non_removable"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] SD stage failed: {exc}")
        print("[TODO] Check that the configured SD card drive exists and is writable.")
        return 1

    state.transition_task(TaskState.COMPLETED, "SD card staging completed.")
    collector.append_event(
        event_type="stage_sd_completed",
        source="deployer",
        summary=f"Staged file to SD card: {result.target_path}",
        payload=result.to_dict(),
    )
    collector.update_summary(
        {
            "status": "stage_sd_completed",
            "deployment_result": result.to_dict(),
            "state": state.to_dict(),
            "result": build_result_contract(
                action="stage-sd",
                decision="success",
                failure_stage=None,
                retryable=False,
                key_excerpts=[
                    build_excerpt(source="deployer", label="target_path", text=str(result.target_path)),
                    build_excerpt(source="deployer", label="sha256", text=result.sha256),
                ],
                next_actions=_build_stage_sd_next_actions(failed=False),
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(
                    args,
                    ["source", "target_subdir", "dest_name", "no_verify", "allow_non_removable"],
                ),
            ),
        }
    )
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Staged file: {args.source.name}")
    print(f"[DONE] Target: {result.target_path}")
    print(f"[ACTIVE] SHA256: {result.sha256}")
    print(f"[TODO] Session directory: {session.session_paths.root}")
    return 0


def _command_storage(args: argparse.Namespace) -> int:
    configured_root: str | None = None
    retrieved_root: str | None = None
    settings = _load_user_settings_from_args(args)
    if settings.storage.sdcard_drive:
        configured_root = settings.storage.sdcard_drive
    if settings.storage.retrieved_root is not None:
        retrieved_root = str(settings.storage.retrieved_root)
    if args.device:
        device_profile = load_device_profile(args.device)
        if device_profile.storage is not None:
            configured_root = configured_root or device_profile.storage.sdcard_drive
            retrieved_root = retrieved_root or device_profile.storage.retrieved_root
    drives = list_host_drives()
    print("[ oooo. ] 4/5 steps")
    if not drives:
        print("[DONE] No host drives detected")
        return 0
    for drive in drives:
        marker = "DONE"
        if configured_root and get_drive_info(configured_root).root == drive.root:
            marker = "ACTIVE"
        volume = drive.volume_name or "(no label)"
        filesystem = drive.filesystem or "(no fs)"
        size = _format_bytes(drive.total_bytes)
        free = _format_bytes(drive.free_bytes)
        print(f"[{marker}] {drive.root} type={drive.drive_type} label={volume} fs={filesystem} size={size} free={free}")
    removable = [drive.root for drive in drives if drive.drive_type == "removable"]
    print(f"[TODO] Removable candidates: {', '.join(removable) if removable else 'none'}")
    if configured_root:
        configured_drive = get_drive_info(configured_root)
        print(
            f"[TODO] Configured device storage.sdcard_drive: {configured_drive.root} "
            f"(type={configured_drive.drive_type})"
        )
        if configured_drive.drive_type != "removable":
            print("[TODO] The configured drive is not removable. Profile update or media insertion is likely needed.")
    if retrieved_root:
        print(f"[TODO] Retrieved device files root: {retrieved_root}")
    return 0


def _command_bootstrap_network(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_bootstrap_network",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, f"Bootstrapping network mode={args.mode}.")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    wifi_settings = _resolve_wifi_settings(
        profiles,
        wifi_ssid=args.wifi_ssid,
        wifi_password=args.wifi_password,
        wifi_mode=args.wifi_mode,
        network_dir=args.network_dir,
    )
    bootstrap_commands = _resolve_bootstrap_commands(
        profiles,
        args.mode,
        args.bootstrap_command,
        **wifi_settings,
    )
    check_commands = _resolve_connectivity_checks(
        profiles,
        args.check_command,
        mode=args.mode,
    )
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_bootstrap_network",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="bootstrap-network",
        loop_context=loop_context,
        plan_details={
            "network": {
                "mode": args.mode,
                "bootstrap_commands": bootstrap_commands,
                "check_commands": check_commands,
                "wifi_ssid": wifi_settings.get("wifi_ssid"),
                "wifi_mode": wifi_settings.get("wifi_mode"),
                "network_dir": wifi_settings.get("network_dir"),
                "timeout": args.timeout,
            }
        },
    )

    if args.mode == "offline":
        collector.update_summary(
            {
                "status": "bootstrap_network_blocked",
                "error": "Network mode is offline; bootstrap was intentionally skipped.",
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="bootstrap-network",
                    decision="blocked",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason="Network mode is offline; bootstrap was intentionally skipped.",
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="offline_mode",
                            text="Network mode is offline; bootstrap was intentionally skipped.",
                            severity="warning",
                        )
                    ],
                    next_actions=_build_bootstrap_network_next_actions(blocked=True, mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["mode", "bootstrap_command", "check_command", "timeout", "wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print("[ERROR] Network mode is offline; bootstrap was intentionally skipped.")
        print("[TODO] Use SD rescue mode for this device/session.")
        return 1

    if args.mode == "wlan_script" and not bootstrap_commands:
        collector.update_summary(
            {
                "status": "bootstrap_network_failed",
                "error": "No bootstrap commands were provided for wlan_script mode.",
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="bootstrap-network",
                    decision="continue",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason="No bootstrap commands were provided for wlan_script mode.",
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="missing_bootstrap_commands",
                            text="No bootstrap commands were provided for wlan_script mode.",
                            severity="error",
                        )
                    ],
                    next_actions=_build_bootstrap_network_next_actions(mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["mode", "bootstrap_command", "check_command", "timeout", "wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print("[ERROR] No bootstrap commands were provided for wlan_script mode.")
        print("[TODO] Configure network.bootstrap_commands in the device profile or pass --bootstrap-command.")
        return 1

    if not check_commands:
        collector.update_summary(
            {
                "status": "bootstrap_network_failed",
                "error": "No connectivity checks were provided.",
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="bootstrap-network",
                    decision="continue",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason="No connectivity checks were provided.",
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="missing_connectivity_checks",
                            text="No connectivity checks were provided.",
                            severity="error",
                        )
                    ],
                    next_actions=_build_bootstrap_network_next_actions(mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["mode", "bootstrap_command", "check_command", "timeout", "wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print("[ERROR] No connectivity checks were provided.")
        print("[TODO] Configure network.connectivity_checks / expected_ip or pass --check-command.")
        return 1

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    bootstrap_results: list[dict[str, Any]] = []
    check_results: list[dict[str, Any]] = []
    try:
        state.transition_task(TaskState.RUNNING_CHECKS, "Executing bootstrap commands and connectivity checks.")
        state.transition_device(DeviceState.ROOT_SHELL, "Network bootstrap uses an interactive root shell.")
        collector.update_summary({"state": state.to_dict()})
        with open_serial_port(
            profiles.device.serial.port,
            profiles.device.serial.baudrate,
            timeout=0.2,
        ) as serial_port:
            for index, command in enumerate(bootstrap_commands, start=1):
                result = _execute_structured_command(
                    controller=controller,
                    shell_command=command,
                    timeout=args.timeout,
                    serial_port=serial_port,
                )
                _write_command_artifacts(collector=collector, prefix=f"bootstrap-network-{index}", result=result)
                bootstrap_results.append(result.to_dict())
                collector.append_event(
                    event_type="bootstrap_network_command",
                    source="device_controller",
                    summary=f"Executed bootstrap command #{index}",
                    payload=result.to_dict(),
                )
            for index, command in enumerate(check_commands, start=1):
                result = _execute_structured_command(
                    controller=controller,
                    shell_command=command,
                    timeout=args.timeout,
                    serial_port=serial_port,
                )
                _write_command_artifacts(collector=collector, prefix=f"bootstrap-network-check-{index}", result=result)
                check_results.append(result.to_dict())
                collector.append_event(
                    event_type="bootstrap_network_check",
                    source="device_controller",
                    summary=f"Executed network check #{index}",
                    payload=result.to_dict(),
                )
    except LoginRequiredError as exc:
        collector.update_summary(
            {
                "status": "bootstrap_network_blocked",
                "error": str(exc),
                "bootstrap_results": bootstrap_results,
                "check_results": check_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="bootstrap-network",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    next_actions=_build_bootstrap_network_next_actions(blocked=True, mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["mode", "bootstrap_command", "check_command", "timeout", "wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry bootstrap-network.")
        return 1
    except Exception as exc:
        state.transition_task(TaskState.FAILED, "Network bootstrap failed.")
        collector.update_summary(
            {
                "status": "bootstrap_network_failed",
                "error": str(exc),
                "bootstrap_results": bootstrap_results,
                "check_results": check_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="bootstrap-network",
                    decision="continue",
                    failure_stage="run_bootstrap_commands",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="workflow_runner", label="bootstrap_network_failed", text=str(exc), severity="error")],
                    next_actions=_build_bootstrap_network_next_actions(failed_checks=check_results, mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["mode", "bootstrap_command", "check_command", "timeout", "wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] bootstrap-network failed: {exc}")
        print("[TODO] Check command transcripts in the session logs.")
        return 1

    failed_checks = [result for result in check_results if result.get("exit_code") != 0]
    state.transition_task(TaskState.COMPLETED if not failed_checks else TaskState.FAILED, "Network bootstrap finished.")
    collector.update_summary(
        {
            "status": "bootstrap_network_completed" if not failed_checks else "bootstrap_network_failed",
            "bootstrap_results": bootstrap_results,
            "check_results": check_results,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="bootstrap-network",
                decision="success" if not failed_checks else "continue",
                failure_stage=None if not failed_checks else "validate_connectivity",
                retryable=False if not failed_checks else True,
                stop_reason=None if not failed_checks else f"{len(failed_checks)} connectivity check(s) failed.",
                key_excerpts=[
                    build_excerpt(
                        source="network_check",
                        label=str(result.get("name", "check")),
                        text=_format_output_excerpt(result.get("output_lines", [])),
                        severity="error",
                    )
                    for result in failed_checks[:3]
                ],
                next_actions=[] if not failed_checks else _build_bootstrap_network_next_actions(failed_checks=failed_checks, mode=args.mode),
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(
                    args,
                    ["mode", "bootstrap_command", "check_command", "timeout", "wifi_ssid", "wifi_password", "wifi_mode", "network_dir"],
                ),
            ),
        }
    )
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Bootstrap commands: {len(bootstrap_results)}")
    print(f"[DONE] Connectivity checks: {len(check_results)}")
    if failed_checks:
        print(f"[ERROR] Failed connectivity checks: {len(failed_checks)}")
    else:
        print("[DONE] Network bootstrap passed all connectivity checks")
    print(f"[TODO] Session directory: {session.session_paths.root}")
    return 0 if not failed_checks else 1


def _select_artifact_server_port(args: argparse.Namespace) -> int | None:
    requested_port = int(args.port)
    auto_port = bool(getattr(args, "auto_port", True)) and not getattr(args, "base_url", None)
    if auto_port:
        try:
            return find_available_port(args.bind, requested_port)
        except (OSError, ValueError) as exc:
            print("[ oxx.. ] 2/5 steps")
            print(f"[ERROR] Could not select a local artifact server port: {exc}")
            return None
    if not is_tcp_port_available(args.bind, requested_port):
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Artifact server port is already in use: {args.bind}:{requested_port}")
        print("[TODO] Stop the existing server or retry without --no-auto-port.")
        return None
    return requested_port


def _spawn_background_artifact_server(
    args: argparse.Namespace,
    *,
    root: Path,
    port: int,
    base_url: str,
    manifest_path: Path,
    pull_script_path: Path,
) -> int:
    health_url = _artifact_health_url(base_url, args.health_name)
    local_health_url = _local_artifact_health_url(args.bind, port, args.health_name)
    log_path = artifact_server_log_path(port)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "autodbg",
        "serve-artifacts",
        "--foreground",
        "--no-auto-port",
        "--root",
        str(root),
        "--bind",
        args.bind,
        "--port",
        str(port),
        "--settings",
        str(args.settings),
        "--base-url",
        base_url,
        "--manifest-name",
        args.manifest_name,
        "--workspace",
        args.workspace,
        "--pull-script-name",
        args.pull_script_name,
        "--health-name",
        args.health_name,
    ]
    if args.duration_seconds is not None:
        command.extend(["--duration-seconds", str(args.duration_seconds)])

    env = os.environ.copy()
    project_src = str(_project_root() / "src")
    env["PYTHONPATH"] = project_src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    with log_path.open("a", encoding="utf-8", newline="\n") as log_handle:
        process = subprocess.Popen(  # noqa: S603 - argv is constructed from parsed CLI args
            command,
            cwd=str(_project_root()),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    write_artifact_server_registry(
        ArtifactServerRegistry(
            root=str(root),
            bind=args.bind,
            port=port,
            base_url=base_url,
            health_url=health_url,
            pid=process.pid,
            manifest_path=str(manifest_path),
            pull_script_path=str(pull_script_path),
            log_path=str(log_path),
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
    )

    deadline = time.monotonic() + max(float(args.startup_timeout), 0.1)
    healthy = False
    while time.monotonic() < deadline:
        if probe_http_url(local_health_url, timeout=0.4):
            healthy = True
            break
        if process.poll() is not None:
            break
        time.sleep(0.1)

    if not healthy:
        if process.poll() is None:
            process.terminate()
        remove_artifact_server_registry(port)
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Artifact server did not become healthy: {local_health_url}")
        print(f"[TODO] Check server log: {log_path}")
        return 1

    registered_server = load_artifact_server_registry(port)
    server_pid = registered_server.pid if registered_server is not None else process.pid
    server_log_path = registered_server.log_path if registered_server is not None and registered_server.log_path else str(log_path)
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Artifact server started in background pid={server_pid}")
    print(f"[DONE] Serving root: {root}")
    print(f"[DONE] Manifest: {manifest_path}")
    print(f"[DONE] Pull script: {pull_script_path}")
    print(f"[DONE] Base URL: {base_url}/")
    print(f"[DONE] Health URL: {health_url}")
    print(f"[TODO] Stop with: autodbg artifact-server stop --port {port}")
    print(f"[TODO] Log: {server_log_path}")
    return 0


def _command_serve_artifacts(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    if not root.is_dir():
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Artifact root does not exist: {root}")
        print("[TODO] Create or populate the directory before serving it.")
        return 1

    selected_port = _select_artifact_server_port(args)
    if selected_port is None:
        return 1

    settings = _load_user_settings_from_args(args)
    configured_host_ip = settings.network.host_ip
    base_url = (args.base_url or _default_base_url(args.bind, selected_port, host_ip=configured_host_ip)).rstrip("/")
    if args.port == 0:
        print(f"[DONE] Selected ephemeral artifact server port {selected_port}.")
    elif selected_port != args.port:
        print(f"[DONE] Requested port {args.port} was unavailable; selected {selected_port}.")

    manifest_path = write_manifest(
        root,
        base_url=base_url,
        manifest_name=args.manifest_name,
        exclude_names=(args.pull_script_name,),
    )
    pull_script_path = write_pull_script(
        root,
        base_url=base_url,
        workspace=args.workspace,
        script_name=args.pull_script_name,
        manifest_name=args.manifest_name,
    )

    if not args.foreground:
        return _spawn_background_artifact_server(
            args,
            root=root,
            port=selected_port,
            base_url=base_url,
            manifest_path=manifest_path,
            pull_script_path=pull_script_path,
        )

    server, thread = serve_directory(
        root,
        bind=args.bind,
        port=selected_port,
        duration_seconds=args.duration_seconds,
        health_name=args.health_name,
    )
    health_url = _artifact_health_url(base_url, args.health_name)
    existing_registry = load_artifact_server_registry(selected_port)
    write_artifact_server_registry(
        ArtifactServerRegistry(
            root=str(root),
            bind=args.bind,
            port=selected_port,
            base_url=base_url,
            health_url=health_url,
            pid=os.getpid(),
            manifest_path=str(manifest_path),
            pull_script_path=str(pull_script_path),
            log_path=existing_registry.log_path if existing_registry is not None else None,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
    )
    try:
        print("[ oooo. ] 4/5 steps")
        print(f"[DONE] Serving root: {root}")
        print(f"[DONE] Manifest: {manifest_path}")
        print(f"[DONE] Pull script: {pull_script_path}")
        print(f"[ACTIVE] Base URL: {base_url}/")
        print(f"[DONE] Health URL: {health_url}")
        print(f"[TODO] Manifest URL: {base_url}/{args.manifest_name}")
        print(f"[TODO] Pull script URL: {base_url}/{args.pull_script_name}")
        if args.duration_seconds is None:
            print("[TODO] Press Ctrl+C to stop the local artifact server")
            thread.join()
        else:
            print(f"[TODO] Auto-stop after {args.duration_seconds:.1f}s")
            thread.join(timeout=args.duration_seconds + 2.0)
    except KeyboardInterrupt:
        print("[DONE] Artifact server stopped by user")
    finally:
        server.shutdown()
        server.server_close()
        remove_artifact_server_registry(selected_port)
    return 0


def _command_artifact_server(args: argparse.Namespace) -> int:
    if args.artifact_server_command == "list":
        registries = list_artifact_server_registries()
        if args.port is not None:
            registries = [registry for registry in registries if registry.port == args.port]
        if not registries:
            print("[ oo... ] 2/5 steps")
            print("[DONE] No artifact servers registered")
            return 0
        print("[ ooo.. ] 3/5 steps")
        for registry in registries:
            running = artifact_server_pid_is_running(registry.pid)
            healthy = probe_http_url(registry.health_url, timeout=0.5)
            status = "healthy" if healthy else "registered"
            if not running:
                status = "stale"
            print(f"[DONE] port={registry.port} pid={registry.pid} status={status} base_url={registry.base_url}")
            print(f"       root={registry.root}")
            print(f"       health={registry.health_url}")
            if registry.log_path:
                print(f"       log={registry.log_path}")
        return 0

    if args.artifact_server_command == "stop":
        result = stop_artifact_server(args.port)
        if result.get("status") == "missing":
            print("[ oo... ] 2/5 steps")
            print(f"[ERROR] No artifact server registry found for port {args.port}")
            return 1
        if result.get("status") == "error":
            print("[ oxx.. ] 2/5 steps")
            print(f"[ERROR] Failed to stop artifact server: {result.get('error')}")
            return 1
        print("[ ooo.. ] 3/5 steps")
        print(f"[DONE] Stopped artifact server port={result.get('port')} pid={result.get('pid')}")
        return 0

    print(f"[ERROR] Unsupported artifact-server command: {args.artifact_server_command}")
    return 1


def _normalize_quickstart_goal(goal: str | None) -> str:
    text = (goal or "").strip().lower()
    if not text:
        return "unknown"
    if any(token in text for token in ["quickstart", "快速开始", "快捷引导", "刚接触", "小白", "新手", "开始使用"]):
        return "quickstart"
    if any(token in text for token in ["helper", "curl", "wget", "交叉编译", "sd http"]):
        return "sd_http_helper"
    if any(token in text for token in ["闭环", "升级", "验证", "deploy", "刷机"]):
        return "deploy_verify"
    if any(token in text for token in ["下发", "拉取", "传输", "device-pull", "artifact", "程序"]):
        return "device_pull"
    if any(token in text for token in ["健康", "health", "检查", "状态"]):
        return "health"
    if any(token in text for token in ["串口", "日志", "observe", "watch", "serial"]):
        return "watch_serial"
    return "unknown"


def _build_quickstart_action_plan(args: argparse.Namespace, ports: list[dict[str, Any]]) -> dict[str, Any]:
    goal = _normalize_quickstart_goal(args.goal)
    serial_port = args.serial_port or (ports[0]["device"] if len(ports) == 1 else None)
    password_known = bool(args.device_password_known or os.environ.get("AUTO_DBG_DEVICE_PASSWORD"))

    questions: list[str] = []
    if serial_port is None and goal in {"unknown", "quickstart", "watch_serial", "health", "device_pull", "deploy_verify"}:
        questions.append("请提供目标设备串口号；如果不确定，先运行 ports 或从 detected_ports 里选择。")
    if goal in {"health", "device_pull", "deploy_verify"} and not password_known:
        questions.append("请提供设备 shell 登录密码；该密码只放 connection.device_password，不要写进普通日志。")
    if goal == "sd_http_helper" and not args.helper_cc:
        questions.append("请提供目标设备交叉编译器命令或完整路径，例如 arm-linux-gnueabihf-gcc。")
    if goal == "sd_http_helper" and not args.sdcard_drive:
        questions.append("请提供本机 SD 卡盘符，用于把编译出的 helper 放入 SD 卡，例如 E:。")
    if goal == "deploy_verify" and args.artifact is None:
        questions.append("请提供要部署验证的本地产物路径，例如 payloads/APP.bin。")
    if goal in {"unknown", "quickstart"}:
        questions.append("请说明你想做什么：观察串口、健康检查、下发程序、升级验证，或编译 SD HTTP helper。")

    connection: dict[str, Any] = {}
    if serial_port:
        connection["serial_port"] = serial_port
    if args.baudrate:
        connection["baudrate"] = args.baudrate
    if args.sdcard_drive:
        connection["sdcard_drive"] = args.sdcard_drive

    next_requests: list[dict[str, Any]] = []
    if serial_port is None and goal in {"unknown", "quickstart", "watch_serial", "health", "device_pull", "deploy_verify"}:
        next_requests.append({"schema_version": 1, "action": "ports"})
    if goal == "watch_serial":
        next_requests.append(
            {
                "schema_version": 1,
                "action": "watch-serial",
                "connection": connection,
                "options": {"tail": 40, "follow": True},
            }
        )
    elif goal == "health":
        next_requests.append(
            {
                "schema_version": 1,
                "action": "health",
                "connection": connection,
                "options": {"timeout": 20.0},
            }
        )
    elif goal == "device_pull":
        options: dict[str, Any] = {"mode": "lan_ready", "transfer_mode": "auto"}
        if args.artifact is not None:
            options["root"] = str(args.artifact.parent)
        next_requests.append(
            {
                "schema_version": 1,
                "action": "device-pull",
                "connection": connection,
                "options": options,
            }
        )
    elif goal == "deploy_verify":
        next_requests.append(
            {
                "schema_version": 1,
                "action": "deploy-verify",
                "connection": connection,
                "options": {
                    "artifact": str(args.artifact) if args.artifact else "<local-artifact-path>",
                    "observe_seconds": 10.0,
                    "transfer_mode": "auto",
                },
            }
        )
    elif goal == "sd_http_helper":
        next_requests.append(
            {
                "schema_version": 1,
                "action": "build-sd-http-helper",
                "options": {"cc": args.helper_cc or "<target-gcc>", "output": "artifacts/autodbg-http-pull"},
            }
        )
        if args.sdcard_drive:
            next_requests.append(
                {
                    "schema_version": 1,
                    "action": "stage-sd",
                    "connection": {"sdcard_drive": args.sdcard_drive},
                    "options": {
                        "source": "artifacts/autodbg-http-pull",
                        "target_subdir": "autodbg",
                        "dest_name": "autodbg-http-pull",
                    },
                }
            )

    return {
        "goal": goal,
        "ready": bool(next_requests) and not questions,
        "detected_ports": ports,
        "supplied": {
            "serial_port": serial_port,
            "baudrate": args.baudrate,
            "device_password_known": password_known,
            "sdcard_drive": args.sdcard_drive,
            "helper_cc": args.helper_cc,
            "artifact": str(args.artifact) if args.artifact else None,
        },
        "questions": questions[:3],
        "next_requests": next_requests,
        "agent_instructions": [
            "Call autodbg_prepare on the selected next_request before autodbg_action.",
            "Ask the questions list first, at most three questions at a time.",
            "Do not print device_password; pass it only through connection.device_password.",
            "If device_password_known is true, reuse the existing secret source and never copy a placeholder value into connection.device_password.",
        ],
    }


def _command_quickstart(args: argparse.Namespace) -> int:
    try:
        ports = list_serial_ports()
    except SerialSupportError:
        ports = []
    payload = _build_quickstart_action_plan(args, ports)
    print("[ ●●○○○ ] 2/5 steps")
    if payload["questions"]:
        print("[ACTIVE] Quickstart guidance needs user input")
        print("[ACTIVE] Missing inputs:")
        for question in payload["questions"]:
            print(f"[TODO] {question}")
    else:
        print("[DONE] Quickstart guidance generated")
        print("[DONE] Ready to run the suggested next request")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _command_device_pull(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_device_pull",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, f"Preparing device pull mode={args.mode}.")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    wifi_settings = _resolve_wifi_settings(
        profiles,
        wifi_ssid=args.wifi_ssid,
        wifi_password=args.wifi_password,
        wifi_mode=args.wifi_mode,
        network_dir=args.network_dir,
    )
    bootstrap_commands = _resolve_bootstrap_commands(
        profiles,
        args.mode,
        args.bootstrap_command,
        **wifi_settings,
    )
    base_url = _resolve_pull_base_url(profiles, args.base_url, bind=args.bind, port=args.port)
    check_commands = _resolve_connectivity_checks(
        profiles,
        args.check_command,
        mode=args.mode,
        base_url=base_url,
    )
    workspace = _resolve_pull_workspace(profiles, args.workspace)
    root = args.root.resolve()
    list_command = args.list_command or f"find {_sh_single_quote(workspace)} -maxdepth 3 -type f | sort"
    carry_forward_options = _collect_option_patch(
        args,
        [
            "mode",
            "transfer_mode",
            "workspace",
            "root",
            "bind",
            "port",
            "base_url",
            "bootstrap_command",
            "check_command",
            "list_command",
            "post_pull_command",
            "reboot_command",
            "post_observe_seconds",
            "validation_command",
            "expect_marker",
            "reject_marker",
            "expected_version",
            "git_commit",
            "changed_file",
            "expected_effect",
            "manifest_name",
            "pull_script_name",
            "sd_http_helper_path",
            "sd_http_list_name",
            "serial_bundle_chunk_size",
            "timeout",
            "wifi_ssid",
            "wifi_password",
            "wifi_mode",
            "network_dir",
        ],
    )
    intervention_context = _build_intervention_context(args)
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_device_pull",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="device-pull",
        loop_context=loop_context,
        plan_details={
            "network": {
                "mode": args.mode,
                "bootstrap_commands": bootstrap_commands,
                "check_commands": check_commands,
                "wifi_ssid": wifi_settings.get("wifi_ssid"),
                "wifi_mode": wifi_settings.get("wifi_mode"),
                "network_dir": wifi_settings.get("network_dir"),
                "base_url": base_url,
                "workspace": workspace,
                "manifest_name": args.manifest_name,
                "pull_script_name": args.pull_script_name,
                "sd_http_helper_path": args.sd_http_helper_path,
                "sd_http_list_name": args.sd_http_list_name,
                "requested_transfer_mode": args.transfer_mode,
                "serial_bundle_chunk_size": args.serial_bundle_chunk_size,
                "list_command": list_command,
                "post_pull_commands": list(args.post_pull_command),
                "reboot_command": args.reboot_command,
                "post_observe_seconds": args.post_observe_seconds,
                "validation": {
                    "validation_commands": list(args.validation_command),
                    "expect_markers": list(args.expect_marker),
                    "reject_markers": list(args.reject_marker),
                    "expected_version": args.expected_version,
                },
                "timeout": args.timeout,
            }
        },
    )

    if args.mode == "offline" and args.transfer_mode in {"http", "sd_http_helper"}:
        transfer_label = "HTTP" if args.transfer_mode == "http" else "SD HTTP helper"
        error_text = f"Network mode is offline, so {transfer_label} transfer is not available."
        collector.update_summary(
            {
                "status": "device_pull_blocked",
                "error": error_text,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="device-pull",
                    decision="blocked",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason=error_text,
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="offline_http_unavailable",
                            text=error_text,
                            severity="warning",
                        )
                    ],
                    next_actions=_build_device_pull_next_actions(blocked=True, mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {error_text}")
        print("[TODO] Retry with --transfer-mode serial_bundle or auto when the device supports base64 + tar.")
        return 1

    if args.mode == "wlan_script" and not bootstrap_commands:
        collector.update_summary(
            {
                "status": "device_pull_failed",
                "error": "No bootstrap commands were provided for wlan_script mode.",
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="device-pull",
                    decision="continue",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason="No bootstrap commands were provided for wlan_script mode.",
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="missing_bootstrap_commands",
                            text="No bootstrap commands were provided for wlan_script mode.",
                            severity="error",
                        )
                    ],
                    next_actions=_build_device_pull_next_actions(mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print("[ERROR] No bootstrap commands were provided for wlan_script mode.")
        print("[TODO] Configure network.bootstrap_commands in the device profile or pass --bootstrap-command.")
        return 1

    if args.mode != "offline" and args.transfer_mode != "serial_bundle" and not check_commands:
        collector.update_summary(
            {
                "status": "device_pull_failed",
                "error": "No connectivity checks were provided.",
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="device-pull",
                    decision="continue",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason="No connectivity checks were provided.",
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="missing_connectivity_checks",
                            text="No connectivity checks were provided.",
                            severity="error",
                        )
                    ],
                    next_actions=_build_device_pull_next_actions(mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print("[ERROR] No connectivity checks were provided.")
        print("[TODO] Configure network.connectivity_checks / expected_ip or pass --check-command.")
        return 1

    if not root.is_dir():
        collector.update_summary(
            {
                "status": "device_pull_failed",
                "error": f"Artifact root does not exist: {root}",
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="device-pull",
                    decision="continue",
                    failure_stage="prepare_transport",
                    retryable=True,
                    stop_reason=f"Artifact root does not exist: {root}",
                    key_excerpts=[
                        build_excerpt(
                            source="workflow_runner",
                            label="missing_artifact_root",
                            text=f"Artifact root does not exist: {root}",
                            severity="error",
                        )
                    ],
                    next_actions=_build_device_pull_next_actions(mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Artifact root does not exist: {root}")
        print("[TODO] Create or populate the directory before retrying device-pull.")
        return 1

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    bootstrap_results: list[dict[str, Any]] = []
    check_results: list[dict[str, Any]] = []
    transfer_probe: dict[str, Any] | None = None
    selected_transfer_mode: str | None = None
    transfer_details: dict[str, Any] = {}
    pull_result: dict[str, Any] | None = None
    list_result: dict[str, Any] | None = None
    post_pull_results: list[dict[str, Any]] = []
    reboot_result: dict[str, Any] | None = None
    post_observation: dict[str, Any] | None = None
    validation_command_results: list[dict[str, Any]] = []
    validation_results: dict[str, Any] = _evaluate_validation_spec(
        expect_markers=[],
        reject_markers=[],
        expected_version=None,
        observation=None,
        command_results=[],
    )
    manifest_path: Path | None = None
    pull_script_path: Path | None = None
    device_pull_stage = "transfer_artifacts"
    server = None
    thread = None

    try:
        state.transition_task(TaskState.RUNNING_CHECKS, "Executing bootstrap commands, checks, and device pull.")
        state.transition_device(DeviceState.ROOT_SHELL, "Device pull uses an interactive root shell.")
        collector.update_summary({"state": state.to_dict()})
        with open_serial_port(
            profiles.device.serial.port,
            profiles.device.serial.baudrate,
            timeout=0.2,
        ) as serial_port:
            for index, command in enumerate(bootstrap_commands, start=1):
                result = _execute_structured_command(
                    controller=controller,
                    shell_command=command,
                    timeout=args.timeout,
                    serial_port=serial_port,
                )
                _write_command_artifacts(collector=collector, prefix=f"device-pull-bootstrap-{index}", result=result)
                bootstrap_results.append(result.to_dict())
                collector.append_event(
                    event_type="device_pull_bootstrap_command",
                    source="device_controller",
                    summary=f"Executed bootstrap command #{index}",
                    payload=result.to_dict(),
                )
            for index, command in enumerate(check_commands, start=1):
                result = _execute_structured_command(
                    controller=controller,
                    shell_command=command,
                    timeout=args.timeout,
                    serial_port=serial_port,
                )
                _write_command_artifacts(collector=collector, prefix=f"device-pull-check-{index}", result=result)
                check_results.append(result.to_dict())
                collector.append_event(
                    event_type="device_pull_check",
                    source="device_controller",
                    summary=f"Executed connectivity check #{index}",
                    payload=result.to_dict(),
                )
            transfer_probe_result = controller.execute(
                _build_transfer_probe_command(args.sd_http_helper_path),
                timeout=args.timeout,
                serial_port=serial_port,
            )
            _write_command_artifacts(collector=collector, prefix="device-pull-transfer-probe", result=transfer_probe_result)
            transfer_probe = {
                "result": transfer_probe_result.to_dict(),
                "capabilities": _parse_transfer_capabilities(transfer_probe_result.output_lines),
            }
            collector.append_event(
                event_type="device_pull_transfer_probe",
                source="device_controller",
                summary="Probed device-side transfer capabilities",
                payload=transfer_probe,
            )
            selected_transfer_mode = _select_transfer_mode(
                args.transfer_mode,
                capabilities=transfer_probe["capabilities"],
                network_mode=args.mode,
            )

            if selected_transfer_mode == "http":
                manifest_path = write_manifest(
                    root,
                    base_url=base_url,
                    manifest_name=args.manifest_name,
                    exclude_names=(args.pull_script_name,),
                )
                pull_script_path = write_pull_script(
                    root,
                    base_url=base_url,
                    workspace=workspace,
                    script_name=args.pull_script_name,
                    manifest_name=args.manifest_name,
                )
                server, thread = serve_directory(root, bind=args.bind, port=args.port)
                remote_pull_command = _build_remote_pull_command(base_url, workspace=workspace, script_name=args.pull_script_name)
                try:
                    pull_exec_result = controller.execute(remote_pull_command, timeout=args.timeout, serial_port=serial_port)
                finally:
                    server, thread = _shutdown_transient_artifact_server(
                        server,
                        thread,
                        collector=collector,
                        reason="http_transfer_finished",
                    )
                _write_command_artifacts(collector=collector, prefix="device-pull-run", result=pull_exec_result)
                pull_result = pull_exec_result.to_dict()
                transfer_details = {
                    "mode": "http",
                    "server": {
                        "root": str(root),
                        "base_url": base_url,
                        "manifest_path": str(manifest_path),
                        "pull_script_path": str(pull_script_path),
                    },
                }
                collector.append_event(
                    event_type="device_pull_run",
                    source="device_controller",
                    summary=f"Executed device pull script from {base_url}",
                    payload=pull_result,
                )
            elif selected_transfer_mode == "sd_http_helper":
                manifest_path = write_manifest(
                    root,
                    base_url=base_url,
                    manifest_name=args.manifest_name,
                    exclude_names=(args.pull_script_name, args.sd_http_list_name),
                )
                transfer_list_path = write_transfer_list(
                    root,
                    list_name=args.sd_http_list_name,
                    include_names=(args.manifest_name,),
                    exclude_names=(args.pull_script_name,),
                )
                server, thread = serve_directory(root, bind=args.bind, port=args.port)
                helper_command = _build_sd_http_helper_command(
                    base_url,
                    workspace=workspace,
                    helper_path=args.sd_http_helper_path,
                    list_name=args.sd_http_list_name,
                )
                should_fallback_to_http = False
                try:
                    helper_result = controller.execute(helper_command, timeout=args.timeout, serial_port=serial_port)
                    downloader = transfer_probe["capabilities"].get("downloader", "unknown")
                    should_fallback_to_http = args.transfer_mode == "auto" and helper_result.exit_code != 0 and downloader not in {"none", "unknown"}
                finally:
                    if not should_fallback_to_http:
                        server, thread = _shutdown_transient_artifact_server(
                            server,
                            thread,
                            collector=collector,
                            reason="sd_http_helper_transfer_finished",
                        )
                _write_command_artifacts(collector=collector, prefix="device-pull-sd-http-helper", result=helper_result)
                pull_result = helper_result.to_dict()
                transfer_details = {
                    "mode": "sd_http_helper",
                    "server": {
                        "root": str(root),
                        "base_url": base_url,
                        "manifest_path": str(manifest_path),
                        "transfer_list_path": str(transfer_list_path),
                    },
                    "helper": {
                        "device_path": args.sd_http_helper_path,
                        "list_name": args.sd_http_list_name,
                    },
                }
                collector.append_event(
                    event_type="device_pull_sd_http_helper",
                    source="device_controller",
                    summary=f"Executed SD-card HTTP helper from {args.sd_http_helper_path}",
                    payload=pull_result,
                )
                if should_fallback_to_http:
                    try:
                        pull_script_path = write_pull_script(
                            root,
                            base_url=base_url,
                            workspace=workspace,
                            script_name=args.pull_script_name,
                            manifest_name=args.manifest_name,
                            exclude_names=(args.sd_http_list_name,),
                        )
                        fallback_command = _build_remote_pull_command(base_url, workspace=workspace, script_name=args.pull_script_name)
                        fallback_result = controller.execute(fallback_command, timeout=args.timeout, serial_port=serial_port)
                    finally:
                        server, thread = _shutdown_transient_artifact_server(
                            server,
                            thread,
                            collector=collector,
                            reason="fallback_http_transfer_finished",
                        )
                    _write_command_artifacts(
                        collector=collector,
                        prefix="device-pull-sd-http-helper-fallback-http",
                        result=fallback_result,
                    )
                    pull_result = fallback_result.to_dict()
                    selected_transfer_mode = "http"
                    transfer_details = {
                        "mode": "http",
                        "fallback_from": "sd_http_helper",
                        "fallback_reason": f"SD HTTP helper returned exit code {helper_result.exit_code}",
                        "server": {
                            "root": str(root),
                            "base_url": base_url,
                            "manifest_path": str(manifest_path),
                            "transfer_list_path": str(transfer_list_path),
                            "pull_script_path": str(pull_script_path),
                        },
                        "previous_helper": {
                            "device_path": args.sd_http_helper_path,
                            "list_name": args.sd_http_list_name,
                            "result": helper_result.to_dict(),
                        },
                    }
                    collector.append_event(
                        event_type="device_pull_sd_http_helper_fallback_http",
                        source="device_controller",
                        summary="SD-card HTTP helper failed in auto mode; retried with device downloader.",
                        payload=pull_result,
                    )
            else:
                bundle = build_serial_bundle(
                    root,
                    output_path=session.session_paths.deploy_dir / "autodbg-serial-bundle.tar",
                    exclude_names=(args.manifest_name, args.pull_script_name),
                )
                collector.write_text_artifact(
                    "deploy/serial-bundle.json",
                    json.dumps(bundle.to_dict(), indent=2, ensure_ascii=False) + "\n",
                )
                prepare_command, upload_commands, finalize_command = _build_serial_bundle_commands(
                    base64_payload=bundle.read_base64_text(),
                    workspace=workspace,
                    chunk_size=args.serial_bundle_chunk_size,
                    artifact_count=bundle.artifact_count,
                )
                prepare_result = controller.execute(prepare_command, timeout=args.timeout, serial_port=serial_port)
                _write_command_artifacts(collector=collector, prefix="device-pull-serial-prepare", result=prepare_result)
                if prepare_result.exit_code != 0:
                    pull_result = prepare_result.to_dict()
                else:
                    for index, command in enumerate(upload_commands, start=1):
                        chunk_result = controller.execute(command, timeout=args.timeout, serial_port=serial_port)
                        if chunk_result.exit_code != 0:
                            pull_result = chunk_result.to_dict()
                            raise RuntimeError(
                                f"Serial bundle chunk {index}/{len(upload_commands)} failed with exit code {chunk_result.exit_code}."
                            )
                        if index == 1 or index == len(upload_commands) or index % 25 == 0:
                            collector.append_event(
                                event_type="device_pull_serial_bundle_progress",
                                source="device_controller",
                                summary=f"Uploaded serial bundle chunk {index}/{len(upload_commands)}",
                                payload={"chunk_index": index, "chunk_count": len(upload_commands)},
                            )
                    finalize_result = controller.execute(
                        finalize_command,
                        timeout=max(args.timeout, 60.0),
                        serial_port=serial_port,
                    )
                    _write_command_artifacts(collector=collector, prefix="device-pull-serial-finalize", result=finalize_result)
                    pull_result = finalize_result.to_dict()
                transfer_details = {
                    "mode": "serial_bundle",
                    "bundle": {
                        **bundle.to_dict(),
                        "chunk_size": args.serial_bundle_chunk_size,
                        "chunk_count": len(upload_commands),
                    },
                }
                collector.append_event(
                    event_type="device_pull_serial_bundle",
                    source="device_controller",
                    summary=f"Transferred tar bundle over serial into {workspace}",
                    payload=transfer_details,
                )

            if pull_result is not None and pull_result.get("exit_code") == 0:
                device_pull_stage = "validate_transfer"
                list_exec_result = _execute_structured_command(
                    controller=controller,
                    shell_command=list_command,
                    timeout=args.timeout,
                    serial_port=serial_port,
                )
                _write_command_artifacts(collector=collector, prefix="device-pull-list", result=list_exec_result)
                list_result = list_exec_result.to_dict()
                collector.append_event(
                    event_type="device_pull_list",
                    source="device_controller",
                    summary="Listed pulled files from the device workspace",
                    payload=list_result,
                )
                device_pull_stage = "deploy"
                for index, command in enumerate(args.post_pull_command, start=1):
                    post_result = _execute_structured_command(
                        controller=controller,
                        shell_command=command,
                        timeout=args.timeout,
                        serial_port=serial_port,
                    )
                    _write_command_artifacts(collector=collector, prefix=f"device-pull-post-{index}", result=post_result)
                    post_pull_results.append(post_result.to_dict())
                    collector.append_event(
                        event_type="device_pull_post_command",
                        source="device_controller",
                        summary=f"Executed post-pull command #{index}",
                        payload=post_result.to_dict(),
                        severity="error" if post_result.exit_code != 0 else "info",
                    )
                    if post_result.exit_code != 0:
                        raise RuntimeError(f"Post-pull command failed: {command}")
                if args.reboot_command:
                    reboot_exec_result = _execute_structured_command(
                        controller=controller,
                        shell_command=args.reboot_command,
                        timeout=args.timeout,
                        serial_port=serial_port,
                    )
                    _write_command_artifacts(collector=collector, prefix="device-pull-reboot", result=reboot_exec_result)
                    reboot_result = reboot_exec_result.to_dict()
                    collector.append_event(
                        event_type="device_pull_reboot_command",
                        source="device_controller",
                        summary="Executed reboot/restart command after pull",
                        payload=reboot_result,
                        severity="error" if reboot_exec_result.exit_code != 0 else "info",
                    )
                    if reboot_exec_result.exit_code != 0:
                        raise RuntimeError(f"Reboot command failed: {args.reboot_command}")
                validation_command_results = _run_validation_commands(
                    controller=controller,
                    collector=collector,
                    commands=list(args.validation_command),
                    timeout=args.timeout,
                    serial_port=serial_port,
                    prefix="device-pull-validation",
                )
                device_pull_stage = "validation"
            if args.post_observe_seconds > 0:
                device_pull_stage = "observe_serial"
                observer = SerialObserver(profiles.device.serial, profiles.model)
                observation = observer.capture(
                    seconds=args.post_observe_seconds,
                    log_path=session.session_paths.logs_dir / "device-pull-post-serial.log",
                )
                post_observation = observation.to_dict()
                collector.append_event(
                    event_type="device_pull_post_observation",
                    source="serial_observer",
                    summary=f"Captured {observation.lines_captured} post-pull serial lines.",
                    payload=post_observation,
                )
            device_pull_stage = "validation"
            validation_results = _evaluate_validation_spec(
                expect_markers=list(args.expect_marker),
                reject_markers=list(args.reject_marker),
                expected_version=args.expected_version,
                observation=post_observation,
                command_results=validation_command_results,
            )
    except LoginRequiredError as exc:
        collector.update_summary(
            {
                "status": "device_pull_blocked",
                "error": str(exc),
                "bootstrap_results": bootstrap_results,
                "check_results": check_results,
                "transfer_probe": transfer_probe,
                "selected_transfer_mode": selected_transfer_mode,
                "transfer_details": transfer_details,
                "pull_result": pull_result,
                "list_result": list_result,
                "post_pull_results": post_pull_results,
                "reboot_result": reboot_result,
                "post_observation": post_observation,
                "validation_results": validation_results,
                "intervention_context": intervention_context,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="device-pull",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    next_actions=_build_device_pull_next_actions(blocked=True, mode=args.mode),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry device-pull.")
        return 1
    except Exception as exc:
        state.transition_task(TaskState.FAILED, "Device pull failed.")
        collector.update_summary(
            {
                "status": "device_pull_failed",
                "error": str(exc),
                "bootstrap_results": bootstrap_results,
                "check_results": check_results,
                "transfer_probe": transfer_probe,
                "selected_transfer_mode": selected_transfer_mode,
                "transfer_details": transfer_details,
                "pull_result": pull_result,
                "list_result": list_result,
                "post_pull_results": post_pull_results,
                "reboot_result": reboot_result,
                "post_observation": post_observation,
                "validation_results": validation_results,
                "intervention_context": intervention_context,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="device-pull",
                    decision="continue",
                    failure_stage=device_pull_stage,
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="workflow_runner", label="device_pull_failed", text=str(exc), severity="error")],
                    next_actions=_build_device_pull_next_actions(
                        failed_checks=check_results,
                        pull_failed=True,
                        mode=args.mode,
                    ),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] device-pull failed: {exc}")
        print("[TODO] Check the latest session logs for bootstrap, pull, and list command transcripts.")
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2.0)

    failed_checks = [result for result in check_results if result.get("exit_code") != 0]
    pull_failed = pull_result is None or pull_result.get("exit_code") != 0
    list_failed = list_result is None or list_result.get("exit_code") != 0
    post_pull_failed = any(result.get("exit_code") != 0 for result in post_pull_results)
    reboot_failed = reboot_result is not None and reboot_result.get("exit_code") != 0
    validation_failed = validation_results.get("verdict") == "fail"
    result_key_excerpts: list[dict[str, str]] = [
        build_excerpt(
            source="network_check",
            label=str(result.get("name", "check")),
            text=_format_output_excerpt(result.get("output_lines", [])),
            severity="error",
        )
        for result in failed_checks[:2]
    ]
    if pull_failed and pull_result is not None:
        result_key_excerpts.append(
            build_excerpt(
                source="device_pull",
                label="pull_result",
                text=_format_output_excerpt(pull_result.get("output_lines", [])),
                severity="error",
            )
        )
    if list_failed and list_result is not None:
        result_key_excerpts.append(
            build_excerpt(
                source="device_pull",
                label="list_result",
                text=_format_output_excerpt(list_result.get("output_lines", [])),
                severity="error",
            )
        )
    result_key_excerpts.extend(_validation_key_excerpts(validation_results))
    result_failure_stage = None
    if failed_checks:
        result_failure_stage = "validate_connectivity"
    elif pull_failed:
        result_failure_stage = "transfer_artifacts"
    elif list_failed:
        result_failure_stage = "validate_transfer"
    elif post_pull_failed or reboot_failed:
        result_failure_stage = "deploy"
    elif validation_failed:
        result_failure_stage = "validation"
    workflow_ok = not failed_checks and not pull_failed and not list_failed and not post_pull_failed and not reboot_failed and not validation_failed
    state.transition_task(
        TaskState.COMPLETED if workflow_ok else TaskState.FAILED,
        "Device pull finished.",
    )
    collector.update_summary(
        {
            "status": "device_pull_completed" if workflow_ok else "device_pull_failed",
            "bootstrap_results": bootstrap_results,
            "check_results": check_results,
            "transfer_probe": transfer_probe,
            "selected_transfer_mode": selected_transfer_mode,
            "transfer_details": transfer_details,
            "pull_result": pull_result,
            "list_result": list_result,
            "post_pull_results": post_pull_results,
            "reboot_result": reboot_result,
            "post_observation": post_observation,
            "validation_results": validation_results,
            "intervention_context": intervention_context,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="device-pull",
                decision="success" if workflow_ok else "continue",
                failure_stage=result_failure_stage,
                retryable=False if workflow_ok else True,
                stop_reason=None
                if workflow_ok
                else (
                    f"{len(failed_checks)} connectivity check(s) failed."
                    if failed_checks
                    else "Artifact transfer returned a non-zero exit code."
                    if pull_failed
                    else "Post-transfer listing failed."
                    if list_failed
                    else "Post-pull deploy command failed."
                    if post_pull_failed or reboot_failed
                    else validation_results.get("summary", "Target validation failed.")
                ),
                key_excerpts=result_key_excerpts,
                next_actions=[]
                if workflow_ok
                else _build_device_pull_next_actions(
                    failed_checks=failed_checks,
                    pull_failed=pull_failed,
                    list_failed=list_failed,
                    mode=args.mode,
                ),
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=carry_forward_options,
                intervention_context=intervention_context,
            ),
        }
    )
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Device workspace: {workspace}")
    print(f"[DONE] Connectivity checks: {len(check_results)}")
    print(f"[DONE] Transfer mode: {selected_transfer_mode or 'unknown'}")
    if failed_checks:
        print(f"[ERROR] Failed connectivity checks: {len(failed_checks)}")
    elif pull_failed:
        print("[ERROR] Artifact transfer returned a non-zero exit code")
    elif list_failed:
        print("[ERROR] Post-transfer listing failed")
    elif post_pull_failed or reboot_failed:
        print("[ERROR] Post-pull deploy command failed")
    elif validation_failed:
        print("[ERROR] Target validation failed")
    else:
        print("[DONE] Device pull completed successfully")
    if selected_transfer_mode == "http":
        print(f"[ACTIVE] Base URL: {base_url}/")
    elif selected_transfer_mode == "sd_http_helper":
        print(f"[ACTIVE] Base URL: {base_url}/")
        print(f"[ACTIVE] SD HTTP helper: {args.sd_http_helper_path}")
    elif transfer_details:
        bundle_info = transfer_details.get("bundle", {})
        print(
            "[ACTIVE] Serial bundle: "
            f"{bundle_info.get('artifact_count', 'unknown')} files, "
            f"{_format_bytes(bundle_info.get('size_bytes'))}, "
            f"{bundle_info.get('chunk_count', 'unknown')} chunks"
        )
    print(f"[TODO] Session directory: {session.session_paths.root}")
    return 0 if workflow_ok else 1


def _run_host_build_command(command: str, *, timeout: float) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=_project_root(),
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout.splitlines(),
        "stderr": completed.stderr.splitlines(),
    }


def _shutdown_transient_artifact_server(
    server,
    thread,
    *,
    collector: EvidenceCollector | None = None,
    reason: str = "transfer_finished",
):
    if server is None:
        return None, None
    payload: dict[str, Any] = {"reason": reason}
    try:
        server.shutdown()
        server.server_close()
    except Exception as exc:
        payload["error"] = str(exc)
    if thread is not None:
        thread.join(timeout=2.0)
        payload["thread_alive"] = bool(thread.is_alive())
    if collector is not None:
        collector.append_event(
            event_type="artifact_server_closed",
            source="artifact_server",
            summary="Closed transient artifact HTTP server after device pull transfer.",
            payload=payload,
            severity="error" if payload.get("error") else "info",
        )
    return None, None


def _default_sd_http_helper_source() -> Path:
    try:
        return Path(resources.files("autodbg.assets").joinpath("autodbg_http_pull.c"))
    except (TypeError, FileNotFoundError):
        return _project_root() / "src" / "autodbg" / "assets" / "autodbg_http_pull.c"


def _resolve_project_relative_output(path: Path) -> Path:
    if path.is_absolute():
        return path
    project_root = _project_root().resolve()
    output = (project_root / path).resolve()
    if not output.is_relative_to(project_root):
        raise ValueError(f"Relative --output escapes project root: {path}")
    return output


def _build_sd_http_helper_compile_command(args: argparse.Namespace) -> tuple[list[str], Path, Path]:
    source = args.source or _default_sd_http_helper_source()
    output = args.output
    if not source.is_absolute():
        source = _project_root() / source
    output = _resolve_project_relative_output(output)
    cflags = ["-Os", *list(args.cflag)]
    if args.static:
        cflags.append("-static")
    command = [args.cc, *cflags, "-o", str(output), str(source)]
    return command, source, output


def _command_build_sd_http_helper(args: argparse.Namespace) -> int:
    try:
        command, source, output = _build_sd_http_helper_compile_command(args)
    except ValueError as exc:
        print("[ ●○○○○ ] 1/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Use an output path under the project, or pass an absolute path deliberately.")
        return 2
    print("[ ●●○○○ ] 2/5 steps")
    print(f"[ACTIVE] Compiler: {args.cc}")
    print(f"[ACTIVE] Source: {source}")
    print(f"[ACTIVE] Output: {output}")

    if not source.is_file():
        print(f"[ERROR] Helper source not found: {source}")
        print("[TODO] Check --source or reinstall the local MCP tool.")
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            command,
            cwd=_project_root(),
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
    except FileNotFoundError:
        print(f"[ERROR] Compiler not found: {args.cc}")
        print("[TODO] Provide a target cross compiler path with --cc, or add it to PATH.")
        return 127
    except subprocess.TimeoutExpired:
        print(f"[ERROR] Compiler timed out after {args.timeout:.1f}s")
        print("[TODO] Check the target toolchain or increase --timeout.")
        return 124

    payload = {
        "command": command,
        "source": str(source),
        "output": str(output),
        "exit_code": completed.returncode,
        "stdout": completed.stdout.splitlines(),
        "stderr": completed.stderr.splitlines(),
        "output_exists": output.is_file(),
        "output_size_bytes": output.stat().st_size if output.is_file() else 0,
        "next_actions": [
            {
                "action": "stage-sd",
                "reason": "Copy the compiled helper to /mnt/sdcard/autodbg/autodbg-http-pull before device-pull auto selection.",
                "source": str(output),
                "target_subdir": "autodbg",
                "dest_name": "autodbg-http-pull",
            }
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if completed.returncode == 0:
        print("[DONE] SD HTTP helper built")
        return 0
    print("[ERROR] SD HTTP helper build failed")
    print("[TODO] Inspect compiler stderr and adjust --cc / --cflag / --no-static.")
    return completed.returncode


def _command_deploy_verify(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_deploy_verify",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.DEPLOYING, "Preparing closed deploy/verify workflow.")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    carry_forward_options = _collect_option_patch(
        args,
        [
            "build_command",
            "artifact",
            "post_pull_command",
            "reboot_command",
            "observe_seconds",
            "timeout",
            "validation_command",
            "expect_marker",
            "reject_marker",
            "expected_version",
            "git_commit",
            "changed_file",
            "expected_effect",
        ],
    )
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_deploy_verify",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="deploy-verify",
        loop_context=loop_context,
        plan_details={
            "closed_loop": {
                "build_command": args.build_command,
                "artifact": str(args.artifact) if args.artifact else None,
                "post_pull_commands": list(args.post_pull_command),
                "reboot_command": args.reboot_command,
                "observe_seconds": args.observe_seconds,
                "validation_commands": list(args.validation_command),
                "expect_markers": list(args.expect_marker),
                "reject_markers": list(args.reject_marker),
                "expected_version": args.expected_version,
            }
        },
    )

    artifact_path = args.artifact.resolve() if args.artifact else None
    artifact_sha256: str | None = None
    build_result: dict[str, Any] | None = None
    deploy_results: list[dict[str, Any]] = []
    reboot_result: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    validation_command_results: list[dict[str, Any]] = []
    validation_results: dict[str, Any] = _evaluate_validation_spec(
        expect_markers=[],
        reject_markers=[],
        expected_version=None,
        observation=None,
        command_results=[],
    )

    try:
        if args.build_command:
            state.transition_task(TaskState.RUNNING_CHECKS, "Running host build command.")
            collector.update_summary({"state": state.to_dict()})
            build_result = _run_host_build_command(args.build_command, timeout=max(args.timeout, 1.0))
            collector.write_text_artifact(
                "logs/deploy-verify-build-stdout.log",
                "\n".join(build_result["stdout"]) + ("\n" if build_result["stdout"] else ""),
            )
            collector.write_text_artifact(
                "logs/deploy-verify-build-stderr.log",
                "\n".join(build_result["stderr"]) + ("\n" if build_result["stderr"] else ""),
            )
            collector.append_event(
                event_type="deploy_verify_build",
                source="host",
                summary="Executed host build command",
                payload=build_result,
                severity="error" if build_result["exit_code"] != 0 else "info",
            )
            if build_result["exit_code"] != 0:
                raise RuntimeError("Host build command failed.")

        if artifact_path is not None:
            if not artifact_path.is_file():
                raise FileNotFoundError(f"Artifact does not exist: {artifact_path}")
            artifact_sha256 = _hash_file_sha256(artifact_path)
            collector.write_text_artifact(
                "deploy/artifact.json",
                json.dumps(
                    {
                        "path": str(artifact_path),
                        "size_bytes": artifact_path.stat().st_size,
                        "sha256": artifact_sha256,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )

        intervention_context = _build_intervention_context(
            args,
            artifact=artifact_path,
            artifact_sha256=artifact_sha256,
            build_result=build_result,
        )

        if artifact_path is not None and not args.post_pull_command and not args.reboot_command:
            collector.update_summary(
                {
                    "status": "deploy_verify_manual_required",
                    "build_result": build_result,
                    "deploy_results": deploy_results,
                    "reboot_result": reboot_result,
                    "post_observation": observation,
                    "validation_results": validation_results,
                    "intervention_context": intervention_context,
                    "state": state.to_dict(),
                    "result": build_result_contract(
                        action="deploy-verify",
                        decision="manual_required",
                        failure_stage="manual_upgrade",
                        retryable=True,
                        stop_reason="Artifact is available but no device-side deploy/restart command was provided.",
                        key_excerpts=[
                            build_excerpt(
                                source="workflow_runner",
                                label="manual_upgrade_required",
                                text="Provide --post-pull-command and/or --reboot-command so the artifact is applied on the device.",
                                severity="warning",
                            )
                        ],
                        next_actions=_build_deploy_verify_next_actions(failure_stage="manual_upgrade"),
                        loop_context=loop_context,
                        current_session_dir=session.session_paths.root,
                        carry_forward_options=carry_forward_options,
                        intervention_context=intervention_context,
                    ),
                }
            )
            print("[ ●●●○○ ] 3/5 steps")
            print("[ERROR] Manual upgrade command is required")
            print("[TODO] Pass --post-pull-command and/or --reboot-command, then retry deploy-verify.")
            print(f"[TODO] Session directory: {session.session_paths.root}")
            return 1

        controller = DeviceController(profiles.device, profiles.model, profiles.transport)
        if args.post_pull_command or args.reboot_command or args.validation_command:
            state.transition_device(DeviceState.ROOT_SHELL, "Deploy/verify uses an interactive root shell.")
            collector.update_summary({"state": state.to_dict()})
            with open_serial_port(
                profiles.device.serial.port,
                profiles.device.serial.baudrate,
                timeout=0.2,
            ) as serial_port:
                for index, command in enumerate(args.post_pull_command, start=1):
                    result = _execute_structured_command(
                        controller=controller,
                        shell_command=command,
                        timeout=args.timeout,
                        serial_port=serial_port,
                    )
                    _write_command_artifacts(collector=collector, prefix=f"deploy-verify-deploy-{index}", result=result)
                    deploy_results.append(result.to_dict())
                    collector.append_event(
                        event_type="deploy_verify_deploy_command",
                        source="device_controller",
                        summary=f"Executed deploy command #{index}",
                        payload=result.to_dict(),
                        severity="error" if result.exit_code != 0 else "info",
                    )
                    if result.exit_code != 0:
                        raise RuntimeError(f"Deploy command failed: {command}")
                if args.reboot_command:
                    reboot_exec_result = _execute_structured_command(
                        controller=controller,
                        shell_command=args.reboot_command,
                        timeout=args.timeout,
                        serial_port=serial_port,
                    )
                    _write_command_artifacts(collector=collector, prefix="deploy-verify-reboot", result=reboot_exec_result)
                    reboot_result = reboot_exec_result.to_dict()
                    collector.append_event(
                        event_type="deploy_verify_reboot_command",
                        source="device_controller",
                        summary="Executed deploy restart/reboot command",
                        payload=reboot_result,
                        severity="error" if reboot_exec_result.exit_code != 0 else "info",
                    )
                    if reboot_exec_result.exit_code != 0:
                        raise RuntimeError(f"Reboot command failed: {args.reboot_command}")
                validation_command_results = _run_validation_commands(
                    controller=controller,
                    collector=collector,
                    commands=list(args.validation_command),
                    timeout=args.timeout,
                    serial_port=serial_port,
                    prefix="deploy-verify-validation",
                )

        if args.observe_seconds > 0:
            state.transition_task(TaskState.WAITING_BOOT, f"Observing serial for {args.observe_seconds:.1f}s.")
            collector.update_summary({"state": state.to_dict()})
            observer = SerialObserver(profiles.device.serial, profiles.model)
            captured = observer.capture(
                seconds=args.observe_seconds,
                log_path=session.session_paths.logs_dir / "deploy-verify-serial.log",
            )
            observation = captured.to_dict()
            collector.append_event(
                event_type="deploy_verify_observation",
                source="serial_observer",
                summary=f"Captured {captured.lines_captured} deploy/verify serial lines.",
                payload=observation,
            )

        validation_results = _evaluate_validation_spec(
            expect_markers=list(args.expect_marker),
            reject_markers=list(args.reject_marker),
            expected_version=args.expected_version,
            observation=observation,
            command_results=validation_command_results,
        )
    except LoginRequiredError as exc:
        intervention_context = _build_intervention_context(
            args,
            artifact=artifact_path,
            artifact_sha256=artifact_sha256,
            build_result=build_result,
        )
        collector.update_summary(
            {
                "status": "deploy_verify_blocked",
                "error": str(exc),
                "build_result": build_result,
                "deploy_results": deploy_results,
                "reboot_result": reboot_result,
                "post_observation": observation,
                "validation_results": validation_results,
                "intervention_context": intervention_context,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="deploy-verify",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    next_actions=_build_deploy_verify_next_actions(failure_stage="establish_control"),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ ●●○○○ ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry deploy-verify.")
        return 1
    except Exception as exc:
        intervention_context = _build_intervention_context(
            args,
            artifact=artifact_path,
            artifact_sha256=artifact_sha256,
            build_result=build_result,
        )
        failure_stage = "build" if build_result and build_result.get("exit_code") != 0 else "prepare_artifact" if isinstance(exc, FileNotFoundError) else "deploy"
        collector.update_summary(
            {
                "status": "deploy_verify_failed",
                "error": str(exc),
                "build_result": build_result,
                "deploy_results": deploy_results,
                "reboot_result": reboot_result,
                "post_observation": observation,
                "validation_results": validation_results,
                "intervention_context": intervention_context,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="deploy-verify",
                    decision="continue",
                    failure_stage=failure_stage,
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="workflow_runner", label=failure_stage, text=str(exc), severity="error")],
                    next_actions=_build_deploy_verify_next_actions(failure_stage=failure_stage),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=carry_forward_options,
                    intervention_context=intervention_context,
                ),
            }
        )
        print("[ ●●○○○ ] 2/5 steps")
        print(f"[ERROR] deploy-verify failed: {exc}")
        print("[TODO] Check the latest session summary for the failing stage.")
        return 1

    intervention_context = _build_intervention_context(
        args,
        artifact=artifact_path,
        artifact_sha256=artifact_sha256,
        build_result=build_result,
    )
    validation_missing = not _has_validation_spec(args)
    validation_failed = validation_results.get("verdict") == "fail"
    decision = "manual_required" if validation_missing else "continue" if validation_failed else "success"
    failure_stage = "validation" if validation_failed or validation_missing else None
    state.transition_task(TaskState.COMPLETED if decision == "success" else TaskState.FAILED, "Deploy/verify workflow finished.")
    collector.update_summary(
        {
            "status": "deploy_verify_completed" if decision == "success" else "deploy_verify_manual_required" if validation_missing else "deploy_verify_failed",
            "build_result": build_result,
            "deploy_results": deploy_results,
            "reboot_result": reboot_result,
            "post_observation": observation,
            "validation_results": validation_results,
            "intervention_context": intervention_context,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="deploy-verify",
                decision=decision,
                failure_stage=failure_stage,
                retryable=decision != "success",
                stop_reason=None
                if decision == "success"
                else "No target-specific validation criteria were provided."
                if validation_missing
                else validation_results.get("summary", "Target validation failed."),
                key_excerpts=_validation_key_excerpts(validation_results)
                if validation_failed
                else [
                    build_excerpt(
                        source="workflow_runner",
                        label="validation_required",
                        text="Pass --validation-command, --expect-marker, --reject-marker, or --expected-version to confirm the fix.",
                        severity="warning",
                    )
                ]
                if validation_missing
                else [],
                next_actions=[] if decision == "success" else _build_deploy_verify_next_actions(failure_stage=failure_stage),
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=carry_forward_options,
                intervention_context=intervention_context,
            ),
        }
    )
    print("[ ●●●●○ ] 4/5 steps")
    print(f"[DONE] Deploy commands: {len(deploy_results)}")
    print(f"[DONE] Validation commands: {len(validation_command_results)}")
    if decision == "success":
        print("[DONE] Deploy/verify completed successfully")
    elif validation_missing:
        print("[ERROR] Target validation criteria missing")
    else:
        print("[ERROR] Target validation failed")
    print(f"[TODO] Session directory: {session.session_paths.root}")
    return 0 if decision == "success" else 1


def _command_health(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_health",
    )

    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, "Preparing device health probes.")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_health",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="health",
        loop_context=loop_context,
        plan_details={
            "control": {
                "mode": "health",
                "timeout": args.timeout,
                "checks": [name for name, _ in _build_health_checks(include_sd_write_probe=not args.skip_sd_write_probe)],
            }
        },
    )

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    checks = _build_health_checks(include_sd_write_probe=not args.skip_sd_write_probe)
    check_results: list[dict[str, Any]] = []
    collector.update_summary({"state": state.to_dict()})

    try:
        state.transition_task(TaskState.RUNNING_CHECKS, "Running device health probes.")
        state.transition_device(DeviceState.ROOT_SHELL, "Health probes require an interactive root shell.")
        collector.update_summary({"state": state.to_dict()})
        with open_serial_port(
            profiles.device.serial.port,
            profiles.device.serial.baudrate,
            timeout=0.2,
        ) as serial_port:
            for name, shell_command in checks:
                check_results.append(
                    _run_baseline_check(
                        name=name,
                        shell_command=shell_command,
                        controller=controller,
                        collector=collector,
                        timeout=args.timeout,
                        serial_port=serial_port,
                    )
                )
    except LoginRequiredError as exc:
        collector.append_event(
            event_type="health_blocked",
            source="workflow_runner",
            summary=str(exc),
            severity="warning",
        )
        collector.update_summary(
            {
                "status": "health_blocked",
                "error": str(exc),
                "health_checks": check_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="health",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    next_actions=_build_health_next_actions(evaluation={}),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["timeout", "skip_sd_write_probe"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry the health command.")
        return 1
    except Exception as exc:
        state.transition_task(TaskState.FAILED, "Health probes failed.")
        collector.append_event(
            event_type="health_failed",
            source="workflow_runner",
            summary=f"Health probes failed: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "health_failed",
                "error": str(exc),
                "health_checks": check_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="health",
                    decision="continue",
                    failure_stage="running_checks",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="workflow_runner", label="health_failed", text=str(exc), severity="error")],
                    next_actions=_build_health_next_actions(evaluation={}),
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["timeout", "skip_sd_write_probe"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Health probes failed: {exc}")
        print("[TODO] Check the latest session logs for the failing command.")
        return 1

    evaluation = evaluate_health_checks(check_results, app_name=profiles.model.app_name)
    health_verdict = str(evaluation.get("verdict", ""))
    state.transition_task(TaskState.FAILED if health_verdict == "fail" else TaskState.COMPLETED, "Device health probes completed.")
    collector.update_summary(
        {
            "status": "health_completed",
            "health_checks": check_results,
            "evaluation": evaluation,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="health",
                decision="success" if evaluation.get("verdict") == "pass" else "continue",
                failure_stage=None if evaluation.get("verdict") == "pass" else "validation",
                retryable=False if evaluation.get("verdict") == "pass" else True,
                stop_reason=None if evaluation.get("verdict") == "pass" else evaluation.get("summary"),
                key_excerpts=[
                    build_excerpt(
                        source="evaluation",
                        label=str(finding.get("check_name", "finding")),
                        text=str(finding.get("message", "")),
                        severity=str(finding.get("level", "info")),
                    )
                    for finding in evaluation.get("findings", [])[:3]
                ],
                next_actions=[] if evaluation.get("verdict") == "pass" else _build_health_next_actions(evaluation=evaluation),
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(args, ["timeout", "skip_sd_write_probe"]),
            ),
        }
    )
    summary = json.loads((session.session_paths.root / "summary.json").read_text(encoding="utf-8"))
    _print_health_report(summary)
    print("[DONE] Health status: health_completed")
    return 0


def _command_observe(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_observe",
    )

    state = StateSnapshot()
    state.transition_task(TaskState.WAITING_BOOT, f"Observing serial for {args.seconds:.1f}s.")
    observer = SerialObserver(profiles.device.serial, profiles.model)
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=workflow_name,
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="observe",
        loop_context=loop_context,
        plan_details={"serial": {"mode": "observe", "seconds": args.seconds}},
    )

    try:
        observe_seconds = 0.0 if args.follow else args.seconds
        line_callback = (
            _build_live_serial_printer(markers_only=args.markers_only, focus_terms=args.focus)
            if args.live
            else None
        )
        if args.live:
            live_mode = "until Ctrl+C" if args.follow else f"for {observe_seconds:.1f}s"
            print("[ o.... ] 1/5 steps")
            print(f"[ACTIVE] Live serial view started on {profiles.device.serial.port} {live_mode}")
            print(f"[TODO] Session log: {session.session_paths.logs_dir / 'serial.log'}")
            print("[TODO] Live view only shows new serial output after this connection opens")
            if args.markers_only:
                print("[TODO] Only marker lines will be printed")
            elif args.focus:
                print(f"[TODO] Focus filters: {', '.join(args.focus)}")
            if args.poke_newline:
                print("[TODO] One newline will be sent after connect to wake the prompt")
        result = observer.capture(
            seconds=observe_seconds,
            log_path=session.session_paths.logs_dir / "serial.log",
            line_callback=line_callback,
            startup_lines=[""] if args.poke_newline else None,
        )
    except Exception as exc:
        collector.append_event(
            event_type="serial_observation_failed",
            source="serial_observer",
            summary=f"Serial observation failed: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "observe_failed",
                "error": str(exc),
                "result": build_result_contract(
                    action="observe",
                    decision="continue",
                    failure_stage="observe_serial",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="serial_observer", label="observe_error", text=str(exc), severity="error")],
                    next_actions=[{"action": "observe", "reason": "Retry after restoring serial connectivity or releasing the COM port."}],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(
                        args,
                        ["seconds", "live", "follow", "markers_only", "focus", "poke_newline"],
                    ),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Serial observe failed: {exc}")
        print("[TODO] Close any program that is already using this COM port, then retry.")
        return 1
    collector.append_event(
        event_type="serial_observation",
        source="serial_observer",
        summary=f"Captured {result.lines_captured} serial lines.",
        payload=result.to_dict(),
    )
    collector.update_summary(
        {
            "status": "observed_interrupted" if result.interrupted else "observed",
            "observation": result.to_dict(),
            "result": build_result_contract(
                action="observe",
                decision="success" if result.lines_captured > 0 or result.interrupted else "continue",
                failure_stage=None if result.lines_captured > 0 or result.interrupted else "observe_serial",
                retryable=False if result.lines_captured > 0 or result.interrupted else True,
                stop_reason=None if result.lines_captured > 0 or result.interrupted else "No serial lines were captured during the observation window.",
                key_excerpts=[
                    build_excerpt(source="serial_observer", label="last_line", text=line)
                    for line in result.last_lines[:3]
                ],
                next_actions=[] if result.lines_captured > 0 or result.interrupted else [{"action": "observe", "reason": "Extend the observation window or trigger device activity, then retry."}],
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(
                    args,
                    ["seconds", "live", "follow", "markers_only", "focus", "poke_newline"],
                ),
            ),
        }
    )

    print("[ oooo. ] 4/5 steps")
    observed_label = "until interrupted" if args.follow else f"for {args.seconds:.1f}s"
    print(f"[DONE] Observed serial {observed_label}")
    print(f"[DONE] Lines captured: {result.lines_captured}")
    print(f"[ACTIVE] Last device state: {result.last_device_state or 'unknown'}")
    if result.interrupted:
        print("[DONE] Live view stopped by user")
    if args.live and result.lines_captured == 0:
        print("[TODO] No new serial lines arrived in this window. The device may simply be quiet.")
        if not args.poke_newline:
            print("[TODO] If you want immediate feedback, retry with --poke-newline or trigger device activity.")
    print(f"[TODO] Serial log: {session.session_paths.logs_dir / 'serial.log'}")
    return 0


def _command_exec(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_exec",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, f"Executing serial command: {args.shell_command}")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=workflow_name,
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="exec",
        loop_context=loop_context,
        plan_details={"control": {"mode": "exec", "command": args.shell_command, "timeout": args.timeout}},
    )

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    try:
        result = controller.execute(args.shell_command, timeout=args.timeout)
    except LoginRequiredError as exc:
        collector.append_event(
            event_type="serial_exec_blocked",
            source="device_controller",
            summary=str(exc),
            severity="warning",
        )
        collector.update_summary(
            {
                "status": "exec_blocked",
                "error": str(exc),
                "result": build_result_contract(
                    action="exec",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["shell_command", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry the exec command.")
        return 1
    except Exception as exc:
        collector.append_event(
            event_type="serial_exec_failed",
            source="device_controller",
            summary=f"Serial exec failed: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "exec_failed",
                "error": str(exc),
                "result": build_result_contract(
                    action="exec",
                    decision="continue",
                    failure_stage="execute_command",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="exec_error", text=str(exc), severity="error")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["shell_command", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Serial exec failed: {exc}")
        print("[TODO] Check whether the COM port is occupied or the device is not ready.")
        return 1
    transcript = "\n".join(result.transcript) + ("\n" if result.transcript else "")
    output = "\n".join(result.output_lines) + ("\n" if result.output_lines else "")
    collector.write_text_artifact("logs/exec-transcript.log", transcript)
    collector.write_text_artifact("logs/exec-output.log", output)
    collector.append_event(
        event_type="serial_exec",
        source="device_controller",
        summary=f"Executed command: {args.shell_command}",
        payload=result.to_dict(),
    )
    collector.update_summary(
        {
            "status": "executed",
            "command_result": result.to_dict(),
            "result": build_result_contract(
                action="exec",
                decision="success" if result.exit_code == 0 else "continue",
                failure_stage=None if result.exit_code == 0 else "execute_command",
                retryable=False if result.exit_code == 0 else True,
                stop_reason=None if result.exit_code == 0 else f"Command exited with {result.exit_code}.",
                key_excerpts=[
                    build_excerpt(
                        source="command_output",
                        label="output_excerpt",
                        text=_format_output_excerpt(result.output_lines),
                        severity="error" if result.exit_code != 0 else "info",
                    )
                ],
                next_actions=[] if result.exit_code == 0 else [{"action": "exec", "reason": "Retry after adjusting the shell command or device state."}],
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(args, ["shell_command", "timeout"]),
            ),
        }
    )

    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Command executed: {args.shell_command}")
    print(f"[DONE] Exit code: {result.exit_code}")
    print(f"[ACTIVE] Output lines: {len(result.output_lines)}")
    print(f"[TODO] Transcript: {session.session_paths.logs_dir / 'exec-transcript.log'}")
    return 0


def _command_fetch_file(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_fetch_file",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, f"Fetching device file: {args.remote_path}")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_fetch_file",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="fetch-file",
        loop_context=loop_context,
        plan_details={
            "control": {
                "mode": "fetch_file",
                "remote_path": args.remote_path,
                "output": str(args.output) if args.output else None,
                "timeout": args.timeout,
            }
        },
    )

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    fetch_command = _build_fetch_file_command(args.remote_path)
    try:
        result = controller.execute(fetch_command, timeout=args.timeout)
    except LoginRequiredError as exc:
        collector.append_event(
            event_type="fetch_file_blocked",
            source="device_controller",
            summary=str(exc),
            severity="warning",
        )
        collector.update_summary(
            {
                "status": "fetch_file_blocked",
                "error": str(exc),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="fetch-file",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry fetch-file.")
        return 1
    except Exception as exc:
        collector.append_event(
            event_type="fetch_file_failed",
            source="device_controller",
            summary=f"Fetch file failed: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "fetch_file_failed",
                "error": str(exc),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="fetch-file",
                    decision="continue",
                    failure_stage="fetch_file",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="fetch_file_error", text=str(exc), severity="error")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Fetch file failed: {exc}")
        print("[TODO] Check whether the COM port is occupied or the device is not ready.")
        return 1

    fetch_result = _fetch_remote_file_artifact(
        controller=controller,
        collector=collector,
        remote_path=args.remote_path,
        timeout=args.timeout,
        prefix="fetch-file",
        output_path=args.output,
    )
    collector.append_event(
        event_type="fetch_file_command",
        source="device_controller",
        summary=f"Fetched file from {args.remote_path}",
        payload=fetch_result,
    )

    if fetch_result["status"] != "ok":
        state.transition_task(TaskState.FAILED, "fetch-file command returned an invalid result.")
        collector.update_summary(
            {
                "status": "fetch_file_failed",
                "error": fetch_result.get("error", "Unknown fetch-file error"),
                "command_result": fetch_result.get("command_result"),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="fetch-file",
                    decision="continue",
                    failure_stage="fetch_file",
                    retryable=True,
                    stop_reason=fetch_result.get("error", "Unknown fetch-file error"),
                    key_excerpts=[
                        build_excerpt(
                            source="fetch_file",
                            label=str(args.remote_path),
                            text=str(fetch_result.get("error", "Unknown fetch-file error")),
                            severity="error",
                        )
                    ],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {fetch_result.get('error', 'Unknown fetch-file error')}")
        print("[TODO] Inspect the fetch-file output/transcript to see whether the file exists and base64 is available.")
        return 1

    state.transition_task(TaskState.COMPLETED, "Device file fetched successfully.")
    collector.update_summary(
        {
            "status": "fetch_file_completed",
            "command_result": fetch_result.get("command_result"),
            "fetch_result": fetch_result,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="fetch-file",
                decision="success",
                key_excerpts=[
                    build_excerpt(
                        source="fetch_file",
                        label=str(args.remote_path),
                        text=str(fetch_result.get("local_path", "")),
                    )
                ],
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
            ),
        }
    )

    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Remote file: {args.remote_path}")
    print(f"[DONE] Local file: {fetch_result['local_path']}")
    print(f"[DONE] Size: {_format_bytes(fetch_result.get('size_bytes'))}")
    print(f"[ACTIVE] SHA256: {fetch_result['sha256']}")
    print(f"[TODO] Transcript: {session.session_paths.logs_dir / 'fetch-file-transcript.log'}")
    return 0


def _command_fetch_path(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_fetch_path",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, f"Fetching device path: {args.remote_path}")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()
    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_fetch_path",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="fetch-path",
        loop_context=loop_context,
        plan_details={
            "control": {
                "mode": "fetch_path",
                "remote_path": args.remote_path,
                "output": str(args.output) if args.output else None,
                "timeout": args.timeout,
            }
        },
    )

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    try:
        fetch_result = _fetch_remote_path_artifact(
            controller=controller,
            collector=collector,
            remote_path=args.remote_path,
            timeout=args.timeout,
            prefix="fetch-path",
            output_path=args.output,
        )
    except LoginRequiredError as exc:
        collector.append_event(
            event_type="fetch_path_blocked",
            source="device_controller",
            summary=str(exc),
            severity="warning",
        )
        collector.update_summary(
            {
                "status": "fetch_path_blocked",
                "error": str(exc),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="fetch-path",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry fetch-path.")
        return 1
    except Exception as exc:
        collector.append_event(
            event_type="fetch_path_failed",
            source="device_controller",
            summary=f"Fetch path failed: {exc}",
            severity="error",
        )
        collector.update_summary(
            {
                "status": "fetch_path_failed",
                "error": str(exc),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="fetch-path",
                    decision="continue",
                    failure_stage="fetch_path",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="fetch_path_error", text=str(exc), severity="error")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Fetch path failed: {exc}")
        print("[TODO] Check whether the COM port is occupied or the device is not ready.")
        return 1

    collector.append_event(
        event_type="fetch_path_command",
        source="device_controller",
        summary=f"Fetched path from {args.remote_path}",
        payload=fetch_result,
    )

    if fetch_result["status"] != "ok":
        state.transition_task(TaskState.FAILED, "fetch-path command returned an invalid result.")
        collector.update_summary(
            {
                "status": "fetch_path_failed",
                "error": fetch_result.get("error", "Unknown fetch-path error"),
                "command_result": fetch_result.get("command_result"),
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="fetch-path",
                    decision="continue",
                    failure_stage="fetch_path",
                    retryable=True,
                    stop_reason=fetch_result.get("error", "Unknown fetch-path error"),
                    key_excerpts=[
                        build_excerpt(
                            source="fetch_path",
                            label=str(args.remote_path),
                            text=str(fetch_result.get("error", "Unknown fetch-path error")),
                            severity="error",
                        )
                    ],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {fetch_result.get('error', 'Unknown fetch-path error')}")
        print("[TODO] Inspect the fetch-path output/transcript to see whether the path exists and tar/base64 are available.")
        return 1

    state.transition_task(TaskState.COMPLETED, "Device path fetched successfully.")
    collector.update_summary(
        {
            "status": "fetch_path_completed",
            "command_result": fetch_result.get("command_result"),
            "fetch_result": fetch_result,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="fetch-path",
                decision="success",
                key_excerpts=[
                    build_excerpt(
                        source="fetch_path",
                        label=str(args.remote_path),
                        text=str(fetch_result.get("local_path", "")),
                    )
                ],
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(args, ["remote_path", "output", "timeout"]),
            ),
        }
    )

    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Remote path: {args.remote_path}")
    print(f"[DONE] Local artifact: {fetch_result['local_path']}")
    print(f"[DONE] Mode: {fetch_result.get('mode', 'unknown')}")
    print(f"[DONE] Size: {_format_bytes(fetch_result.get('size_bytes'))}")
    print(f"[ACTIVE] SHA256: {fetch_result['sha256']}")
    print(f"[TODO] Transcript: {session.session_paths.logs_dir / 'fetch-path-transcript.log'}")
    return 0


def _command_collect_evidence(args: argparse.Namespace) -> int:
    profiles = _load_profiles_from_args(args)
    session, loop_context = _create_session_with_loop(
        args,
        profiles=profiles,
        task_type=f"{profiles.task.task_type}_collect_evidence",
    )
    state = StateSnapshot()
    state.transition_task(TaskState.ESTABLISHING_CONTROL, "Collecting device evidence bundle.")
    collector = EvidenceCollector(session)
    workflow_name, workflow_steps = WorkflowRunner(profiles).build_plan()

    default_commands = [] if args.skip_defaults else _build_collect_evidence_commands(profiles)
    extra_commands = [(f"user_command_{index}", command) for index, command in enumerate(args.shell_command, start=1)]
    command_items = default_commands + extra_commands

    default_files = [] if args.skip_defaults else _default_collect_evidence_files(profiles)
    remote_files = list(dict.fromkeys([*default_files, *args.remote_file]))

    collector.bootstrap(
        profiles=profiles,
        state_snapshot=state,
        workflow_name=f"{workflow_name}_collect_evidence",
        workflow_steps=WorkflowRunner.as_dicts(workflow_steps),
        action_name="collect-evidence",
        loop_context=loop_context,
        plan_details={
            "control": {
                "mode": "collect_evidence",
                "shell_commands": [{"name": name, "command": command} for name, command in command_items],
                "remote_files": remote_files,
                "timeout": args.timeout,
            }
        },
    )

    controller = DeviceController(profiles.device, profiles.model, profiles.transport)
    command_results: list[dict[str, Any]] = []
    file_results: list[dict[str, Any]] = []

    try:
        state.transition_task(TaskState.RUNNING_CHECKS, "Running evidence commands and file fetches.")
        state.transition_device(DeviceState.ROOT_SHELL, "Evidence collection requires an interactive root shell.")
        collector.update_summary({"state": state.to_dict()})
        with open_serial_port(
            profiles.device.serial.port,
            profiles.device.serial.baudrate,
            timeout=0.2,
        ) as serial_port:
            command_results, file_results = _collect_evidence_bundle(
                profiles=profiles,
                controller=controller,
                collector=collector,
                timeout=args.timeout,
                serial_port=serial_port,
                command_items=command_items,
                remote_files=remote_files,
                command_prefix="collect-evidence",
                file_prefix="collect-evidence-file",
            )
    except LoginRequiredError as exc:
        collector.update_summary(
            {
                "status": "collect_evidence_blocked",
                "error": str(exc),
                "command_results": command_results,
                "file_results": file_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="collect-evidence",
                    decision="blocked",
                    failure_stage="establish_control",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="device_controller", label="login_required", text=str(exc), severity="warning")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["shell_command", "remote_file", "skip_defaults", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        print("[TODO] Export AUTO_DBG_DEVICE_PASSWORD in this shell, then retry collect-evidence.")
        return 1
    except Exception as exc:
        state.transition_task(TaskState.FAILED, "Evidence collection failed.")
        collector.update_summary(
            {
                "status": "collect_evidence_failed",
                "error": str(exc),
                "command_results": command_results,
                "file_results": file_results,
                "state": state.to_dict(),
                "result": build_result_contract(
                    action="collect-evidence",
                    decision="continue",
                    failure_stage="collect_evidence",
                    retryable=True,
                    stop_reason=str(exc),
                    key_excerpts=[build_excerpt(source="workflow_runner", label="collect_evidence_failed", text=str(exc), severity="error")],
                    loop_context=loop_context,
                    current_session_dir=session.session_paths.root,
                    carry_forward_options=_collect_option_patch(args, ["shell_command", "remote_file", "skip_defaults", "timeout"]),
                ),
            }
        )
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] collect-evidence failed: {exc}")
        print("[TODO] Inspect the latest session logs for the failing command or file fetch.")
        return 1

    command_failures = [item for item in command_results if item.get("exit_code") != 0]
    file_failures = [item for item in file_results if item.get("status") != "ok"]
    state.transition_task(
        TaskState.COMPLETED if not command_failures and not file_failures else TaskState.FAILED,
        "Evidence collection finished.",
    )
    collector.update_summary(
        {
            "status": "collect_evidence_completed" if not command_failures and not file_failures else "collect_evidence_partial",
            "command_results": command_results,
            "file_results": file_results,
            "state": state.to_dict(),
            "result": build_result_contract(
                action="collect-evidence",
                decision="success" if not command_failures and not file_failures else "continue",
                failure_stage=None if not command_failures and not file_failures else "collect_evidence",
                retryable=False if not command_failures and not file_failures else True,
                stop_reason=None
                if not command_failures and not file_failures
                else f"{len(command_failures)} command failure(s), {len(file_failures)} file failure(s).",
                key_excerpts=[
                    *[
                        build_excerpt(
                            source="evidence_command",
                            label=str(item.get("name", "command_failure")),
                            text=_format_output_excerpt(item.get("output_lines", [])),
                            severity="error",
                        )
                        for item in command_failures[:2]
                    ],
                    *[
                        build_excerpt(
                            source="evidence_file",
                            label=str(item.get("remote_path", "file_failure")),
                            text=str(item.get("error") or item.get("status") or "file fetch failed"),
                            severity="error",
                        )
                        for item in file_failures[:2]
                    ],
                ],
                next_actions=[]
                if not command_failures and not file_failures
                else [{"action": "collect-evidence", "reason": "Retry after addressing the failed command or file fetch."}],
                loop_context=loop_context,
                current_session_dir=session.session_paths.root,
                carry_forward_options=_collect_option_patch(args, ["shell_command", "remote_file", "skip_defaults", "timeout"]),
            ),
        }
    )

    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Evidence commands: {len(command_results)}")
    print(f"[DONE] Remote files fetched: {sum(1 for item in file_results if item.get('status') == 'ok')}/{len(file_results)}")
    if command_failures:
        print(f"[ERROR] Command failures: {len(command_failures)}")
    if file_failures:
        print(f"[ERROR] File fetch failures: {len(file_failures)}")
    print(f"[ACTIVE] Session directory: {session.session_paths.root}")
    return 0 if not command_failures and not file_failures else 1


def _command_record_intervention(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir).absolute()
    metadata: dict[str, Any] = {}
    if args.metadata_json:
        try:
            raw_metadata = json.loads(args.metadata_json)
        except json.JSONDecodeError as exc:
            print("[ oxx.. ] 2/5 steps")
            print(f"[ERROR] metadata-json is not valid JSON: {exc}")
            print("[TODO] Pass a JSON object string such as {\"change\": \"retry with new wifi config\"}.")
            return 1
        if not isinstance(raw_metadata, dict):
            print("[ oxx.. ] 2/5 steps")
            print("[ERROR] metadata-json must decode to a JSON object.")
            print("[TODO] Wrap structured metadata in {...} before retrying.")
            return 1
        metadata = raw_metadata

    try:
        summary = _load_session_summary(session_dir)
        session = _restore_session_context(session_dir, summary)
        collector = EvidenceCollector(session)
        related_session = args.related_session
        if related_session:
            related_path = Path(related_session)
            if related_path.is_absolute() or len(related_path.parts) > 1:
                related_session = str(related_path.absolute())
        record = collector.append_intervention(
            kind=args.kind,
            summary=args.summary,
            details=args.details,
            files=[str(Path(item).absolute()) for item in args.file],
            git_commit=args.git_commit,
            expected_effect=args.expected_effect,
            related_session=related_session,
            metadata=metadata,
        )
        refreshed_summary = _load_session_summary(session_dir)
        interventions = refreshed_summary.get("interventions", {})
    except Exception as exc:
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] Failed to record intervention: {exc}")
        print("[TODO] Confirm the session directory contains a valid summary.json, then retry.")
        return 1

    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Intervention recorded: {record['kind']}")
    print(f"[DONE] Summary: {record['summary']}")
    print(f"[DONE] Total interventions: {interventions.get('count', 0)}")
    print(f"[ACTIVE] Session directory: {session_dir}")
    return 0


def _command_summary(args: argparse.Namespace) -> int:
    summary_path = args.session_dir / "summary.json"
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _command_report(args: argparse.Namespace) -> int:
    session_dir = args.session_dir
    if getattr(args, "latest", False):
        session_dir = SessionManager(args.artifacts_root).latest_session_dir()
    report_path = session_dir / "report.md"
    print(report_path.read_text(encoding="utf-8"))
    return 0


def _command_agent_call(args: argparse.Namespace) -> int:
    try:
        request = load_agent_request(args.request)
        response = execute_agent_request(
            request,
            project_root=_project_root(),
            parser_builder=_build_parser,
            dispatcher=_dispatch_command,
        )
    except (AgentCallError, ProfileResolutionError, json.JSONDecodeError) as exc:
        response = build_agent_error_response(project_root=_project_root(), error=exc)

    if args.pretty:
        print(json.dumps(response, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(response, ensure_ascii=False))
    return 0


def _command_describe_agent_tool(args: argparse.Namespace) -> int:
    project_root = _project_root()
    if args.format == "markdown":
        print(render_agent_tool_markdown(project_root=project_root))
        return 0
    manifest = build_agent_tool_manifest(project_root=project_root)
    if args.pretty:
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(manifest, ensure_ascii=False))
    return 0


def _command_install_home_plugin(args: argparse.Namespace) -> int:
    result = install_home_plugin(
        project_root=args.project_root,
        home_root=args.home_root,
        plugin_name=args.plugin_name,
    )
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Home plugin installed: {result['plugin_dir']}")
    print(f"[DONE] Plugin manifest: {result['plugin_manifest']}")
    print(f"[DONE] MCP config: {result['mcp_config']}")
    print(f"[DONE] Marketplace: {result['marketplace']}")
    print(f"[ACTIVE] Default AUTO_DBG_PROJECT_ROOT: {Path(args.project_root).absolute()}")
    return 0


def _command_install_local_tool(args: argparse.Namespace) -> int:
    result = install_local_tool(
        project_root=args.project_root,
        install_root=args.install_root,
        include_venv=not args.skip_venv,
        include_local_settings=not args.skip_local_settings,
    )
    print("[ oooo. ] 4/5 steps")
    print(f"[DONE] Local tool installed: {result['install_root']}")
    print(f"[DONE] Command bin: {result['bin_dir']}")
    print(f"[DONE] Wrapper: {result['autodbg_cmd']}")
    print(f"[DONE] Observe wrapper: {result['observe_cmd']}")
    return 0


def _command_show_mvp() -> int:
    doc_path = _project_root() / "docs" / "mvp-workflow.md"
    print("[ oo... ] 2/5 steps")
    print(f"[DONE] MVP doc: {doc_path}")
    print("[DONE] Primary entry: python -m autodbg run --serial-port COM19")
    return 0


def _command_ports() -> int:
    try:
        ports = list_serial_ports()
    except SerialSupportError as exc:
        print("[ o.... ] 1/5 steps")
        print(f"[ERROR] {exc}")
        return 1

    print("[ oo... ] 2/5 steps")
    if not ports:
        print("[DONE] No serial ports detected")
        return 0

    for port in ports:
        print(f"[DONE] {port['device']} - {port['description']}")
        print(f"       {port['hwid']}")
    return 0


def _dispatch_command(args: argparse.Namespace) -> int:
    if args.command == "run":
        return _command_run(args)
    if args.command == "stage-sd":
        return _command_stage_sd(args)
    if args.command == "storage":
        return _command_storage(args)
    if args.command == "bootstrap-network":
        return _command_bootstrap_network(args)
    if args.command == "serve-artifacts":
        return _command_serve_artifacts(args)
    if args.command == "artifact-server":
        return _command_artifact_server(args)
    if args.command == "quickstart":
        return _command_quickstart(args)
    if args.command == "device-pull":
        return _command_device_pull(args)
    if args.command == "build-sd-http-helper":
        return _command_build_sd_http_helper(args)
    if args.command == "deploy-verify":
        return _command_deploy_verify(args)
    if args.command == "observe":
        return _command_observe(args)
    if args.command == "exec":
        return _command_exec(args)
    if args.command == "fetch-file":
        return _command_fetch_file(args)
    if args.command == "fetch-path":
        return _command_fetch_path(args)
    if args.command == "collect-evidence":
        return _command_collect_evidence(args)
    if args.command == "health":
        return _command_health(args)
    if args.command == "record-intervention":
        return _command_record_intervention(args)
    if args.command == "resume":
        return _command_summary(args)
    if args.command == "summary":
        return _command_summary(args)
    if args.command == "report":
        return _command_report(args)
    if args.command == "watch-serial":
        return _command_watch_serial(args)
    if args.command == "serial-broker":
        return _command_serial_broker(args)
    if args.command == "agent-call":
        return _command_agent_call(args)
    if args.command == "describe-agent-tool":
        return _command_describe_agent_tool(args)
    if args.command == "install-home-plugin":
        return _command_install_home_plugin(args)
    if args.command == "install-local-tool":
        return _command_install_local_tool(args)
    if args.command == "show-mvp":
        return _command_show_mvp()
    if args.command == "ports":
        return _command_ports()
    raise ValueError(f"Unsupported command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        return _dispatch_command(args)
    except ProfileResolutionError as exc:
        print("[ oxx.. ] 2/5 steps")
        print(f"[ERROR] {exc}")
        defaults_path = getattr(args, "profiles_defaults", None)
        if defaults_path is not None:
            print(
                "[TODO] Provide --device/--model/--task/--transport explicitly, "
                f"or fix the defaults manifest: {Path(defaults_path).resolve()}"
            )
        return 1
    except ValueError as exc:
        parser.error(str(exc))
        return 2
