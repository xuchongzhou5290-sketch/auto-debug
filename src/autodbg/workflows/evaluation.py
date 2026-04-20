from __future__ import annotations

from typing import Any


def _index_checks(checks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(check.get("name", "")): check for check in checks}


def _output_lines(check: dict[str, Any] | None) -> list[str]:
    if not check:
        return []
    return [str(line) for line in check.get("output_lines", [])]


def _first_output_line(check: dict[str, Any] | None) -> str | None:
    lines = _output_lines(check)
    if not lines:
        return None
    return lines[0].strip() or None


def _check_ok(check: dict[str, Any] | None) -> bool:
    return check is not None and check.get("exit_code") == 0


def _contains_any(lines: list[str], needles: list[str]) -> bool:
    lowered = [line.lower() for line in lines]
    for needle in needles:
        if any(needle.lower() in line for line in lowered):
            return True
    return False


def _build_finding(level: str, check_name: str, message: str) -> dict[str, str]:
    return {
        "level": level,
        "check_name": check_name,
        "message": message,
    }


def _finalize_evaluation(*, highlights: dict[str, Any], findings: list[dict[str, str]]) -> dict[str, Any]:
    errors = [finding for finding in findings if finding["level"] == "error"]
    warnings = [finding for finding in findings if finding["level"] == "warning"]
    if errors:
        verdict = "fail"
        summary = f"{len(errors)} blocking issue(s) detected."
    elif warnings:
        verdict = "partial_pass"
        summary = f"{len(warnings)} warning(s) detected."
    else:
        verdict = "pass"
        summary = "All machine checks passed."
    return {
        "verdict": verdict,
        "summary": summary,
        "findings": findings,
        "highlights": highlights,
    }


def evaluate_health_checks(checks: list[dict[str, Any]], *, app_name: str) -> dict[str, Any]:
    checks_by_name = _index_checks(checks)
    findings: list[dict[str, str]] = []

    appver = checks_by_name.get("appver")
    appver_line = _first_output_line(appver)
    if not _check_ok(appver) or not appver_line:
        findings.append(_build_finding("error", "appver", "Failed to read app version from /opt/appver.txt."))

    lecam_process = checks_by_name.get("lecam_process")
    lecam_lines = _output_lines(lecam_process)
    if not _check_ok(lecam_process) or not _contains_any(lecam_lines, [app_name]):
        findings.append(_build_finding("error", "lecam_process", f"{app_name} process was not confirmed from ps output."))

    mmc_devices = checks_by_name.get("mmc_devices")
    mmc_device_lines = _output_lines(mmc_devices)
    if not _check_ok(mmc_devices) or not _contains_any(mmc_device_lines, ["mmcblk"]):
        findings.append(_build_finding("error", "mmc_devices", "No mmc block device was detected on the target."))

    mmc_partitions = checks_by_name.get("mmc_partitions")
    mmc_partition_lines = _output_lines(mmc_partitions)
    if not _check_ok(mmc_partitions) or not _contains_any(mmc_partition_lines, ["mmcblk0p", "mmcblk"]):
        findings.append(_build_finding("error", "mmc_partitions", "No mmc partition information was reported."))

    mmc_mount = checks_by_name.get("mmc_mount")
    mmc_mount_lines = _output_lines(mmc_mount)
    if not _check_ok(mmc_mount) or not _contains_any(mmc_mount_lines, ["/mnt/sdcard"]):
        findings.append(_build_finding("error", "mmc_mount", "The SD card mount point /mnt/sdcard is not mounted."))

    sdcard_listing = checks_by_name.get("sdcard_listing")
    if not _check_ok(sdcard_listing):
        findings.append(_build_finding("error", "sdcard_listing", "Failed to list /mnt/sdcard contents."))

    sdcard_capacity = checks_by_name.get("sdcard_capacity")
    if not _check_ok(sdcard_capacity):
        findings.append(_build_finding("error", "sdcard_capacity", "Failed to read /mnt/sdcard capacity information."))

    write_probe = checks_by_name.get("sdcard_write_probe")
    if write_probe is not None:
        probe_lines = _output_lines(write_probe)
        if not _check_ok(write_probe) or not _contains_any(probe_lines, ["PROBE_OK"]):
            findings.append(_build_finding("error", "sdcard_write_probe", "Temporary SD card write/read/delete probe failed."))

    dmesg_tail = checks_by_name.get("mmc_dmesg_tail")
    dmesg_lines = _output_lines(dmesg_tail)
    if _contains_any(dmesg_lines, ["Volume was not properly unmounted", "Please run fsck"]):
        findings.append(_build_finding("warning", "mmc_dmesg_tail", "FAT filesystem reports the card was not cleanly unmounted."))
    if _contains_any(dmesg_lines, ["error -110", "tried to reset card"]):
        findings.append(_build_finding("warning", "mmc_dmesg_tail", "MMC driver reported timeout/reset activity in recent dmesg output."))

    highlights = {
        "app_version": appver_line,
        "app_process_seen": _contains_any(lecam_lines, [app_name]),
        "mmc_device_count": len(mmc_device_lines),
        "sd_mount": _first_output_line(mmc_mount),
        "sd_listing_preview": _first_output_line(sdcard_listing),
        "sd_write_probe_ok": _contains_any(_output_lines(write_probe), ["PROBE_OK"]) if write_probe is not None else None,
    }
    return _finalize_evaluation(highlights=highlights, findings=findings)


