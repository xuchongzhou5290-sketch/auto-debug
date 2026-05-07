from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import socket
from typing import Any
from urllib.parse import urlparse

from autodbg.control.controller import CommandResult, DeviceController
from autodbg.evidence.collector import EvidenceCollector
from autodbg.host.bundle import split_base64_payload


_FETCH_META_PREFIX = "__AUTODBG_META__"
_FETCH_B64_PREFIX = "__AUTODBG_B64__"
_STRUCTURED_OUTPUT_PREFIX = "__AUTODBG_CMD__"
_DEFAULT_PREFERRED_INTERFACES = ("eth0", "wlan0", "usb0", "wlan1", "ra0", "apcli0")


def _cli_main_module():
    from autodbg.cli import main as cli_main

    return cli_main


def _resolve_bootstrap_commands(
    profiles,
    mode: str,
    override_commands: list[str],
    *,
    wifi_ssid: str | None = None,
    wifi_password: str | None = None,
    wifi_mode: str | None = None,
    network_dir: str | None = None,
) -> list[str]:
    if override_commands:
        return override_commands
    network = profiles.device.network
    if network is None:
        return []
    if mode == "wlan_script":
        if network.bootstrap_commands:
            return list(network.bootstrap_commands)
        wifi_settings = _resolve_wifi_settings(
            profiles,
            wifi_ssid=wifi_ssid,
            wifi_password=wifi_password,
            wifi_mode=wifi_mode,
            network_dir=network_dir,
        )
        return _build_auto_wlan_bootstrap_commands(**wifi_settings)
    return []


def _resolve_preferred_interfaces(profiles) -> list[str]:
    network = profiles.device.network
    if network is not None and network.preferred_interfaces:
        return list(dict.fromkeys(network.preferred_interfaces))
    return list(_DEFAULT_PREFERRED_INTERFACES)


def _resolve_host_ip(profiles, *, base_url: str | None = None) -> str | None:
    network = profiles.device.network
    if network is not None and network.host_ip:
        return network.host_ip
    return _host_from_base_url(base_url) or _cli_main_module().detect_host_ipv4()


def _build_existing_network_check(profiles) -> str:
    interface_tokens = " ".join(_sh_single_quote(item) for item in _resolve_preferred_interfaces(profiles))
    return (
        "AUTODBG_FOUND_IFACE=''; "
        f"for AUTODBG_IFACE in {interface_tokens}; do "
        "if ifconfig \"$AUTODBG_IFACE\" >/dev/null 2>&1; then "
        "if ifconfig \"$AUTODBG_IFACE\" | grep -E 'inet addr|inet ' >/dev/null 2>&1; then "
        "AUTODBG_FOUND_IFACE=\"$AUTODBG_IFACE\"; "
        "break; "
        "fi; "
        "fi; "
        "done; "
        "if [ -n \"$AUTODBG_FOUND_IFACE\" ]; then "
        "printf 'AUTODBG_ACTIVE_IFACE=%s\\n' \"$AUTODBG_FOUND_IFACE\"; "
        "ifconfig \"$AUTODBG_FOUND_IFACE\"; "
        "else "
        "echo 'AUTODBG_NO_ACTIVE_NETWORK' >&2; "
        "false; "
        "fi"
    )


def _resolve_connectivity_checks(
    profiles,
    override_commands: list[str],
    *,
    mode: str = "lan_ready",
    base_url: str | None = None,
) -> list[str]:
    if override_commands:
        return override_commands
    network = profiles.device.network
    if network is not None and network.connectivity_checks:
        return list(network.connectivity_checks)
    if mode == "offline":
        return []
    commands = [_build_existing_network_check(profiles)]
    host_ip = _resolve_host_ip(profiles, base_url=base_url)
    if host_ip:
        commands.append(f"ping -c 1 {_sh_single_quote(host_ip)}")
    elif network.expected_ip:
        commands.append(f"ifconfig | grep {_sh_single_quote(network.expected_ip)}")
    return commands


def _resolve_pull_base_url(profiles, override_base_url: str | None, *, bind: str, port: int) -> str:
    if override_base_url:
        return override_base_url.rstrip("/")
    network = profiles.device.network
    if network is not None and network.pull_base_url:
        return network.pull_base_url.rstrip("/")
    return _default_base_url(bind, port, host_ip=_resolve_host_ip(profiles))


