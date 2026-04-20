from pathlib import Path
import tarfile
import tempfile
import unittest

from autodbg.host.bundle import build_serial_bundle, split_base64_payload


class SerialBundleTest(unittest.TestCase):
    def test_build_serial_bundle_packages_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload_root = root / "payload"
            nested_dir = payload_root / "configs"
            nested_dir.mkdir(parents=True, exist_ok=True)
            (payload_root / "hello.txt").write_text("hello\n", encoding="utf-8", newline="\n")
            (nested_dir / "device.ini").write_text("mode=test\n", encoding="utf-8", newline="\n")

            bundle = build_serial_bundle(
                payload_root,
                output_path=root / "bundle.tar",
            )

            self.assertEqual(bundle.artifact_count, 2)
            self.assertTrue(bundle.bundle_path.exists())
            self.assertIn("hello.txt", bundle.relative_paths)
            self.assertIn("configs/device.ini", bundle.relative_paths)

            with tarfile.open(bundle.bundle_path, mode="r") as archive:
                names = archive.getnames()
            self.assertIn("hello.txt", names)
            self.assertIn("configs/device.ini", names)

    def test_split_base64_payload_respects_chunk_size(self) -> None:
        self.assertEqual(
            split_base64_payload("ABCDEFGHIJ", chunk_size=4),
            ["ABCD", "EFGH", "IJ"],
        )


if __name__ == "__main__":
    unittest.main()
