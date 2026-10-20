"""Persistent failure context, exact container ownership, quotas and API tests."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from aquahub_agent.core import Application, Config
from aquahub_agent.docker import PROJECT
from aquahub_agent.health_history import HealthHistory
from aquahub_agent.security import ApiError, AuthPolicy, RequestContext
from aquahub_agent.store import Store
from test_core import FakeDocker


OLD = "a" * 64
NEW = "b" * 64


class LogDocker(FakeDocker):
    def __init__(self):
        super().__init__()
        self.identity = {OLD: {"service": "gcs", "project": PROJECT}, NEW: {"service": "gcs", "project": PROJECT}}
        self.texts = {OLD: "2026-10-20T12:00:00Z old container failed\npassword='private secret'\n" +
                      "PrivateKey=" + "A" * 43 + "=\n\x1b[31mterminal\x1b[0m\x00",
                      NEW: "new container restarted successfully"}
        self.calls = []
        self.logs_error = None
        next(row for row in self.rows if row["service"] == "gcs")["id"] = OLD

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        identifier = args[-1]
        if args[0] == "inspect":
            if identifier not in self.identity:
                raise ApiError("not_found", "Le conteneur a été supprimé")
            return json.dumps(self.identity[identifier])
        if args[0] == "logs":
            if self.logs_error:
                raise self.logs_error
            return self.texts[identifier]
        raise AssertionError(args)


class HealthStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.docker = LogDocker()
        self.history = HealthHistory(self.store, self.docker, minimum_interval=0)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def capture(self, key="event-one", **kwargs):
        return self.history.capture(key, "gcs", OLD, "die", **kwargs)

    def test_excerpts_are_persisted_redacted_and_survive_container_removal_and_restart(self):
        excerpt = self.capture()
        self.assertTrue(excerpt["available"])
        self.assertEqual(excerpt["status"], "available")
        before = self.history.get(excerpt["id"])
        self.assertIn("old container failed", before["text"])
        for secret in ("private secret", "A" * 43, "\x1b", "\x00"):
            self.assertNotIn(secret, before["text"])
        for database_file in self.store.path.parent.glob("aquahub.sqlite3*"):
            content = database_file.read_bytes()
            for secret in ("private secret", "A" * 43):
                self.assertNotIn(secret.encode(), content)
        self.docker.identity.clear()
        self.docker.texts.clear()
        self.store.close()
        self.store = Store(self.root / "state")
        self.history = HealthHistory(self.store, self.docker)
        self.assertEqual(self.history.get(excerpt["id"])["text"], before["text"])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_duplicate_event_never_recaptures_logs_from_a_replacement_container(self):
        first = self.capture()
        count = len(self.docker.calls)
        self.docker.identity.pop(OLD)
        again = self.capture()
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.docker.calls), count)

    def test_wrong_project_or_service_prevents_all_log_reads(self):
        self.docker.identity[OLD] = {"service": "gcs", "project": "another-project"}
        summary = self.capture()
        self.assertEqual(summary["status"], "unavailable")
        self.assertFalse(any(args[0] == "logs" for args, _ in self.docker.calls))
        self.assertEqual(self.history.get(summary["id"])["text"], "")

    def test_destroyed_container_and_log_driver_errors_are_explicit_without_fake_logs(self):
        self.docker.identity.pop(OLD)
        first = self.capture()
        self.assertFalse(first["available"])
        self.assertIn("supprimé", first["error"])
        self.docker.identity[OLD] = {"service": "gcs", "project": PROJECT}
        self.docker.logs_error = ApiError("timeout", "Logs indisponibles")
        second = self.capture("event-two")
        self.assertEqual(second["status"], "unavailable")
        self.assertEqual(self.history.get(second["id"])["text"], "")

    def test_entry_byte_quota_respects_utf8_and_marks_truncation(self):
        self.history = HealthHistory(self.store, self.docker, max_entry_bytes=24, minimum_interval=0)
        self.docker.texts[OLD] = "🛥" * 100
        summary = self.capture()
        text = self.history.get(summary["id"])["text"]
        self.assertLessEqual(len(text.encode()), 24)
        self.assertTrue(summary["truncated"])
        self.assertNotIn("�", text)

    def test_global_count_and_byte_quotas_prune_excerpts_but_leave_events(self):
        self.history = HealthHistory(self.store, self.docker, max_entry_bytes=20, max_total_bytes=40,
                                     max_records=2, minimum_interval=0)
        self.docker.texts[OLD] = "x" * 20
        identifiers = []
        for number in range(4):
            excerpt = self.capture("event-" + str(number))
            identifiers.append(excerpt["id"])
            self.store.event("docker_event", "Docker : die", service="gcs", details={"log_snapshot_id": excerpt["id"]})
        with self.store.lock:
            count, size = self.store.db.execute("SELECT COUNT(*),SUM(bytes) FROM health_logs").fetchone()
        self.assertEqual(count, 2)
        self.assertLessEqual(size, 40)
        self.assertEqual(self.history.summary(identifiers[0])["status"], "expired")
        events = self.history.enrich(self.store.events())
        self.assertEqual(len(events), 4)
        self.assertEqual(events[-1]["log_snapshot"]["status"], "expired")

    def test_age_retention_expires_excerpt_without_removing_original_event(self):
        excerpt = self.capture()
        self.store.event("docker_event", "Docker : die", service="gcs", details={"log_snapshot_id": excerpt["id"]})
        old = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat().replace("+00:00", "Z")
        with self.store.transaction() as database:
            database.execute("UPDATE health_logs SET captured_at=?", (old,))
        self.history.prune()
        self.assertEqual(self.history.summary(excerpt["id"])["status"], "expired")
        self.assertEqual(len(self.store.events()), 1)
        with self.assertRaises(ApiError) as error:
            self.history.get(excerpt["id"])
        self.assertEqual(error.exception.code, "log_expired")

    def test_rate_limit_and_interrupted_capture_remain_visible(self):
        self.history.minimum_interval = 60
        self.capture()
        summary = self.capture("event-two")
        self.assertEqual(summary["status"], "rate_limited")
        self.assertEqual(sum(args[0] == "logs" for args, _ in self.docker.calls), 1)
        with self.store.transaction() as database:
            database.execute("UPDATE health_logs SET status='pending' WHERE id=?", (summary["id"],))
        self.history = HealthHistory(self.store, self.docker)
        self.assertEqual(self.history.summary(summary["id"])["status"], "interrupted")

    def test_export_has_global_size_limit_and_event_time_window(self):
        self.docker.texts[OLD] = "x" * 20
        events = []
        for number in range(3):
            excerpt = self.capture("event-" + str(number), event_at="2026-10-20T12:00:00Z")
            events.append({"details": {"log_snapshot_id": excerpt["id"]}})
        self.assertEqual(len(self.history.export(events, max_bytes=25)), 1)
        logs_args = next(args for args, _ in self.docker.calls if args[0] == "logs")
        self.assertIn("--since", logs_args)
        self.assertIn("--until", logs_args)
        self.assertEqual(logs_args[-1], OLD)


class HealthApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.docker = LogDocker()
        self.config = Config(self.root / "socket", self.root / "state", no_poll=True)
        self.app = Application(self.config, docker=self.docker, policy=AuthPolicy(os.getuid(), privileged=False))
        self.context = RequestContext(os.getuid(), os.getpid())

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def call(self, method, params=None):
        return self.app.dispatch(method, params or {}, self.context)

    def failure(self, action="die", actor=OLD):
        self.docker.pending_events = [{"Action": action, "timeNano": 1792497600000000000,
            "Actor": {"ID": actor, "Attributes": {"com.docker.compose.service": "gcs", "exitCode": "137"}}}]
        self.call("status.get")
        return next(event for event in self.call("events.list")["events"] if event["kind"] == "docker_event")

    def test_short_lived_failure_context_uses_the_old_container_not_current_replacement(self):
        next(row for row in self.docker.rows if row["service"] == "gcs")["id"] = NEW
        event = self.failure()
        self.assertEqual(event["log_snapshot"]["container_id"], OLD)
        snapshot = self.call("events.logs", {"snapshot_id": event["log_snapshot"]["id"]})
        self.assertIn("old container failed", snapshot["text"])
        self.assertNotIn("new container restarted", snapshot["text"])

    def test_unhealthy_event_and_state_change_share_one_deduplicated_excerpt(self):
        self.call("status.get")
        next(row for row in self.docker.rows if row["service"] == "gcs")["health"] = "unhealthy"
        event = self.failure("health_status: unhealthy")
        entries = self.call("events.list")["events"]
        changed = next(entry for entry in entries if entry["kind"] == "state_change")
        self.assertEqual(changed["details"]["log_snapshot_id"], event["details"]["log_snapshot_id"])
        self.assertEqual(sum(args[0] == "logs" for args, _ in self.docker.calls), 1)

    def test_initial_failed_state_is_observed_without_inventing_a_past_crash(self):
        row = next(row for row in self.docker.rows if row["service"] == "gcs")
        row.update(state="exited", running=False)
        self.call("status.get")
        entries = self.call("events.list")["events"]
        observed = next(event for event in entries if event["kind"] == "state_observed")
        self.assertIsNone(observed["details"]["before"])
        self.assertEqual(observed["log_snapshot"]["status"], "available")
        self.assertFalse(any(event["kind"] == "docker_event" for event in entries))

    def test_diagnostics_export_contains_redacted_historical_excerpt_and_api_is_closed(self):
        event = self.failure()
        self.docker.identity.clear()
        result = self.call("diagnostics.export")
        payload = json.loads(result["content"])
        self.assertEqual(payload["failure_log_excerpts"][0]["id"], event["log_snapshot"]["id"])
        self.assertIn("old container failed", payload["failure_log_excerpts"][0]["text"])
        self.assertNotIn("private secret", result["content"])
        self.assertEqual(Path(result["path"]).stat().st_mode & 0o777, 0o600)
        for identifier in (True, "../aquahub.sqlite3", "a" * 65):
            with self.assertRaises(ApiError):
                self.call("events.logs", {"snapshot_id": identifier})


if __name__ == "__main__":
    unittest.main()
