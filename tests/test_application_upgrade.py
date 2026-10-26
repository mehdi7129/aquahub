# SPDX-License-Identifier: MIT
from pathlib import Path
from contextlib import closing
import fcntl
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('application_upgrade', Path(__file__).resolve().parents[1]/'installer/application_upgrade.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class ApplicationUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.state = self.base/'agent'
        self.state.mkdir(mode=0o700)
        self.stage = self.state/'app-updates'
        self.stage.mkdir(mode=0o700)
        self.runtime = self.base/'runtime.py'
        self.runtime.write_text('__version__ = "0.1.2"\n')
        self.runtime.chmod(0o644)
        self.marker = self.base/'setup.complete'
        self.marker.write_text('0.1.2\n')
        self.marker.chmod(0o644)
        self.bundle = self.base/'supplier.env'
        self.bundle.write_bytes(b'existing unchanged supplier fixture')
        self.package = self.stage/'package.deb'
        self.package.write_bytes(b'fake archive, package execution is mocked')
        self.package.chmod(0o600)
        self.metadata = {'schema': 1, 'version': '0.2.0', 'bytes': self.package.stat().st_size,
                         'sha256': hashlib.sha256(self.package.read_bytes()).hexdigest()}
        self.pending = self.stage/'pending.json'
        self.pending.write_text(json.dumps(self.metadata))
        self.pending.chmod(0o600)
        self.installed_version = '0.1.2'
        self.package_fields = {'Package': 'aquahub-setup', 'Version': '0.2.0', 'Architecture': 'amd64', 'X-AquaHub-Update': 'application-only'}
        self.active = True
        self.calls = []
        self.fail_install = False
        self.fail_preflight = False
        self.runtime_mismatch = False
        with closing(sqlite3.connect(self.state/'aquahub.sqlite3')) as connection, connection:
            connection.execute('CREATE TABLE jobs (status TEXT)')
        (self.state/'aquahub.sqlite3').chmod(0o600)

    def tearDown(self):
        self.directory.cleanup()

    def run_command(self, arguments, **kwargs):
        self.calls.append(arguments)
        executable = Path(arguments[0]).name
        if executable == 'dpkg-deb':
            return self.package_fields[arguments[-1]]
        if executable == 'dpkg-query':
            return self.installed_version
        if executable == 'systemctl':
            if arguments[1] == 'stop':
                self.active = False
            elif arguments[1] == 'start':
                self.active = True
            elif arguments[1] == 'is-active' and not self.active:
                raise upgrade.UpgradeError('inactive fixture')
            return ''
        if executable == 'dpkg' and '--install' in arguments:
            if '--no-act' in arguments:
                self.assertTrue(self.active)
                if self.fail_preflight:
                    raise upgrade.UpgradeError('dependency failure fixture')
                return ''
            self.assertFalse(self.active)
            # A real dpkg preinst must be able to take the same maintenance
            # lock after the agent has stopped; otherwise the update deadlocks.
            descriptor = os.open(self.state/'maintenance.lock', os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            if self.fail_install:
                raise upgrade.UpgradeError('dpkg failure fixture')
            self.installed_version = '0.2.0'
            if not self.runtime_mismatch:
                self.runtime.write_text('__version__ = "0.2.0"\n')
            return ''
        if executable == 'dpkg' and '--compare-versions' in arguments:
            return ''
        self.fail('Unexpected system command: '+repr(arguments))

    def install(self):
        return upgrade.install(state=self.state, stage=self.stage, runtime_version=self.runtime,
                               setup_marker=self.marker, run=self.run_command)

    def status(self):
        return json.loads((self.stage/'status.json').read_text())

    def test_wireguard_intent_prevents_stopping_the_agent_even_when_time_elapsed(self):
        with closing(sqlite3.connect(self.state/'aquahub.sqlite3')) as connection, connection:
            connection.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
            connection.execute('INSERT INTO settings VALUES (?,?)', ('wireguard_pending', json.dumps({'schema': 1, 'until': 1})))
        with self.assertRaisesRegex(upgrade.UpgradeError, 'WireGuard'):
            upgrade.no_active_jobs(self.state)
        self.assertTrue(self.active)
        self.assertEqual(self.calls, [])
        with closing(sqlite3.connect(self.state/'aquahub.sqlite3')) as connection, connection:
            connection.execute('UPDATE settings SET value=?', ('null',))
        upgrade.no_active_jobs(self.state)

    def test_application_install_stops_only_own_agent_and_releases_lock_before_dpkg(self):
        self.assertTrue(self.install())
        self.assertEqual(self.status()['status'], 'installed')
        self.assertEqual(self.status()['version'], '0.2.0')
        self.assertEqual(self.marker.read_text(), '0.2.0\n')
        self.assertTrue(self.active)
        self.assertEqual(self.bundle.read_bytes(), b'existing unchanged supplier fixture')
        self.assertEqual((self.stage/'status.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(all('docker' not in ' '.join(call) for call in self.calls))
        self.assertTrue(all('setup-helper' not in ' '.join(call) for call in self.calls))
        for call in self.calls:
            if Path(call[0]).name == 'systemctl':
                self.assertEqual(call[-1], 'aquahub-agent.service')

    def test_failed_dpkg_restarts_original_agent_and_persists_failed_status(self):
        self.fail_install = True
        self.assertFalse(self.install())
        self.assertTrue(self.active)
        self.assertEqual(self.status()['status'], 'failed')
        self.assertEqual(self.marker.read_text(), '0.1.2\n')

    def test_dpkg_preflight_failure_keeps_running_agent_available(self):
        self.fail_preflight = True
        self.assertFalse(self.install())
        self.assertTrue(self.active)
        self.assertEqual(self.status()['status'], 'failed')
        self.assertFalse(any('stop' in call for call in self.calls))
        self.assertFalse(any('--force-confold' in call for call in self.calls))

    def test_tampered_package_fails_before_stopping_agent(self):
        self.package.write_bytes(b'tampered')
        self.assertFalse(self.install())
        self.assertEqual(self.status()['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.active)

    def test_full_offline_kit_cannot_be_installed_as_application_update(self):
        self.package_fields['X-AquaHub-Update'] = 'offline-kit'
        self.assertFalse(self.install())
        self.assertTrue(self.active)
        self.assertFalse(any('stop' in call for call in self.calls))

    def test_active_drone_or_bundle_job_prevents_installation(self):
        with closing(sqlite3.connect(self.state/'aquahub.sqlite3')) as connection, connection:
            connection.execute("INSERT INTO jobs VALUES ('running')")
        self.assertFalse(self.install())
        self.assertTrue(self.active)
        self.assertFalse(any('stop' in call for call in self.calls))

    def test_installed_runtime_must_match_package_before_success_is_reported(self):
        self.runtime_mismatch = True
        self.assertFalse(self.install())
        self.assertTrue(self.active)
        self.assertEqual(self.status()['status'], 'failed')
        self.assertEqual(self.marker.read_text(), '0.1.2\n')

    def test_metadata_symlink_is_rejected_before_any_system_command(self):
        original = self.pending.read_bytes()
        self.pending.unlink()
        other = self.base/'other.json'
        other.write_bytes(original)
        self.pending.symlink_to(other)
        self.assertFalse(self.install())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status()['status'], 'failed')

    def test_boolean_size_and_world_readable_stage_are_rejected(self):
        self.metadata['bytes'] = True
        self.pending.write_text(json.dumps(self.metadata))
        self.assertFalse(self.install())
        self.stage.chmod(0o755)
        with self.assertRaises(upgrade.UpgradeError):
            self.install()

    def test_maintenance_lock_wait_is_bounded(self):
        descriptor = os.open(self.state/'maintenance.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(upgrade.UpgradeError, 'en cours'):
                upgrade.acquire_maintenance(self.state, timeout=.01)
        finally:
            os.close(descriptor)

    def test_helper_refuses_arguments_even_when_root(self):
        with patch.object(upgrade.sys, 'argv', ['application-upgrade.py', '/tmp/untrusted.deb']), patch.object(upgrade.os, 'geteuid', return_value=0), patch.object(upgrade, 'install') as install:
            self.assertEqual(upgrade.main(), 1)
            install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
