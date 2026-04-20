from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class WorkflowStep:
    step_id: str
    title: str
    description: str


def startup_check_steps() -> list[WorkflowStep]:
    return [
        WorkflowStep("load_profiles", "Load profiles", "Load device, model, task, and transport profiles."),
        WorkflowStep("create_session", "Create session", "Create the artifact directory and session metadata."),
        WorkflowStep("observe_serial", "Observe serial", "Prepare continuous serial observation and marker matching."),
        WorkflowStep("establish_control", "Establish control", "Plan the control path for shell or debug agent access."),
        WorkflowStep("run_checks", "Run startup checks", "Execute baseline startup validation steps."),
        WorkflowStep("collect_evidence", "Collect evidence", "Write summary, event stream, and artifact manifest."),
    ]