def evaluate_startup_run(
    checks: list[dict[str, Any]],
    *,
    app_name: str,
    observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks_by_name = _index_checks(checks)
    findings: list[dict[str, str]] = []
    observation = observation or {}
    marker_hits = observation.get("marker_hits", [])

    appver = checks_by_name.get("appver")
    appver_line = _first_output_line(appver)
    if not _check_ok(appver) or not appver_line:
        findings.append(_build_finding("error", "appver", "Startup run could not confirm the application version."))

    lecam_process = checks_by_name.get("lecam_process")
    lecam_lines = _output_lines(lecam_process)
    app_process_seen = _check_ok(lecam_process) and _contains_any(lecam_lines, [app_name])
    if not app_process_seen:
        findings.append(_build_finding("error", "lecam_process", f"Startup run did not confirm a running {app_name} process."))

    if any(str(hit.get("tag", "")) == "panic" for hit in marker_hits):
        findings.append(_build_finding("error", "serial_observation", "Serial observation captured a panic marker during startup."))

    mmc_mount = checks_by_name.get("mmc_mount")
    if not _check_ok(mmc_mount):
        findings.append(_build_finding("warning", "mmc_mount", "Startup run did not confirm the SD card mount state."))

    sdcard_listing = checks_by_name.get("sdcard_listing")
    if not _check_ok(sdcard_listing):
        findings.append(_build_finding("warning", "sdcard_listing", "Startup run could not list /mnt/sdcard contents."))

    app_ready_seen = any(str(hit.get("tag", "")) == "app_ready" for hit in marker_hits)
    if not app_ready_seen and not app_process_seen and int(observation.get("lines_captured", 0) or 0) > 0:
        findings.append(_build_finding("warning", "serial_observation", "No app_ready marker was observed in the serial window."))

    if int(observation.get("lines_captured", 0) or 0) == 0:
        findings.append(_build_finding("warning", "serial_observation", "No serial lines were captured during the observation window."))

    highlights = {
        "app_version": appver_line,
        "app_process_seen": app_process_seen,
        "app_ready_seen": app_ready_seen,
        "serial_lines_captured": int(observation.get("lines_captured", 0) or 0),
        "last_device_state": observation.get("last_device_state"),
        "sd_mount": _first_output_line(mmc_mount),
    }
    return _finalize_evaluation(highlights=highlights, findings=findings)
