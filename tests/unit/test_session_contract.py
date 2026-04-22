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
            current_session_dir=Path("X:/Auto-Debug/artifacts/20260420/demo-session"),
            carry_forward_options={"observe_seconds": 3.0},
        )

        self.assertEqual(result["decision"], "continue")
        self.assertEqual(result["carry_forward_request"]["action"], "run")
        self.assertEqual(result["carry_forward_request"]["loop"]["goal_id"], "startup-fix-001")
        self.assertEqual(result["carry_forward_request"]["loop"]["iteration"], 3)
        self.assertEqual(
            result["carry_forward_request"]["loop"]["prev_session"],
            "X:\\Auto-Debug\\artifacts\\20260420\\demo-session",
        )
        self.assertEqual(result["carry_forward_request"]["options"]["observe_seconds"], 3.0)

    def test_build_intervention_record_defaults_to_empty_metadata(self) -> None:
        record = build_intervention_record(
            kind="ai_patch",
            summary="Adjust retry window",
            files=["X:/Auto-Debug/src/autodbg/cli/main.py"],
        )

        self.assertEqual(record["kind"], "ai_patch")
        self.assertEqual(record["summary"], "Adjust retry window")
        self.assertEqual(record["files"], ["X:/Auto-Debug/src/autodbg/cli/main.py"])
        self.assertEqual(record["metadata"], {})


if __name__ == "__main__":
    unittest.main()
