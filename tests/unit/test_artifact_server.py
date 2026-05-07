from pathlib import Path
import json
import socket
import tempfile
import unittest
import urllib.request

from autodbg.host.artifact_server import build_manifest, find_available_port, serve_directory, write_manifest, write_pull_script, write_transfer_list


class ArtifactServerTest(unittest.TestCase):
    def test_build_manifest_collects_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "payload").mkdir(parents=True, exist_ok=True)
            (root / "payload" / "agent.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8", newline="\n")
            (root / "manifest-ignore.json").write_text("{}", encoding="utf-8", newline="\n")

            manifest = build_manifest(root, base_url="http://127.0.0.1:8765", manifest_name="manifest-ignore.json")

        self.assertEqual(manifest["artifact_count"], 1)
        self.assertEqual(manifest["root"], ".")
        self.assertEqual(manifest["artifacts"][0]["relative_path"], "payload/agent.sh")
        self.assertEqual(manifest["artifacts"][0]["url"], "http://127.0.0.1:8765/payload/agent.sh")

    def test_write_manifest_persists_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "artifact.bin").write_bytes(b"demo")

            manifest_path = write_manifest(root, manifest_name="autodbg-manifest.json")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["artifact_count"], 1)
        self.assertEqual(payload["root"], ".")
        self.assertEqual(payload["artifacts"][0]["relative_path"], "artifact.bin")

    def test_write_pull_script_embeds_workspace_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "payload").mkdir(parents=True, exist_ok=True)
            (root / "payload" / "agent.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8", newline="\n")

            script_path = write_pull_script(
                root,
                base_url="http://127.0.0.1:8765",
                workspace="/mnt/sdcard/autodbg",
                script_name="autodbg-pull.sh",
                manifest_name="autodbg-manifest.json",
            )
            script_text = script_path.read_text(encoding="utf-8")

        self.assertIn("DEFAULT_WORKSPACE='/mnt/sdcard/autodbg'", script_text)
        self.assertIn('fetch_to_file "$BASE_URL/$MANIFEST_NAME"', script_text)
        self.assertIn('fetch_to_file "$BASE_URL/payload/agent.sh"', script_text)
        self.assertIn('verify_file "$WORKSPACE/payload/agent.sh"', script_text)
        self.assertIn("AUTODBG_PULL_OK", script_text)

    def test_write_transfer_list_includes_manifest_and_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "payload").mkdir(parents=True, exist_ok=True)
            (root / "payload" / "agent.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8", newline="\n")
            (root / "autodbg-pull.sh").write_text("ignored\n", encoding="utf-8", newline="\n")

            list_path = write_transfer_list(
                root,
                list_name="autodbg-files.txt",
                include_names=("autodbg-manifest.json",),
                exclude_names=("autodbg-pull.sh",),
            )
            lines = list_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(lines[0], "autodbg-manifest.json")
        self.assertIn("payload/agent.sh", lines)
        self.assertNotIn("autodbg-pull.sh", lines)
        self.assertNotIn("autodbg-files.txt", lines)

    def test_serve_directory_exposes_health_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server, thread = serve_directory(root, bind="127.0.0.1", port=0)
            try:
                port = server.server_address[1]
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/__autodbg_health.json", timeout=2.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2.0)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["root"], str(root.resolve()))

    def test_find_available_port_skips_occupied_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            occupied_port = sock.getsockname()[1]

            selected_port = find_available_port("127.0.0.1", occupied_port, attempts=20)

        self.assertNotEqual(selected_port, occupied_port)


if __name__ == "__main__":
    unittest.main()
