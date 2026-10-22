"""Private capture boundaries; subprocess fixtures never contact a network."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/capture_network_reconnect.py"
spec = importlib.util.spec_from_file_location("network_capture", SCRIPT)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class NetworkCaptureTests(unittest.TestCase):
    def test_command_output_is_bounded_while_the_process_is_running(self):
        result = capture.command([sys.executable, "-c",
            "import os,time; os.write(1,b'x'*1048576); time.sleep(30)"], timeout=2)
        self.assertEqual(result["status"], "truncated")
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["stdout"]), capture.MAX_COMMAND_BYTES)

    def test_closed_pipes_do_not_bypass_the_command_deadline(self):
        start = time.monotonic()
        result = capture.command([sys.executable, "-c",
            "import os,time; os.close(1); os.close(2); time.sleep(30)"], timeout=0.15)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - start, 3)

    def test_regular_reader_is_bounded_and_rejects_links_and_fifos(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            regular.write_bytes(b"fixture" * 1024)
            raw, info = capture.read_regular(regular, 9)
            self.assertEqual(len(raw), 9)
            self.assertTrue(stat.S_ISREG(info.st_mode))
            link = root / "link"
            link.symlink_to(regular)
            with self.assertRaises(OSError):
                capture.read_regular(link, 9)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                capture.read_regular(fifo, 9)
            self.assertEqual(regular.read_bytes(), b"fixture" * 1024)

    def test_artifacts_are_private_and_existing_files_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o755)
            with self.assertRaises(ValueError):
                capture.private_destination(root)
            root.chmod(0o700)
            destination = capture.private_destination(root)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            report = destination / "test.json"
            capture.json_file(report, {"fixture": True})
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                capture.json_file(report, {"overwrite": True})
            self.assertEqual(json.loads(report.read_text()), {"fixture": True})

    def test_incomplete_fingerprints_do_not_claim_configuration_is_unchanged(self):
        valid = {"fixture": {"sha256": "synthetic", "mode": 0o600}}
        self.assertTrue(capture.compare_fingerprints(valid, valid))
        self.assertFalse(capture.compare_fingerprints(valid, {}))
        for incomplete in ({"$scan_limit": True}, {"fixture": {"unavailable": True}},
                           {"fixture": {"too_large": True}}):
            self.assertIsNone(capture.compare_fingerprints(incomplete, incomplete))


if __name__ == "__main__":
    unittest.main()
