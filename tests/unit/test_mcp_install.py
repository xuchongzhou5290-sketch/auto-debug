import json
import tempfile
import unittest
from pathlib import Path

from autodbg.mcp.install import HOME_PLUGIN_NAME, install_home_plugin


class McpInstallTest(unittest.TestCase):
    def test_repo_local_plugin_config_is_relocatable(self) -> None:
        root = Path(__file__).resolve().parents[2]
        legacy_root = "X:" + "\\Auto-Debug"
        plugin_dir = root / "plugins" / HOME_PLUGIN_NAME
        mcp_config = json.loads((plugin_dir / ".mcp.json").read_text(encoding="utf-8"))
        server = mcp_config["mcpServers"]["embedded-device-auto-debug"]

        self.assertEqual(server["command"], "powershell.exe")
        self.assertIn(".\\scripts\\launch-autodbg-mcp.ps1", server["args"])
        self.assertEqual(server["env"]["AUTO_DBG_MCP_LOG_DIR"], ".autodbg")
        self.assertTrue((plugin_dir / "scripts" / "launch-autodbg-mcp.ps1").is_file())
        self.assertNotIn(legacy_root, json.dumps(mcp_config, ensure_ascii=False))

    def test_install_home_plugin_creates_expected_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "Auto-Debug"
            home_root = root / "home"
            project_root.mkdir()
            home_root.mkdir()

            result = install_home_plugin(project_root=project_root, home_root=home_root)
            plugin_dir = result["plugin_dir"]
            self.assertEqual(plugin_dir, home_root / "plugins" / HOME_PLUGIN_NAME)
            self.assertTrue(result["plugin_manifest"].is_file())
            self.assertTrue(result["mcp_config"].is_file())
            self.assertTrue(result["launcher_script"].is_file())
            self.assertTrue(result["server_script"].is_file())
            self.assertTrue(result["marketplace"].is_file())

            mcp_config = json.loads(result["mcp_config"].read_text(encoding="utf-8"))
            server = mcp_config["mcpServers"]["embedded-device-auto-debug"]
            self.assertEqual(server["command"], "powershell.exe")
            self.assertEqual(server["env"]["AUTO_DBG_PROJECT_ROOT"], str(project_root))
            self.assertEqual(server["env"]["AUTO_DBG_MCP_LOG_DIR"], str(project_root / ".autodbg"))
            self.assertIn(str(result["launcher_script"]), server["args"])

            launcher_text = result["launcher_script"].read_text(encoding="utf-8")
            legacy_root = "X:" + "\\Auto-Debug"
            self.assertIn("$PSScriptRoot", launcher_text)
            self.assertNotIn("C:\\Users\\demo\\plugins", launcher_text)
            self.assertIn("AUTO_DBG_HOME", launcher_text)
            self.assertIn("AUTO_DBG_MCP_LOG_DIR", launcher_text)
            self.assertIn("mcp-launcher.log", launcher_text)
            self.assertIn("mcp-stderr", launcher_text)
            self.assertNotIn(legacy_root, launcher_text)

            server_text = result["server_script"].read_text(encoding="utf-8")
            self.assertIn('AUTO_DBG_PROJECT_ROOT', server_text)
            self.assertIn('AUTO_DBG_HOME', server_text)
            self.assertNotIn(legacy_root, server_text)

    def test_install_home_plugin_upserts_marketplace_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project_root = root / "Auto-Debug"
            home_root = root / "home"
            marketplace_path = home_root / ".agents" / "plugins" / "marketplace.json"
            project_root.mkdir()
            marketplace_path.parent.mkdir(parents=True)
            marketplace_path.write_text(
                json.dumps(
                    {
                        "name": "custom-local",
                        "interface": {"displayName": "Custom Local Plugins"},
                        "plugins": [
                            {
                                "name": "other-plugin",
                                "source": {"source": "local", "path": "./plugins/other-plugin"},
                                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                                "category": "Productivity",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )

            result = install_home_plugin(project_root=project_root, home_root=home_root)
            marketplace = json.loads(result["marketplace"].read_text(encoding="utf-8"))
            self.assertEqual(marketplace["name"], "custom-local")
            self.assertEqual(marketplace["interface"]["displayName"], "Custom Local Plugins")
            plugin_names = [item["name"] for item in marketplace["plugins"]]
            self.assertIn("other-plugin", plugin_names)
            self.assertIn(HOME_PLUGIN_NAME, plugin_names)


if __name__ == "__main__":
    unittest.main()
