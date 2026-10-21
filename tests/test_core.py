from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from aquahub_agent.core import Application, Config
from aquahub_agent import __version__
from aquahub_agent.docker import SERVICES
from aquahub_agent.security import ApiError, AuthPolicy, RequestContext, process_start_time, redact, safe_value
from aquahub_agent.store import Store


class FakeDocker:
    def __init__(self):
        self.failure = False
        self.actions = []
        self.rows = [{"service": name, "label": label, "id": name, "state": "running", "running": True,
                      "health": "healthy" if name == "mosquitto" else "not_defined", "restart_count": 0,
                      "image_id": "sha256:fixed", "version": None} for name,(label,_) in SERVICES.items()]
        self.pending_events = []

    def containers(self):
        if self.failure:
            raise ApiError("docker_unavailable", "Docker indisponible")
        return [dict(row) for row in self.rows]

    def discover_bundle(self, rows):
        return None

    def portainer(self):
        return {"available": False, "url": None}

    def events(self, since, until):
        result = self.pending_events
        self.pending_events = []
        return result

    def logs(self, service, tail):
        return redact("ready\npassword=supersecret\nhttps://operator:private@example.com/path")

    def action(self, service, operation):
        self.actions.append((service, operation))
        return {"service": service, "state": "running", "health": "not_defined", "image_id": "sha256:fixed"}


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.fake = FakeDocker()
        self.config = Config(self.base / "socket", self.base / "state", no_poll=True)
        self.app = Application(self.config, docker=self.fake, policy=AuthPolicy(os.getuid(), privileged=False))
        self.ctx = RequestContext(os.getuid(), os.getpid(), process_start_time(os.getpid()))

    def tearDown(self):
        self.app.close()
        self.directory.cleanup()

    def call(self, method, params=None, key=None):
        context = RequestContext(self.ctx.uid, self.ctx.pid, self.ctx.start_time, key)
        return self.app.dispatch(method, params, context)

    def wait_job(self, job_id):
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            job = self.app.store.job(job_id)
            if job["status"] not in {"queued", "running"} and not self.app._mutation_lock.locked():
                return job
            time.sleep(.01)
        self.fail("Job did not finish")

    def assertCode(self, code, method, params=None):
        with self.assertRaises(ApiError) as caught:
            self.call(method, params)
        self.assertEqual(caught.exception.code, code)

    def test_status_does_not_invent_health_or_release_versions(self):
        result = self.call("status.get")
        self.assertEqual(result["summary"]["running"], 8)
        self.assertEqual(result["summary"]["without_healthcheck"], 7)
        self.assertIsNone(result["services"][0]["version"])
        self.assertFalse(result["stale"])

    def test_collection_outage_preserves_snapshot_and_records_gap(self):
        self.call("status.get")
        self.fake.failure = True
        result = self.call("status.get")
        self.assertTrue(result["stale"])
        self.assertEqual(len(result["services"]), 8)
        self.fake.failure = False
        self.call("status.get")
        events = self.call("events.list")["events"]
        self.assertTrue(any(item["kind"] == "collection_resumed" for item in events))
        self.assertGreaterEqual(sum(item["kind"] == "history_gap" for item in events), 2)

    def test_state_changes_and_short_lived_crashes_remain_in_history(self):
        self.call("status.get")
        self.fake.rows[0]["state"] = "exited"
        self.fake.rows[0]["running"] = False
        self.fake.pending_events = [{"Action": "die", "timeNano": 12345, "Actor": {"ID":"abc", "Attributes":{"com.docker.compose.service":"gcs", "exitCode":"137"}}}]
        self.call("status.get")
        events = self.call("events.list")["events"]
        self.assertTrue(any(item["service"] == "mosquitto" and item["kind"] == "state_change" and item["severity"] == "error" for item in events))
        self.assertTrue(any(item["service"] == "gcs" and item["details"].get("exit_code") == "137" for item in events))

    def test_job_idempotency_survives_restart_without_second_action(self):
        job = self.call("service.restart", {"service":"gcs"}, "operation-123")
        self.assertEqual(self.wait_job(job["id"])["status"], "succeeded")
        again = self.call("service.restart", {"service":"gcs"}, "operation-123")
        self.assertEqual(job["id"], again["id"])
        self.app.close()
        self.app = Application(self.config, docker=self.fake, policy=AuthPolicy(os.getuid(), privileged=False))
        again = self.call("service.restart", {"service":"gcs"}, "operation-123")
        self.assertEqual(job["id"], again["id"])
        self.assertEqual(self.fake.actions, [("gcs","restart")])
        with self.assertRaises(ApiError) as caught:
            self.call("service.stop", {"service":"gcs"}, "operation-123")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_one_mutation_only_and_errors_are_persistent(self):
        started = threading.Event()
        release = threading.Event()
        job = self.app.submit_job("test.blocking", "test", {}, lambda: (started.set(), release.wait(2), {"ok":True})[-1])
        self.assertTrue(started.wait(1))
        try:
            self.assertCode("busy", "service.restart", {"service":"gcs"})
        finally:
            release.set()
        self.assertEqual(self.wait_job(job["id"])["status"], "succeeded")
        def failure():
            raise ApiError("test_failure", "password=never-export-me")
        job = self.app.submit_job("test.failure", "test", {}, failure)
        result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("never-export-me", json.dumps(result))

    def test_job_progress_is_durable_observed_and_bound_to_its_worker(self):
        observed = threading.Event()
        release = threading.Event()
        progress = {"phase": "reboot", "label": "Aqua_Drone_5 redémarre", "completed": 10,
                    "total": 12, "percent": 83, "indeterminate": True, "detail": "password=not-exported"}
        def work():
            self.app.report_progress(progress)
            observed.set()
            release.wait(2)
            raise ApiError("reboot_unverified", "Retour non confirmé")
        with self.assertRaises(RuntimeError):
            self.app.report_progress(progress)
        job = self.app.submit_job("configure_drone", "Aqua_Drone_5", {"number": 5}, work)
        self.assertTrue(observed.wait(1))
        try:
            first = self.call("jobs.get", {"id": job["id"]})
            second = self.call("status.get")["active_job"]
            self.assertEqual(first["progress"], second["progress"])
            self.assertEqual(first["progress"]["percent"], 83)
            self.assertNotIn("not-exported", json.dumps(first))
        finally:
            release.set()
        terminal = self.wait_job(job["id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["progress"]["percent"], 83)
        self.app.store.set_job_progress(job["id"], {**progress, "completed": 12, "percent": 100})
        self.assertEqual(self.app.store.job(job["id"])["progress"]["percent"], 83)
        self.app.close()
        self.app = Application(self.config, docker=self.fake, policy=AuthPolicy(os.getuid(), privileged=False))
        self.assertEqual(self.call("jobs.get", {"id": job["id"]})["progress"], terminal["progress"])

    def test_job_progress_rejects_fabricated_percent_and_invalid_values(self):
        base = {"phase": "download", "label": "Téléchargement", "completed": 10, "total": 20, "percent": 50}
        rejected = []
        def work():
            for replacement in ({"percent": 99}, {"completed": True}, {"total": 0},
                                {"completed": 21}, {"indeterminate": "yes"}, {"detail": "x" * 1001}, {"command": "id"}):
                try:
                    self.app.report_progress({**base, **replacement})
                except ValueError:
                    rejected.append(replacement)
            self.app.report_progress(base)
            return {}
        job = self.app.submit_job("test.progress", "test", {}, work)
        result = self.wait_job(job["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(rejected), 7)
        self.assertEqual(result["progress"]["percent"], 50)

    def test_network_actions_are_explicitly_blocked(self):
        for service in ("net_setup", "wireguard", "gcs_reactor"):
            operations = ("restart", "start", "stop") if service == "wireguard" else ("start", "stop")
            for operation in operations:
                self.assertCode("network_guard", f"service.{operation}", {"service":service})
        self.assertFalse(self.fake.actions)

    def test_only_restart_is_available_for_reviewed_network_services(self):
        for service in ("net_setup", "gcs_reactor"):
            job = self.call("service.restart", {"service": service})
            self.assertEqual(self.wait_job(job["id"])["status"], "succeeded")
        self.assertEqual(self.fake.actions, [("net_setup", "restart"), ("gcs_reactor", "restart")])

    def test_clean_exit_is_history_not_reported_as_crash(self):
        self.fake.pending_events=[{'Action':'die','timeNano':index,'Actor':{'ID':'a'*64,'Attributes':{'com.docker.compose.service':'gcs','exitCode':code}}} for index,code in enumerate(('0','137'),1)]
        with patch.object(self.app.health_history,'capture',return_value={'id':'b'*64}):
            self.call('status.get')
        events=[row for row in self.app.store.events() if row['kind']=='docker_event']
        self.assertEqual({row['details']['exit_code']:row['severity'] for row in events},{'0':'info','137':'error'})

    def test_portainer_restart_requires_confirmation_and_uses_serialized_job(self):
        self.assertCode('confirmation_required','portainer.restart',{})
        with patch.object(self.fake,'restart_portainer',create=True,return_value={'accounts_preserved':True}) as restart:
            job=self.call('portainer.restart',{'confirmed':True})
            self.assertEqual(self.wait_job(job['id'])['status'],'succeeded')
            restart.assert_called_once_with()

    def test_history_pagination_is_stable_when_new_events_arrive(self):
        for number in range(12):
            self.app.store.event('test',str(number),service='gcs' if number%2 else 'mosquitto')
        first=self.call('events.list',{'limit':3,'service':'gcs'})
        self.assertTrue(first['has_more'])
        self.app.store.event('test','new arrival',service='gcs')
        second=self.call('events.list',{'limit':3,'service':'gcs','before_id':first['next_before_id']})
        self.assertFalse(second['has_more'])
        self.assertFalse(set(row['id'] for row in first['events']) & set(row['id'] for row in second['events']))
        self.assertTrue(all(row['service']=='gcs' for row in second['events']))
        self.assertCode('invalid_params','events.list',{'before_id':True})

    def test_history_retention_bounds_count_and_removes_expired_events(self):
        with patch('aquahub_agent.store.MAX_EVENTS',5):
            for number in range(12): self.app.store.event('test',str(number))
            self.assertEqual(len(self.app.store.events(limit=100)),5)
            self.app.store.event('old','expired',at='2000-01-01T00:00:00.000Z')
        self.assertFalse(any(row['message']=='expired' for row in self.app.store.events(limit=100)))

    def test_active_job_stays_visible_after_more_than_twenty_recent_failed_requests(self):
        started=threading.Event();release=threading.Event()
        job=self.app.submit_job('test.blocking','test',{},lambda:(started.set(),release.wait(3),{})[-1])
        self.assertTrue(started.wait(1))
        try:
            for number in range(25):
                other,_=self.app.store.create_job('busy','test',{'attempt':number})
                self.app.store.set_job(other['id'],'failed',error={'code':'busy'})
            self.assertEqual(self.call('status.get')['active_job']['id'],job['id'])
        finally: release.set()
        self.wait_job(job['id'])

    def test_input_cannot_escape_closed_service_or_command_list(self):
        self.assertCode("invalid_service", "service.restart", {"service":"gcs; touch /tmp/pwn"})
        self.assertCode("invalid_params", "service.logs", {"service":"gcs", "tail":True})
        self.assertCode("invalid_params", "service.logs", {"service":"gcs", "tail":1001})
        self.assertCode("invalid_params", "service.restart", {"service":"gcs", "command":"rm"})
        self.assertCode("unknown_method", "system.exec", {"command":"id"})
        self.assertCode("invalid_params", "settings.save", {"theme":[]})
        self.assertFalse(self.fake.actions)

    def test_settings_validate_all_values_before_writing(self):
        self.call("settings.save", {"theme":"dark", "github_url":"https://github.com/aqua/hub"})
        for url in ("http://github.com/aqua/hub", "https://evil.example/aqua/hub", "https://user:secret@github.com/aqua/hub", "https://github.com/aqua/hub?token=secret", "https://github.com/aqua", "https://[bad"):
            self.assertCode("invalid_params", "settings.save", {"theme":"light", "github_url":url})
            self.assertEqual(self.call("settings.get")["theme"], "dark")

    def test_logs_and_diagnostics_are_filtered_and_private(self):
        self.assertNotIn("supersecret", self.call("service.logs", {"service":"gcs"})["text"])
        self.app.store.event("test", "token=hidden-token", details={"private_key":"private-blob"})
        result = self.call("diagnostics.export")
        exported = Path(result["path"])
        self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
        raw = exported.read_text()
        for secret in ("supersecret", "hidden-token", "private-blob"):
            self.assertNotIn(secret, raw)
        self.assertEqual(json.loads(raw)["version"], __version__)

    def test_unauthorized_uid_cannot_read_or_mutate(self):
        for method in ("status.get", "service.restart"):
            with self.assertRaises(ApiError) as caught:
                self.app.dispatch(method, {"service":"gcs"} if method.startswith("service.") else {}, RequestContext(os.getuid()+1,os.getpid()))
            self.assertEqual(caught.exception.code, "unauthorized")

    def test_second_agent_cannot_use_same_state(self):
        with self.assertRaises(ApiError) as caught:
            Application(self.config, docker=self.fake, policy=AuthPolicy(os.getuid(), privileged=False))
        self.assertEqual(caught.exception.code,"already_running")


class DurabilityAndSecurityTests(unittest.TestCase):
    def test_progress_migrates_existing_database_without_losing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aquahub.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,target TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,payload TEXT NOT NULL,result TEXT,error TEXT,idempotency_key TEXT UNIQUE,fingerprint TEXT NOT NULL)")
                db.execute("INSERT INTO jobs(id,kind,target,status,created_at,payload,fingerprint) VALUES('legacy','configure_drone','Aqua_Drone_5','succeeded','2026-01-01T00:00:00Z','{}','old-fingerprint')")
            store = Store(Path(directory))
            self.assertIsNone(store.job("legacy")["progress"])
            self.assertEqual(store.job("legacy")["status"], "succeeded")
            job, _ = store.create_job("configure_drone", "Aqua_Drone_5", {})
            store.set_job(job["id"], "running")
            progress = {"phase": "backup", "label": "Sauvegarde", "completed": 1, "total": 12, "percent": 8}
            store.set_job_progress(job["id"], progress)
            store.close()
            store = Store(Path(directory))
            self.assertEqual(store.interrupt_jobs(), 1)
            self.assertEqual(store.job(job["id"])["status"], "interrupted")
            self.assertEqual(store.job(job["id"])["progress"], progress)
            self.assertEqual(store.job("legacy")["status"], "succeeded")
            store.close()

    def test_inflight_jobs_are_never_replayed_or_marked_successful_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            job, _ = store.create_job("service.restart", "gcs", {}, "once")
            store.set_job(job["id"], "running")
            store.close()
            store = Store(Path(directory))
            self.assertEqual(store.interrupt_jobs(), 1)
            self.assertEqual(store.job(job["id"])["status"], "interrupted")
            self.assertEqual(store.interrupt_jobs(), 0)
            store.close()

    def test_event_dedupe_and_threadsafe_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            workers = [threading.Thread(target=lambda: [store.event("test", "same", dedupe=str(n)) for n in range(25)]) for _ in range(4)]
            for worker in workers: worker.start()
            for worker in workers: worker.join()
            self.assertEqual(len(store.events(limit=100)),25)
            store.close()

    def test_redaction_removes_keys_credentials_and_terminal_control(self):
        raw = ("\x1b[31mpassword='strange secret'\x1b[0m\nAuthorization: Bearer aaa.bbb.ccc\n"
               "PrivateKey = " + "A"*43 + "=\nhttps://operator:admin@example.org/a\n"
               "-----BEGIN RSA PRIVATE KEY-----\nSECRETMATERIAL\n-----END RSA PRIVATE KEY-----\x00")
        result = redact(raw)
        for secret in ("strange secret", "aaa.bbb.ccc", "operator:admin", "SECRETMATERIAL", "A"*43, "\x1b", "\x00"):
            self.assertNotIn(secret,result)
        self.assertEqual(safe_value({"password":"hidden"})["password"], "[MASQUÉ]")

    def test_root_mutation_requires_live_peer_and_policykit_result(self):
        policy = AuthPolicy(os.getuid(), privileged=True)
        context = RequestContext(os.getuid(),os.getpid(),process_start_time(os.getpid()))
        with patch("aquahub_agent.security.subprocess.run") as run:
            run.return_value.returncode = 1
            with self.assertRaises(ApiError):
                policy.authorize(context, True)
            argv = run.call_args.args[0]
            self.assertIn(f"{context.pid},{context.start_time},{context.uid}", argv)
            run.return_value.returncode = 0
            policy.authorize(context, True)
        with patch("aquahub_agent.security.subprocess.run") as run:
            with self.assertRaises(ApiError):
                policy.authorize(RequestContext(os.getuid(),os.getpid(),"wrong"),True)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
