from pathlib import Path
import tempfile
import unittest

from autodbg.session.manager import SessionManager


class SessionManagerTest(unittest.TestCase):
    def test_create_session_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir) / "artifacts"
            session = SessionManager(artifacts_root).create("AV130N-LAB", "startup_check")
            self.assertTrue(session.session_paths.root.exists())
            self.assertTrue(session.session_paths.core_dir.exists())
            self.assertTrue(session.session_paths.deploy_dir.exists())
            self.assertTrue(session.session_paths.logs_dir.exists())
            self.assertTrue(session.session_paths.retrieved_dir.exists())
            self.assertIn("startup_check", session.session_id)
            self.assertEqual(session.session_paths.retrieved_dir.parents[1], artifacts_root.parent / "retrieved")

    def test_latest_session_dir_returns_lexically_latest_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifacts_root = Path(temp_dir)
            older = artifacts_root / "20260415" / "235959000-av130n-lab-startup_check-aaaa"
            newer = artifacts_root / "20260416" / "000001000-av130n-lab-startup_check-bbbb"
            older.mkdir(parents=True, exist_ok=True)
            newer.mkdir(parents=True, exist_ok=True)

            latest = SessionManager(artifacts_root).latest_session_dir()

            self.assertEqual(latest, newer)


if __name__ == "__main__":
    unittest.main()
