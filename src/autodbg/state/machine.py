from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    IDLE = "idle"
    PRECHECK = "precheck"
    PREPARE_ARTIFACTS = "prepare_artifacts"
    DEPLOYING = "deploying"
    WAITING_BOOT = "waiting_boot"
    ESTABLISHING_CONTROL = "establishing_control"
    RUNNING_CHECKS = "running_checks"
    COLLECTING_EVIDENCE = "collecting_evidence"
    AWAITING_MANUAL_ACTION = "awaiting_manual_action"
    COMPLETED = "completed"
    FAILED = "failed"


class DeviceState(StrEnum):
    OFFLINE = "offline"
    BOOTLOADER = "bootloader"
    KERNEL_BOOTING = "kernel_booting"
    INIT_STARTING = "init_starting"
    LOGIN_PROMPT = "login_prompt"
    ROOT_SHELL = "root_shell"
    APP_STARTING = "app_starting"
    APP_READY = "app_ready"
    CRASH_LOOP = "crash_loop"
    PANIC_OR_HANG = "panic_or_hang"
    REBOOTING = "rebooting"


@dataclass(slots=True)
class StateEvent:
    event_type: str
    from_state: str
    to_state: str
    reason: str


@dataclass(slots=True)
class StateSnapshot:
    task_state: TaskState = TaskState.IDLE
    device_state: DeviceState = DeviceState.OFFLINE
    history: list[StateEvent] = field(default_factory=list)

    def transition_task(self, to_state: TaskState, reason: str) -> None:
        self.history.append(
            StateEvent(
                event_type="task",
                from_state=self.task_state.value,
                to_state=to_state.value,
                reason=reason,
            )
        )
        self.task_state = to_state

    def transition_device(self, to_state: DeviceState, reason: str) -> None:
        self.history.append(
            StateEvent(
                event_type="device",
                from_state=self.device_state.value,
                to_state=to_state.value,
                reason=reason,
            )
        )
        self.device_state = to_state

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["task_state"] = self.task_state.value
        payload["device_state"] = self.device_state.value
        return payload