def _resolve_wifi_settings(
    profiles,
    *,
    wifi_ssid: str | None,
    wifi_password: str | None,
    wifi_mode: str | None,
    network_dir: str | None,
) -> dict[str, str | None]:
    network = profiles.device.network
    return {
        "wifi_ssid": wifi_ssid or (network.wifi_ssid if network is not None else None),
        "wifi_password": wifi_password or (network.wifi_password if network is not None else None),
        "wifi_mode": wifi_mode or (network.wifi_mode if network is not None else "WPA2"),
        "network_dir": network_dir or (network.network_dir if network is not None else None),
    }


def _build_auto_wlan_bootstrap_commands(
    *,
    wifi_ssid: str | None,
    wifi_password: str | None,
    wifi_mode: str | None,
    network_dir: str | None,
) -> list[str]:
    if not wifi_ssid or wifi_password is None:
        return []
    net_dir_expr = _build_network_dir_resolver(network_dir)
    quoted_ssid = _sh_single_quote(wifi_ssid)
    quoted_password = _sh_single_quote(wifi_password)
    quoted_mode = _sh_single_quote(wifi_mode or "WPA2")
    return [
        f"{net_dir_expr} && sh \"$NET_DIR/wlan_run.sh\" start",
        f"{net_dir_expr} && sh \"$NET_DIR/wifi_cmd.sh\" connect {quoted_ssid} {quoted_password} {quoted_mode}",
    ]


def _resolve_pull_workspace(profiles, override_workspace: str | None) -> str:
    if override_workspace:
        return override_workspace
    network = profiles.device.network
    if network is not None and network.pull_workspace:
        return network.pull_workspace
    if any(path.startswith("/mnt/sdcard") for path in profiles.model.artifact_paths):
        return "/mnt/sdcard/autodbg"
    return profiles.model.debug_workspace


def _default_base_url(bind: str, port: int, *, host_ip: str | None = None) -> str:
    host = bind
    if bind == "0.0.0.0":
        host = host_ip or _cli_main_module().detect_host_ipv4() or socket.gethostbyname(socket.gethostname())
    return f"http://{host}:{port}"


def _artifact_health_url(base_url: str, health_name: str) -> str:
    return f"{base_url.rstrip('/')}/{health_name.lstrip('/')}"


def _local_artifact_health_url(bind: str, port: int, health_name: str) -> str:
    host = bind
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/{health_name.lstrip('/')}"


def _host_from_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url)
    return parsed.hostname


def _build_network_dir_resolver(network_dir: str | None) -> str:
    if network_dir:
        return f"NET_DIR={_sh_single_quote(network_dir)}"
    candidates = [
        "/opt/network",
        "/mnt/sdcard/network",
        "/mnt/sdcard/autodbg/network",
        "/tmp/debug/network",
        "/opt/lecam/network",
        "/usr/local/wifi",
        "/system/network",
    ]
    lines = ["NET_DIR=''"]
    for candidate in candidates:
        lines.append(
            f'if [ -z "$NET_DIR" ] && [ -f "{candidate}/wifi_cmd.sh" ] && [ -f "{candidate}/wlan_run.sh" ]; then NET_DIR="{candidate}"; fi'
        )
    lines.append('if [ -z "$NET_DIR" ]; then NET_FILE=$(find / -path "*/network/wifi_cmd.sh" 2>/dev/null | head -n 1); [ -n "$NET_FILE" ] && NET_DIR=$(dirname "$NET_FILE"); fi')
    lines.append('[ -n "$NET_DIR" ]')
    return "; ".join(lines)


def _normalize_focus_terms(focus_terms: list[str]) -> list[str]:
    return [term.strip().lower() for term in focus_terms if term.strip()]


def _line_matches_focus_terms(line: str, focus_terms: list[str]) -> bool:
    if not focus_terms:
        return True
    lower_line = line.lower()
    return any(term in lower_line for term in focus_terms)


def _build_remote_pull_command(base_url: str, *, workspace: str, script_name: str) -> str:
    quoted_workspace = _sh_single_quote(workspace)
    quoted_script_name = _sh_single_quote(script_name)
    quoted_script_url = _sh_single_quote(f"{base_url.rstrip('/')}/{script_name}")
    return (
        f"mkdir -p {quoted_workspace} && "
        f"cd {quoted_workspace} && "
        "if command -v curl >/dev/null 2>&1; then "
        f"curl -fsSL {quoted_script_url} -o {quoted_script_name}; "
        "elif command -v wget >/dev/null 2>&1 && wget --help >/dev/null 2>&1; then "
        f"wget -q -O {quoted_script_name} {quoted_script_url}; "
        "elif command -v busybox >/dev/null 2>&1 && busybox --list 2>/dev/null | grep -qx 'wget'; then "
        f"busybox wget -q -O {quoted_script_name} {quoted_script_url}; "
        "else "
        "echo 'No downloader available (curl/wget/busybox wget).' >&2; "
        "exit 127; "
        "fi && "
        f"WORKSPACE={quoted_workspace} sh {quoted_script_name}"
    )


