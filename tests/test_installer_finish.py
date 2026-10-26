import importlib.util
from contextlib import closing
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import hashlib
import sys
import sqlite3
import types
from unittest.mock import patch

MODULE = Path(__file__).parents[1] / "installer/finish.py"
spec = importlib.util.spec_from_file_location("aquahub_finish_test", MODULE)
finish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finish)


class BundleInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, extra=None):
        path = self.root / "bundle.tar.gz"
        with tarfile.open(path, "w:gz") as output:
            content = {"docker-compose.yml": b"services: {}", "deploy.py": b"original", "mavlink-config-generator/main.py": b"generator", "aqua-hub-images-amd64.tar": b"original-image-archive"}
            content.update(extra or {})
            for name, data in content.items():
                member = tarfile.TarInfo("supplier/aqua-hub-bundle/" + name)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        return path

    def test_publishes_complete_original_bundle_without_removing_supplier_files(self):
        output = finish.extract_bundle(self.archive(), self.root / "target")
        self.assertEqual((output / "deploy.py").read_bytes(), b"original")
        self.assertEqual((output / "aqua-hub-images-amd64.tar").read_bytes(), b"original-image-archive")
        self.assertEqual((output / "deploy.py").stat().st_mode & 0o777, 0o600)

    def test_existing_installation_is_adopted_not_overwritten(self):
        target = self.root / "existing"
        target.mkdir()
        (target / "docker-compose.yml").write_text("original-customer-data")
        finish.extract_bundle(self.archive(), target)
        self.assertEqual((target / "docker-compose.yml").read_text(), "original-customer-data")

    def test_rejects_traversal_before_any_file_is_published(self):
        with self.assertRaises(finish.SetupError):
            finish.extract_bundle(self.archive({"../../outside": b"bad"}), self.root / "target")
        self.assertFalse((self.root / "target").exists())
        self.assertFalse((self.root / "outside").exists())

    def test_rejects_archive_symlinks_and_cleans_stage(self):
        source = self.archive()
        with tarfile.open(source, "r:gz") as original:
            members = [(member, original.extractfile(member).read()) for member in original]
        with tarfile.open(source, "w:gz") as output:
            for member, data in members:
                output.addfile(member, io.BytesIO(data))
            link = tarfile.TarInfo("supplier/aqua-hub-bundle/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/shadow"
            output.addfile(link)
        with self.assertRaises(finish.SetupError):
            finish.extract_bundle(source, self.root / "target")
        self.assertFalse(list(self.root.glob('.aquahub-install-*')))

    def test_atomic_state_does_not_leave_partial_success_marker(self):
        marker = self.root / "setup.complete"
        finish.write_atomic(marker, "0.1.0\n", 0o644)
        self.assertEqual(marker.read_text(), "0.1.0\n")
        self.assertEqual(marker.stat().st_mode & 0o777, 0o644)
        self.assertFalse(list(self.root.glob('.aquahub-*')))

    def test_new_installation_cannot_complete_with_unhealthy_component(self):
        rows=[{'service':name,'running':True,'state':'running','health':'unhealthy' if name=='mosquitto' else None} for name in finish.SERVICES]
        def docker(args):
            return 'container' if args[0]=='ps' else '\n'.join(json.dumps(row) for row in rows)
        with patch.object(finish,'docker',docker):
            with self.assertRaisesRegex(finish.SetupError,'démarrage complet'):
                finish.service_readiness(True,timeout=0)
            result=finish.service_readiness(False,timeout=0)
        self.assertFalse(result['ready'])

    def test_startup_waits_for_real_healthchecks_and_keeps_missing_probes_unknown(self):
        probes=iter(['starting','healthy'])
        def docker(args):
            if args[0]=='ps': return 'container'
            health=next(probes)
            return '\n'.join(json.dumps({'service':name,'running':True,'state':'running','health':health if name in ('mosquitto','dmx_fleet_controller') else None}) for name in finish.SERVICES)
        with patch.object(finish,'docker',docker),patch.object(finish.time,'sleep'):
            result=finish.service_readiness(True)
        self.assertTrue(result['ready'])
        self.assertEqual(sum(row['health'] is None for row in result['services']),6)

    def test_fresh_unit_pins_original_compose_project_independently_of_directory_name(self):
        template='WorkingDirectory=__AQUA_HUB_BUNDLE_DIR__\nExecStart=/usr/bin/docker compose up -d\nExecStop=/usr/bin/docker compose down\n'
        rendered=finish.render_new_bundle_unit(template,Path('/opt/aqua-hub/bundle'))
        self.assertIn('WorkingDirectory=/opt/aqua-hub/bundle',rendered)
        self.assertEqual(rendered.count('--context default compose --project-name aqua-hub-bundle'),2)
        self.assertIn('compose up -d',template)


class RepairMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / 'agent'
        self.state.mkdir(mode=0o700)
        self.db = self.state / 'aquahub.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
            db.execute('CREATE TABLE jobs (status TEXT)')
        self.db.chmod(0o600)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_or_cleared_intent_allows_a_read_only_precheck(self):
        finish.check_agent_maintenance(self.state)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO settings VALUES (?, ?)', ('wireguard_pending', 'null'))
        finish.check_agent_maintenance(self.state)

    def test_elapsed_corrupt_or_replaced_intent_never_permits_repair(self):
        for value in ('{"schema":1,"until":1}', '{}', 'false', 'corrupt-json'):
            with self.subTest(value=value):
                with closing(sqlite3.connect(self.db)) as db, db:
                    db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', ('wireguard_pending', value))
                with self.assertRaises(finish.SetupError):
                    finish.check_agent_maintenance(self.state)

    def test_legacy_running_job_and_unsafe_database_block_precheck(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO jobs VALUES ('running')")
        with self.assertRaises(finish.SetupError):
            finish.check_agent_maintenance(self.state)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DELETE FROM jobs')
        self.db.chmod(0o666)
        with self.assertRaises(finish.SetupError):
            finish.check_agent_maintenance(self.state)

    def test_helper_precheck_mode_and_finish_stop_before_any_system_command(self):
        with patch.object(finish.os, 'geteuid', return_value=0), \
                patch.object(finish, 'check_agent_maintenance', side_effect=finish.SetupError('WireGuard fixture pending')), \
                patch.object(finish, 'run') as run, patch.object(finish, 'progress'):
            self.assertEqual(finish.main(['--check-maintenance']), 1)
            with self.assertRaises(finish.SetupError):
                finish.finish(Path('/fixture-not-read'), 1000)
            run.assert_not_called()


class ImageIdentityGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.archive=Path(self.temp.name)/'images.tar.gz'
        self.archive.write_bytes(b'authenticated archive fixture')
        self.config='sha256:'+'a'*64
        self.manifest='sha256:'+'b'*64
        self.reference='docker.io/aqua-hub/gcs:latest'
        self.proof={'config_digest':self.config,'manifest_digests':[self.manifest],'index_digests':[],
                    'identities':[self.config,self.manifest],'platform':{'os':'linux','architecture':'amd64'}}
        self.module=types.ModuleType('aquahub_agent.image_identity')
        self.module.canonical_reference=lambda value: value if value.startswith('docker.io/') else 'docker.io/'+value
        self.module.archive_identities=lambda archive:{self.reference:self.proof}
        self.record={'reference':'aqua-hub/gcs:latest','image_id':self.config,
                     'archive_sha256':hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                     'archive_identity':self.proof}

    def tearDown(self):self.temp.cleanup()

    def test_classic_and_containerd_ids_are_accepted_only_for_proven_reference(self):
        with patch.dict(sys.modules,{'aquahub_agent.image_identity':self.module}),patch.object(finish,'verify_resource'):
            proofs=finish.verified_archive_identities(self.archive,[self.record])
            self.assertTrue(finish.image_matches_archive(self.record['reference'],self.config,proofs))
            self.assertTrue(finish.image_matches_archive(self.record['reference'],self.manifest,proofs))
            self.assertFalse(finish.image_matches_archive(self.record['reference'],'sha256:'+'c'*64,proofs))
            self.assertFalse(finish.image_matches_archive('aqua-hub/other:latest',self.manifest,proofs))

    def test_metadata_cannot_add_arbitrary_runtime_identity(self):
        tampered={**self.record,'archive_identity':{**self.proof,'identities':[self.config,'sha256:'+'c'*64]}}
        with patch.dict(sys.modules,{'aquahub_agent.image_identity':self.module}),patch.object(finish,'verify_resource'):
            with self.assertRaises(finish.SetupError):finish.verified_archive_identities(self.archive,[tampered])

    def test_archive_hash_is_checked_before_any_proof_is_accepted(self):
        with patch.dict(sys.modules,{'aquahub_agent.image_identity':self.module}),patch.object(finish,'verify_resource',side_effect=finish.SetupError('hash mismatch')):
            with self.assertRaises(finish.SetupError):finish.verified_archive_identities(self.archive,[self.record])

    def test_mismatched_source_identity_duplicate_reference_and_platform_fail(self):
        with patch.dict(sys.modules,{'aquahub_agent.image_identity':self.module}),patch.object(finish,'verify_resource'):
            with self.assertRaises(finish.SetupError):finish.verified_archive_identities(self.archive,[{**self.record,'image_id':'sha256:'+'c'*64}])
            with self.assertRaises(finish.SetupError):finish.verified_archive_identities(self.archive,[self.record,self.record])
            self.proof['platform']={'os':'linux','architecture':'arm64'}
            with self.assertRaises(finish.SetupError):finish.verified_archive_identities(self.archive,[self.record])

    def test_retry_reuses_only_an_already_verified_image(self):
        with patch.dict(sys.modules,{'aquahub_agent.image_identity':self.module}),patch.object(finish,'verified_archive_identities',return_value={self.reference:self.proof}),patch.object(finish,'docker',return_value=self.manifest) as docker:
            result=finish.ensure_image_archive(self.archive,[self.record])
            self.assertEqual(result[self.record['reference']],self.manifest)
            self.assertFalse(any(call.args[0][1]=='load' for call in docker.call_args_list))

    def test_missing_or_mismatched_image_loads_then_verifies_again(self):
        with patch.dict(sys.modules,{'aquahub_agent.image_identity':self.module}),patch.object(finish,'verified_archive_identities',return_value={self.reference:self.proof}),patch.object(finish,'docker',side_effect=['sha256:'+'c'*64,'loaded',self.manifest]) as docker:
            self.assertEqual(finish.ensure_image_archive(self.archive,[self.record])[self.record['reference']],self.manifest)
            self.assertEqual(docker.call_args_list[1].args[0][:2],['image','load'])


if __name__ == "__main__":
    unittest.main()
