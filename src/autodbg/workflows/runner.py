from __future__ import annotations

from dataclasses import asdict

from autodbg.models.profile import RunProfiles
from autodbg.workflows.mvp import WorkflowStep, startup_check_steps


class WorkflowRunner:
    def __init__(self, profiles: RunProfiles) -> None:
        self.profiles = profiles

    def build_plan(self) -> tuple[str, list[WorkflowStep]]:
        if self.profiles.task.initial_workflow == "startup_check":
            return "startup_check", startup_check_steps()
        raise ValueError(f"Unsupported workflow: {self.profiles.task.initial_workflow}")

    @staticmethod
    def as_dicts(steps: list[WorkflowStep]) -> list[dict[str, str]]:
        return [asdict(step) for step in steps]