def _build_sd_http_helper_command(base_url: str, *, workspace: str, helper_path: str, list_name: str) -> str:
    return (
        f"chmod +x {_sh_single_quote(helper_path)} 2>/dev/null || true; "
        f"{_sh_single_quote(helper_path)} "
        f"{_sh_single_quote(base_url.rstrip('/'))} "
        f"{_sh_single_quote(workspace)} "
        f"{_sh_single_quote(list_name)}"
    )


def _build_transfer_probe_command(sd_http_helper_path: str | None = None) -> str:
    helper_probe = "printf 'AUTODBG_SD_HTTP_HELPER=not_configured\\n'"
    if sd_http_helper_path:
        helper_probe = (
            f"printf 'AUTODBG_SD_HTTP_HELPER='; "
            f"if [ -f {_sh_single_quote(sd_http_helper_path)} ]; then printf 'yes\\n'; else printf 'no\\n'; fi"
        )
    return (
        "printf 'AUTODBG_DOWNLOADER='; "
        "if command -v curl >/dev/null 2>&1; then printf 'curl\\n'; "
        "elif command -v wget >/dev/null 2>&1 && wget --help >/dev/null 2>&1; then printf 'wget\\n'; "
        "elif command -v busybox >/dev/null 2>&1 && busybox --list 2>/dev/null | grep -qx 'wget'; then printf 'busybox_wget\\n'; "
        "else printf 'none\\n'; fi; "
        "printf 'AUTODBG_BASE64='; "
        "if command -v base64 >/dev/null 2>&1; then printf 'yes\\n'; else printf 'no\\n'; fi; "
        "printf 'AUTODBG_TAR='; "
        "if command -v tar >/dev/null 2>&1; then printf 'yes\\n'; else printf 'no\\n'; fi; "
        f"{helper_probe}"
    )


def _build_fetch_file_command(remote_path: str) -> str:
    quoted_remote_path = _sh_single_quote(remote_path)
    mode_meta = _sh_single_quote(_FETCH_META_PREFIX + "MODE=file\n")
    source_meta = _sh_single_quote(_FETCH_META_PREFIX + "SOURCE=" + remote_path + "\n")
    return (
        f"if [ ! -f {quoted_remote_path} ]; then "
        f"echo 'AUTODBG_FETCH_MISSING {remote_path}' >&2; "
        "exit 2; "
        "fi; "
        f"printf {mode_meta}; "
        f"printf {source_meta}; "
        f"base64 < {quoted_remote_path} | "
        "while IFS= read -r line; do printf '__AUTODBG_B64__%s\\n' \"$line\"; done"
    )


def _build_fetch_path_command(remote_path: str) -> str:
    quoted_remote_path = _sh_single_quote(remote_path)
    file_mode_meta = _sh_single_quote(_FETCH_META_PREFIX + "MODE=file\n")
    tar_mode_meta = _sh_single_quote(_FETCH_META_PREFIX + "MODE=tar\n")
    source_meta = _sh_single_quote(_FETCH_META_PREFIX + "SOURCE=" + remote_path + "\n")
    return (
        f"if [ -f {quoted_remote_path} ]; then "
        f"printf {file_mode_meta}; "
        f"printf {source_meta}; "
        f"base64 < {quoted_remote_path} | while IFS= read -r line; do printf '__AUTODBG_B64__%s\\n' \"$line\"; done; "
        f"elif [ -d {quoted_remote_path} ]; then "
        f"printf {tar_mode_meta}; "
        f"printf {source_meta}; "
        f"tar -cf - {quoted_remote_path} | base64 | while IFS= read -r line; do printf '__AUTODBG_B64__%s\\n' \"$line\"; done; "
        "else "
        f"echo 'AUTODBG_FETCH_MISSING {remote_path}' >&2; "
        "exit 2; "
        "fi"
    )


def _fetch_artifact_relative_path(remote_path: str) -> str:
    parts = [part for part in PurePosixPath(remote_path).parts if part not in {"", "/"}]
    safe_parts = [part.replace(":", "_") for part in parts] or ["fetched-device-file.bin"]
    return str(Path("fetched").joinpath(*safe_parts))


