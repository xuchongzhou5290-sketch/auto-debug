import ctypes
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autodbg.deploy.local_tool import install_local_tool


class LocalToolInstallTest(unittest.TestCase):
    def test_install_local_tool_copies_core_layout_and_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "project"
            install_root = root / "installed"
            (project_root / "autodbg").mkdir(parents=True)
            (project_root / "src" / "autodbg").mkdir(parents=True)
            (project_root / "config").mkdir(parents=True)
            (project_root / "docs").mkdir(parents=True)
            (project_root / "profiles").mkdir(parents=True)
            (project_root / "plugins").mkdir(parents=True)
            (project_root / ".venv" / "Scripts").mkdir(parents=True)
            (project_root / "autodbg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
            (project_root / "src" / "autodbg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
            (project_root / "config" / "user-settings.toml").write_text("demo=1\n", encoding="utf-8", newline="\n")
            (project_root / "README.md").write_text("# demo\n", encoding="utf-8", newline="\n")
            (project_root / "observe-serial.ps1").write_text("Write-Host demo\n", encoding="utf-8", newline="\n")
            (project_root / "install-home-plugin.ps1").write_text("Write-Host plugin\n", encoding="utf-8", newline="\n")
            (project_root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8", newline="\n")
            (project_root / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8", newline="\n")

            result = install_local_tool(project_root=project_root, install_root=install_root)

            self.assertTrue((install_root / "autodbg" / "__init__.py").is_file())
            self.assertTrue((install_root / "src" / "autodbg" / "__init__.py").is_file())
            self.assertTrue((install_root / ".venv" / "Scripts" / "python.exe").is_file())
            self.assertTrue((install_root / "config" / "user-settings.toml").is_file())
            self.assertTrue(result["autodbg_cmd"].is_file())
            self.assertTrue(result["observe_cmd"].is_file())
            self.assertTrue(result["install_home_plugin_cmd"].is_file())

            autodbg_cmd_text = result["autodbg_cmd"].read_text(encoding="utf-8")
            self.assertIn("AUTO_DBG_HOME", autodbg_cmd_text)
            self.assertIn("AUTO_DBG_PROJECT_ROOT", autodbg_cmd_text)
            self.assertIn("PYTHONPATH", autodbg_cmd_text)
            self.assertIn("-m autodbg", autodbg_cmd_text)

    def test_install_local_tool_can_skip_venv_and_preserve_installed_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "project"
            install_root = root / "installed"
            (project_root / "autodbg").mkdir(parents=True)
            (project_root / "src" / "autodbg").mkdir(parents=True)
            (project_root / "config").mkdir(parents=True)
            (project_root / "README.md").write_text("# demo\n", encoding="utf-8", newline="\n")
            (project_root / "observe-serial.ps1").write_text("Write-Host demo\n", encoding="utf-8", newline="\n")
            (project_root / "install-home-plugin.ps1").write_text("Write-Host plugin\n", encoding="utf-8", newline="\n")
            (project_root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8", newline="\n")
            (project_root / "config" / "user-settings.toml").write_text("demo=1\n", encoding="utf-8", newline="\n")
            (project_root / ".venv" / "Scripts").mkdir(parents=True)
            (project_root / ".venv" / "Scripts" / "python.exe").write_text("", encoding="utf-8", newline="\n")
            (install_root / "config").mkdir(parents=True)
            (install_root / "config" / "user-settings.toml").write_text(
                "custom=1\n",
                encoding="utf-8",
                newline="\n",
            )

            install_local_tool(
                project_root=project_root,
                install_root=install_root,
                include_venv=False,
                include_local_settings=False,
            )

            self.assertFalse((install_root / ".venv").exists())
            self.assertEqual(
                (install_root / "config" / "user-settings.toml").read_text(encoding="utf-8"),
                "custom=1\n",
            )

    def test_install_local_tool_replaces_hidden_readonly_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "project"
            install_root = root / "installed"
            project_root.mkdir(parents=True)
            install_root.mkdir(parents=True)
            (project_root / ".gitignore").write_text("new\n", encoding="utf-8", newline="\n")
            target = install_root / ".gitignore"
            target.write_text("old\n", encoding="utf-8", newline="\n")
            target.chmod(stat.S_IREAD)
            if os.name == "nt":
                ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x2)

            try:
                with patch("autodbg.deploy.local_tool.LOCAL_COPY_DIRS", []), patch(
                    "autodbg.deploy.local_tool.LOCAL_COPY_FILES",
                    [".gitignore"],
                ):
                    install_local_tool(
                        project_root=project_root,
                        install_root=install_root,
                        include_venv=False,
                        include_local_settings=False,
                    )

                self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            finally:
                if target.exists():
                    if os.name == "nt":
                        ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x80)
                    target.chmod(stat.S_IWRITE | stat.S_IREAD)


if __name__ == "__main__":
    unittest.main()
