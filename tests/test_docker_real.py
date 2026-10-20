"""Optional integration against native Docker, strictly read-only.

Enable with AQUAHUB_TEST_DOCKER=1. No start/stop/restart, loading, or provisioning.
"""
from __future__ import annotations
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"agent"))
from aquahub_agent.docker import Docker, SERVICES


@unittest.skipUnless(os.environ.get("AQUAHUB_TEST_DOCKER")=="1", "Native Docker integration is explicitly opt-in")
class NativeDockerReadOnlyTests(unittest.TestCase):
    def test_inspect_handles_actual_docker_missing_health_fields(self):
        rows = Docker().containers()
        self.assertEqual({row["service"] for row in rows},set(SERVICES))
        for row in rows:
            self.assertIn(row["health"], {"not_defined","starting","healthy","unhealthy"})
            self.assertIsNone(row["version"])
            if row.get("id"):
                self.assertRegex(row["image_id"],r"^sha256:[0-9a-f]{64}$")

    def test_existing_bundle_is_discovered_without_reading_private_configs(self):
        docker = Docker()
        rows = docker.containers()
        if not any(row.get("id") for row in rows):
            self.skipTest("No existing Aqua Hub installation")
        bundle = docker.discover_bundle(rows)
        self.assertIsNotNone(bundle)
        self.assertTrue((bundle/"docker-compose.yml").is_file())

    def test_logs_are_bounded_from_existing_mqtt(self):
        docker = Docker()
        if not any(row.get("id") and row["service"]=="mosquitto" for row in docker.containers()):
            self.skipTest("MQTT container missing")
        text = docker.logs("mosquitto",10)
        self.assertLessEqual(len(text),131072)
        self.assertNotIn("\x1b",text)


if __name__=="__main__":
    unittest.main()