def _parse_fetch_output_lines(output_lines: list[str]) -> tuple[dict[str, str], list[str]]:
    metadata: dict[str, str] = {}
    payload_lines: list[str] = []
    for line in output_lines:
        if line.startswith(_FETCH_META_PREFIX):
            key_value = line[len(_FETCH_META_PREFIX) :]
            key, _, value = key_value.partition("=")
            if key:
                metadata[key.lower()] = value
            continue
        if line.startswith(_FETCH_B64_PREFIX):
            payload_lines.append(line[len(_FETCH_B64_PREFIX) :])
    return metadata, payload_lines


def _fetch_remote_file_artifact(
    *,
    controller: DeviceController,
    collector: EvidenceCollector,
    remote_path: str,
    timeout: float,
    prefix: str,
    serial_port=None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    result = controller.execute(
        _build_fetch_file_command(remote_path),
        timeout=timeout,
        serial_port=serial_port,
    )
    return _finalize_fetch_result(
        collector=collector,
        remote_path=remote_path,
        prefix=prefix,
        result=result,
        output_path=output_path,
    )


def _fetch_remote_path_artifact(
    *,
    controller: DeviceController,
    collector: EvidenceCollector,
    remote_path: str,
    timeout: float,
    prefix: str,
    serial_port=None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    result = controller.execute(
        _build_fetch_path_command(remote_path),
        timeout=timeout,
        serial_port=serial_port,
    )
    return _finalize_fetch_result(
        collector=collector,
        remote_path=remote_path,
        prefix=prefix,
        result=result,
        output_path=output_path,
    )


def _finalize_fetch_result(
    *,
    collector: EvidenceCollector,
    remote_path: str,
    prefix: str,
    result,
    output_path: Path | None = None,
) -> dict[str, Any]:
    _write_command_artifacts(collector=collector, prefix=prefix, result=result)
    fetch_summary: dict[str, Any] = {
        "remote_path": remote_path,
        "command_result": result.to_dict(),
        "status": "error",
    }
    if result.exit_code != 0:
        fetch_summary["error"] = f"Remote fetch command returned exit code {result.exit_code}"
        return fetch_summary

    metadata, payload_lines = _parse_fetch_output_lines(result.output_lines)
    mode = metadata.get("mode", "file")
    fetch_summary["mode"] = mode
    if not payload_lines:
        fetch_summary["error"] = "No prefixed payload lines were found in fetch output."
        return fetch_summary
    try:
        payload = base64.b64decode("".join(payload_lines), validate=False)
    except Exception as exc:
        fetch_summary["error"] = f"Failed to decode base64 output: {exc}"
        return fetch_summary

    if output_path is None:
        relative_path = _fetch_artifact_relative_path(remote_path)
        if mode == "tar":
            relative_path += ".tar"
        local_path = collector.write_retrieved_artifact(
            relative_path,
            payload,
            artifact_type="retrieved_tar" if mode == "tar" else "retrieved_file",
        )
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(payload)
        collector.write_text_artifact("logs/fetched-external-output-path.txt", str(output_path) + "\n")
        local_path = collector.register_artifact(
            output_path,
            artifact_type="retrieved_tar" if mode == "tar" else "retrieved_file",
        )

    fetch_summary.update(
        {
            "status": "ok",
            "local_path": str(local_path),
            "size_bytes": len(payload),
            "sha256": _sha256_bytes(payload),
        }
    )
    return fetch_summary


def _parse_transfer_capabilities(output_lines: list[str]) -> dict[str, Any]:
    capabilities: dict[str, Any] = {
        "downloader": "unknown",
        "has_base64": False,
        "has_tar": False,
        "sd_http_helper": "unknown",
    }
    for raw_line in output_lines:
        line = raw_line.strip()
        if line.startswith("AUTODBG_DOWNLOADER="):
            capabilities["downloader"] = line.partition("=")[2] or "unknown"
        elif line.startswith("AUTODBG_BASE64="):
            capabilities["has_base64"] = line.partition("=")[2].lower() == "yes"
        elif line.startswith("AUTODBG_TAR="):
            capabilities["has_tar"] = line.partition("=")[2].lower() == "yes"
        elif line.startswith("AUTODBG_SD_HTTP_HELPER="):
            capabilities["sd_http_helper"] = line.partition("=")[2].lower()
    return capabilities


def _select_transfer_mode(requested_mode: str, *, capabilities: dict[str, Any], network_mode: str) -> str:
    downloader = capabilities.get("downloader", "unknown")
    has_base64 = bool(capabilities.get("has_base64"))
    has_tar = bool(capabilities.get("has_tar"))

    if network_mode == "offline" and requested_mode in {"http", "sd_http_helper"}:
        raise RuntimeError("Offline mode cannot use network HTTP transfer; use serial_bundle or auto.")

    if requested_mode == "http":
        if downloader in {"none", "unknown"}:
            raise RuntimeError("HTTP transfer requested, but the device has no usable downloader.")
        return "http"

    if requested_mode == "sd_http_helper":
        if capabilities.get("sd_http_helper") != "yes":
            raise RuntimeError("SD HTTP helper transfer requested, but the helper executable was not found on the device.")
        return "sd_http_helper"

    if requested_mode == "serial_bundle":
        if not has_base64 or not has_tar:
            raise RuntimeError("Serial bundle transfer requested, but the device lacks base64/tar support.")
        return "serial_bundle"

    if network_mode == "offline":
        if has_base64 and has_tar:
            return "serial_bundle"
        raise RuntimeError("Offline mode requires serial bundle support (base64 + tar), but the device does not provide it.")

    if downloader not in {"none", "unknown"}:
        return "http"
    if capabilities.get("sd_http_helper") == "yes":
        return "sd_http_helper"
    if has_base64 and has_tar:
        return "serial_bundle"
    raise RuntimeError("No usable transfer path found: downloader unavailable, SD HTTP helper missing, and serial bundle support missing.")


def _build_serial_bundle_commands(
    *,
    base64_payload: str,
    workspace: str,
    chunk_size: int,
    bundle_name: str = ".autodbg-bundle.tar",
    artifact_count: int | None = None,
) -> tuple[str, list[str], str]:
    if chunk_size <= 0:
        raise ValueError("serial bundle chunk size must be positive")
    quoted_workspace = _sh_single_quote(workspace)
    quoted_bundle_name = _sh_single_quote(bundle_name)
    quoted_base64_name = _sh_single_quote(f"{bundle_name}.b64")
    chunks = split_base64_payload(base64_payload, chunk_size=chunk_size)
    prepare_command = (
        f"mkdir -p {quoted_workspace} && "
        f"cd {quoted_workspace} && "
        f"rm -f {quoted_base64_name} {quoted_bundle_name} && "
        f": > {quoted_base64_name}"
    )
    upload_commands = [
        f"cd {quoted_workspace} && printf '%s' '{chunk}' >> {quoted_base64_name}"
        for chunk in chunks
    ]
    files_label = artifact_count if artifact_count is not None else "unknown"
    finalize_command = (
        f"cd {quoted_workspace} && "
        f"base64 -d < {quoted_base64_name} > {quoted_bundle_name} && "
        f"tar -xf {quoted_bundle_name} && "
        f"rm -f {quoted_base64_name} {quoted_bundle_name} && "
        f"printf 'AUTODBG_SERIAL_BUNDLE_OK workspace=%s chunks={len(chunks)} files={files_label}\\n' \"$PWD\""
    )
    return prepare_command, upload_commands, finalize_command


def _sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _build_structured_command(shell_command: str) -> str:
    quoted_prefix = _sh_single_quote(_STRUCTURED_OUTPUT_PREFIX + "%s\\n")
    return (
        "( "
        "AUTODBG_TMP=\"/tmp/.autodbg_cmd_$$.log\"; "
        f"{{ {shell_command}; }} >\"$AUTODBG_TMP\" 2>&1; "
        "AUTODBG_RC=$?; "
        "while IFS= read -r AUTODBG_LINE || [ -n \"$AUTODBG_LINE\" ]; do "
        f"printf {quoted_prefix} \"$AUTODBG_LINE\"; "
        "done < \"$AUTODBG_TMP\"; "
        "rm -f \"$AUTODBG_TMP\"; "
        "exit \"$AUTODBG_RC\""
        " )"
    )


def _extract_structured_output_lines(output_lines: list[str]) -> list[str]:
    structured_lines = [
        line[len(_STRUCTURED_OUTPUT_PREFIX) :]
        for line in output_lines
        if line.startswith(_STRUCTURED_OUTPUT_PREFIX)
    ]
    return structured_lines or output_lines


def _execute_structured_command(
    *,
    controller: DeviceController,
    shell_command: str,
    timeout: float,
    serial_port=None,
) -> CommandResult:
    result = controller.execute(
        _build_structured_command(shell_command),
        timeout=timeout,
        serial_port=serial_port,
    )
    return CommandResult(
        command=shell_command,
        exit_code=result.exit_code,
        output_lines=_extract_structured_output_lines(result.output_lines),
        transcript=result.transcript,
    )
