from pathlib import Path
import unittest

from autodbg.session.contract import build_intervention_record, build_result_contract


class SessionContractTest(unittest.TestCase):
    def test_build_result_contract_emits_carry_forward_request_for_continue(self) -> None:
        result = build_result_contract(
            action="run",
            decision="continue",
            failure_stage="validation",
            retryable=True,
            loop_context={
                "goal_id": "startup-fix-001",
                "goal": "Reach app_ready",
                "iteration": 2,
                "max_iterations": 6,
            },
            current_session_dir=Path("C:/repo/auto-debug/artifacts/20260420/demo-session"),
            carry_forward_options={"observe_seconds": 3.0},
        )

        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["carry_forward_request"]["action"], "run")
        self.assertEqual(result["carry_forward_request"]["loop"]["goal_id"], "startup-fix-001")
        self.assertEqual(result["carry_forward_request"]["loop"]["iteration"], 3)
        self.assertEqual(
            result["carry_forward_request"]["loop"]["prev_session"],
            "C:\\repo\\auto-debug\\artifacts\\20260420\\demo-session",
        )
        self.assertEqual(result["carry_forward_request"]["options"]["observe_seconds"], 3.0)

    def test_build_result_contract_carries_intervention_context(self) -> None:
        result = build_result_contract(
            action="deploy-verify",
            decision="manual_required",
            failure_stage="manual_upgrade",
            retryable=True,
            loop_context={
                "goal_id": "upgrade-fix-001",
                "goal": "Upgrade and confirm fix",
                "iteration": 1,
            },
            current_session_dir=Path("C:/repo/auto-debug/artifacts/20260420/deploy-session"),
            carry_forward_options={"artifact": "payloads/APP.bin"},
            intervention_context={
                "git_commit": "abc1234",
                "artifact_sha256": "deadbeef",
                "expected_effect": "APP_READY appears",
            },
        )

        self.assertEqual(result["intervention_context"]["git_commit"], "abc1234")
        self.assertEqual(
            result["carry_forward_request"]["intervention_context"]["artifact_sha256"],
            "deadbeef",
        )

    def test_build_intervention_record_defaults_to_empty_metadata(self) -> None:
        record = build_intervention_record(
            kind="ai_patch",
            summary="Adjust retry window",
            files=["C:/repo/auto-debug/src/autodbg/cli/main.py"],
        )

        self.assertEqual(record["kind"], "ai_patch")
        self.assertEqual(record["summary"], "Adjust retry window")
        self.assertEqual(record["files"], ["C:/repo/auto-debug/src/autodbg/cli/main.py"])
        self.assertEqual(record["metadata"], {})


if __name__ == "__main__":
    unittest.main()
