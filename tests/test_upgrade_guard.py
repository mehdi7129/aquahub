from pathlib import Path
import fcntl
import importlib.util
import os
import tempfile
import threading
import time
import unittest

from aquahub_agent.core import Application, Config
from aquahub_agent.security import ApiError, AuthPolicy
from test_core import FakeDocker

SPEC=importlib.util.spec_from_file_location('upgrade_guard',Path(__file__).resolve().parents[1]/'installer/upgrade_guard.py')
upgrade=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class UpgradeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.state=Path(self.temporary.name)/'state'
        self.app=Application(Config(self.state/'sock',self.state,no_poll=True),docker=FakeDocker(),policy=AuthPolicy(os.getuid(),privileged=False))

    def tearDown(self):
        self.app.close()
        self.temporary.cleanup()

    def test_upgrade_cannot_stop_agent_during_a_live_job(self):
        entered=threading.Event(); release=threading.Event(); stopped=[]
        def work():
            entered.set()
            release.wait(5)
            return {'ok':True}
        job=self.app.submit_job('test','test',{},work)
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaisesRegex(RuntimeError,'en cours'):
                upgrade.guard(self.state,lambda:stopped.append(True))
            self.assertEqual(stopped,[])
        finally:
            release.set()
        deadline=time.monotonic()+2
        while self.app.store.job(job['id'])['status'] in ('queued','running') and time.monotonic()<deadline:
            time.sleep(.01)
        upgrade.guard(self.state,lambda:stopped.append(True))
        self.assertEqual(stopped,[True])

    def test_installer_lock_blocks_new_job_without_leaving_queued_record(self):
        descriptor=os.open(self.state/'maintenance.lock',os.O_RDWR)
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(ApiError) as caught:
                self.app.submit_job('test','test',{},lambda:self.fail('must not execute'),idempotency_key='upgrade-race')
            self.assertEqual(caught.exception.code,'maintenance')
            self.assertEqual(self.app.store.jobs()[0]['status'],'failed')
        finally:
            os.close(descriptor)
        self.assertTrue(self.app._mutation_lock.acquire(blocking=False))
        self.app._mutation_lock.release()

    def test_unresolved_legacy_job_blocks_upgrade_even_without_flock(self):
        self.app.store.create_job('legacy','drone',{})
        with self.assertRaisesRegex(RuntimeError,'pas terminée'):
            upgrade.guard(self.state,lambda:self.fail('must not stop'))

    def test_wireguard_intent_blocks_upgrade_even_after_its_deadline(self):
        for value in ({'schema': 1, 'until': 1}, {}, False):
            with self.subTest(value=value):
                self.app.store.setting_set('wireguard_pending', value)
                with self.assertRaisesRegex(RuntimeError, 'WireGuard'):
                    upgrade.guard(self.state, lambda:self.fail('must not stop'))
        self.app.store.setting_set('wireguard_pending', None)
        stopped=[]
        upgrade.guard(self.state, lambda:stopped.append(True))
        self.assertEqual(stopped, [True])
