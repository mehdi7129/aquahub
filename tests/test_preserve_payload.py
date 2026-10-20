# SPDX-License-Identifier: MIT
from pathlib import Path
import errno
import importlib.util
import shutil
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('preserve_payload', Path(__file__).resolve().parents[1]/'installer/preserve_payload.py')
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class OfflineKitPreservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.source = self.base/'usr/offline'
        self.source.mkdir(parents=True, mode=0o755)
        (self.source/'repository').mkdir(mode=0o755)
        (self.source/'repository/package.deb').write_bytes(b'public dependency')
        (self.source/'repository/package.deb').chmod(0o644)
        (self.source/'bundle.tar.gz').write_bytes(b'private repair kit')
        (self.source/'bundle.tar.gz').chmod(0o600)
        self.destination = self.base/'var/offline'

    def tearDown(self):
        self.directory.cleanup()

    def assert_preserved(self):
        self.assertFalse(self.source.exists())
        self.assertEqual((self.destination/'bundle.tar.gz').read_bytes(), b'private repair kit')
        self.assertEqual((self.destination/'bundle.tar.gz').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.destination/'repository/package.deb').read_bytes(), b'public dependency')
        self.assertEqual((self.destination/'repository/package.deb').stat().st_mode & 0o777, 0o644)

    def test_same_filesystem_migration_preserves_private_modes_and_bytes(self):
        self.assertTrue(migration.preserve_payload(self.source, self.destination))
        self.assert_preserved()
        self.assertFalse(migration.preserve_payload(self.source, self.destination))

    def test_cross_filesystem_migration_verifies_copy_before_removing_source(self):
        rename = Path.rename
        def cross_device(path, destination):
            if path == self.source:
                raise OSError(errno.EXDEV, 'cross-device test')
            return rename(path, destination)
        with patch.object(Path, 'rename', cross_device):
            migration.preserve_payload(self.source, self.destination)
        self.assert_preserved()
        self.assertFalse(list(self.destination.parent.glob('.offline-preserve-*')))

    def test_low_space_cross_filesystem_migration_keeps_original(self):
        rename = Path.rename
        def cross_device(path, destination):
            if path == self.source:
                raise OSError(errno.EXDEV, 'cross-device test')
            return rename(path, destination)
        with patch.object(Path, 'rename', cross_device), patch.object(migration.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(10, 10, 0)):
            with self.assertRaisesRegex(RuntimeError, 'insuffisant'):
                migration.preserve_payload(self.source, self.destination)
        self.assertTrue((self.source/'bundle.tar.gz').exists())
        self.assertFalse(self.destination.exists())

    def test_resume_after_published_copy_keeps_exactly_one_verified_kit(self):
        self.destination.parent.mkdir(mode=0o755)
        shutil.copytree(self.source, self.destination)
        migration.preserve_payload(self.source, self.destination)
        self.assert_preserved()

    def test_ambiguous_existing_different_kit_keeps_both(self):
        self.destination.parent.mkdir(mode=0o755)
        shutil.copytree(self.source, self.destination)
        (self.destination/'bundle.tar.gz').write_bytes(b'different kit')
        with self.assertRaisesRegex(RuntimeError, 'différents'):
            migration.preserve_payload(self.source, self.destination)
        self.assertTrue(self.source.exists())
        self.assertTrue(self.destination.exists())

    def test_symlink_member_or_writable_file_cannot_enter_migration(self):
        (self.source/'external').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(RuntimeError, 'inattendus'):
            migration.preserve_payload(self.source, self.destination)
        (self.source/'external').unlink()
        (self.source/'bundle.tar.gz').chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, 'inattendus'):
            migration.preserve_payload(self.source, self.destination)
        self.assertTrue(self.source.exists())

    def test_destination_symlink_is_rejected_without_touching_source(self):
        self.destination.parent.mkdir(mode=0o755)
        self.destination.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'dossier ordinaire'):
            migration.preserve_payload(self.source, self.destination)
        self.assertTrue(self.source.exists())


if __name__ == '__main__':
    unittest.main()
