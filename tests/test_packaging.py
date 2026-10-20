"""Package construction and privilege-boundary tests; never installs a package."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("aquahub_build_package",ROOT/"installer/build_package.py")
builder=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def rehash(payload:Path):
    lines=[f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(payload).as_posix()}" for path in sorted(payload.rglob("*")) if path.is_file() and path.name!="SHA256SUMS"]
    (payload/"SHA256SUMS").write_text("\n".join(lines)+"\n")


def fixture_payload(base:Path,private=False)->Path:
    payload=base/"payload"
    (payload/"repository").mkdir(parents=True)
    for name in builder.REQUIRED_PAYLOAD-{"SHA256SUMS","bundle.tar.gz"}:
        target=payload/name
        target.write_text('{"schema":1}' if name=="kit.json" else "test fixture, never install\n")
    with tarfile.open(payload/"bundle.tar.gz","w:gz") as archive:
        info=tarfile.TarInfo("aqua-hub/config/wireguard/hub/wg0.conf" if private else "aqua-hub/README.txt")
        content=b"private test data" if private else b"test readme"
        info.size=len(content)
        archive.addfile(info,io.BytesIO(content))
    rehash(payload)
    return payload


def fixture_source(base:Path)->Path:
    source=base/'source'
    (source/'installer').mkdir(parents=True)
    shutil.copyfile(ROOT/'LICENSE',source/'LICENSE')
    for filename in ('bootstrap.c','setup_helper.c','finish.py','upgrade_guard.py','preserve_payload.py','application_upgrade.py'):
        shutil.copyfile(ROOT/'installer'/filename,source/'installer'/filename)
    for module in ('agent','desktop'):
        package=source/module/('aquahub_agent' if module=='agent' else 'aquahub_desktop')
        package.mkdir(parents=True)
        (package/'__init__.py').write_text('__version__ = "0.0.0"\n')
    (source/'frontend/dist').mkdir(parents=True)
    for filename in ('index.html','app.js','styles.css'):
        (source/'frontend/dist'/filename).write_text('fixture')
    return source


class PayloadValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.base=Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_payload_integrity_covers_every_file_exactly(self):
        payload=fixture_payload(self.base)
        result=builder.validate_payload(payload,False)
        self.assertFalse(result["private"])
        (payload/"repository/Packages.gz").write_text("tampered")
        with self.assertRaisesRegex(ValueError,"empreinte"):
            builder.validate_payload(payload,False)

    def test_unhashed_extra_files_are_rejected(self):
        payload=fixture_payload(self.base)
        (payload/"surprise.sh").write_text("not part of manifest")
        with self.assertRaisesRegex(ValueError,"exactement"):
            builder.validate_payload(payload,False)

    def test_private_supplier_bundle_requires_explicit_private_kit(self):
        payload=fixture_payload(self.base,private=True)
        with self.assertRaisesRegex(ValueError,"private-kit"):
            builder.validate_payload(payload,False)
        self.assertTrue(builder.validate_payload(payload,True)["private"])

    def test_declared_private_kit_cannot_be_silently_reclassified_public(self):
        payload=fixture_payload(self.base)
        (payload/'kit.json').write_text(json.dumps({'schema':1,'private':True}))
        rehash(payload)
        with self.assertRaisesRegex(ValueError,'déclaré privé'):
            builder.validate_payload(payload,False)
        self.assertTrue(builder.validate_payload(payload,True)['private'])

    def test_symlinks_and_checksum_traversal_are_rejected(self):
        payload=fixture_payload(self.base)
        (payload/"link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(ValueError,"symboliques"):
            builder.validate_payload(payload,True)
        (payload/"link").unlink()
        with (payload/"SHA256SUMS").open("a") as stream:
            stream.write("0"*64+"  ../outside\n")
        with self.assertRaisesRegex(ValueError,"dangereux"):
            builder.validate_payload(payload,True)

    def test_version_cannot_inject_control_file_or_shell(self):
        for version in ("1.0\nDepends: evil", "$(id)", "1.0;command"):
            with self.assertRaisesRegex(ValueError,"version"):
                builder.build(self.base/"nonexistent",self.base/"unused.deb",version=version)


@unittest.skipUnless(shutil.which("cc") and shutil.which("pkg-config") and shutil.which("dpkg-deb"),"Debian build tools unavailable")
class DebianPackageTests(unittest.TestCase):
    def test_public_application_update_has_no_supplier_payload_and_preserves_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            output=base/'aquahub-setup_0.2.0_amd64.deb'
            source=fixture_source(base)
            with patch.object(builder,'ROOT',source):
                result=builder.build(None,output,version='0.2.0',application_only=True)
            self.assertFalse(result['private'])
            self.assertEqual(result['kind'],'application')
            self.assertEqual(output.stat().st_mode & 0o777,0o644)
            self.assertLess(result['bytes'],20*1024**2)
            unpacked=base/'unpacked'
            subprocess.run(['dpkg-deb','-x',str(output),str(unpacked)],check=True)
            self.assertFalse((unpacked/'usr/share/aquahub/offline').exists())
            self.assertFalse(list(unpacked.rglob('*.tar.gz')))
            self.assertTrue((unpacked/'usr/lib/aquahub/application-upgrade.py').is_file())
            launcher=(unpacked/'usr/bin/aquahub').read_text()
            self.assertIn('if [ ! -f /var/lib/aquahub/setup.complete ]; then',launcher)
            self.assertNotIn('$(cat /var/lib/aquahub/setup.complete)',launcher)
            self.assertIn('__version__ = "0.2.0"',(unpacked/'usr/lib/aquahub/agent/aquahub_agent/__init__.py').read_text())
            kind=subprocess.check_output(['dpkg-deb','-f',str(output),'X-AquaHub-Update'],text=True).strip()
            self.assertEqual(kind,'application-only')
            dependencies=subprocess.check_output(['dpkg-deb','-f',str(output),'Depends'],text=True)
            for dependency in ('python3-gi','gir1.2-gtk-3.0','gir1.2-webkit2-4.1','pkexec'):
                self.assertIn(dependency,dependencies)
            self.assertNotIn('docker',dependencies)
            control=base/'control'
            subprocess.run(['dpkg-deb','-e',str(output),str(control)],check=True)
            preinst=(control/'preinst').read_text()
            self.assertIn('AQUAHUB_PRESERVE_OFFLINE_KIT',preinst)
            self.assertIn("Path('/var/lib/aquahub/offline')",preinst)
            self.assertLess(preinst.index('AQUAHUB_UPGRADE_GUARD'),preinst.index('AQUAHUB_PRESERVE_OFFLINE_KIT'))
            for name in ('preinst','postinst','prerm','postrm'):
                subprocess.run(['sh','-n',str(control/name)],check=True)

    def test_public_application_update_cannot_accidentally_embed_private_kit(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            with self.assertRaisesRegex(ValueError,'aucun payload'):
                builder.build(base/'payload',base/'no.deb',version='0.2.0',application_only=True)
            with self.assertRaisesRegex(ValueError,'aucun payload'):
                builder.build(None,base/'no.deb',version='0.2.0',application_only=True,private_kit=True)

    def test_build_extract_permissions_and_maintainer_script_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            payload=fixture_payload(base,private=True)
            source=base/"source"
            (source/"installer").mkdir(parents=True)
            shutil.copyfile(ROOT/"LICENSE",source/"LICENSE")
            for filename in ("bootstrap.c","setup_helper.c"):
                shutil.copyfile(ROOT/"installer"/filename,source/"installer"/filename)
            shutil.copyfile(ROOT/'installer/upgrade_guard.py',source/'installer/upgrade_guard.py')
            (source/"installer/finish.py").write_text("raise SystemExit(0)\n")
            for module in ("agent","desktop"):
                (source/module/"__pycache__").mkdir(parents=True)
                (source/module/"example.py").write_text("# fixture\n")
                (source/module/"__pycache__/example.pyc").write_bytes(b"not distributed")
                package=source/module/('aquahub_agent' if module=='agent' else 'aquahub_desktop')
                package.mkdir()
                (package/'__init__.py').write_text('__version__ = "0.0.0"\n')
            (source/"frontend/dist").mkdir(parents=True)
            for filename in ("index.html","app.js","styles.css"):
                (source/"frontend/dist"/filename).write_text("fixture")
            output=base/"aquahub-setup.deb"
            with patch.object(builder,"ROOT",source):
                result=builder.build(payload,output,private_kit=True)
            self.assertEqual(output.stat().st_mode&0o777,0o600)
            self.assertTrue(result["private"])
            self.assertEqual(subprocess.check_output(["dpkg-deb","-f",str(output),"Architecture"],text=True).strip(),"amd64")
            self.assertEqual(subprocess.check_output(["dpkg-deb","-f",str(output),"Package"],text=True).strip(),"aquahub-setup")
            unpacked=base/"unpacked"
            subprocess.run(["dpkg-deb","-x",str(output),str(unpacked)],check=True)
            self.assertEqual((unpacked/"usr/share/aquahub/offline/bundle.tar.gz").stat().st_mode&0o777,0o600)
            self.assertEqual((unpacked/"usr/share/aquahub/offline/repo-key.gpg").stat().st_mode&0o777,0o644)
            self.assertFalse(list(unpacked.rglob("*.pyc")))
            copyright_notice=(unpacked/"usr/share/doc/aquahub-setup/copyright").read_text(encoding="utf-8")
            self.assertIn((ROOT/"LICENSE").read_text(encoding="utf-8"),copyright_notice)
            self.assertIn("It does not relicense third-party dependencies",copyright_notice)
            self.assertIn("Each of those components retains its original copyright and license terms.",copyright_notice)
            metadata=ET.parse(unpacked/"usr/share/metainfo/com.aquadrone.aquahub.metainfo.xml")
            self.assertEqual(metadata.findtext("project_license"),"MIT")
            self.assertIn('__version__ = "0.1.0"', (unpacked/'usr/lib/aquahub/agent/aquahub_agent/__init__.py').read_text())
            control=base/"control"
            subprocess.run(["dpkg-deb","-e",str(output),str(control)],check=True)
            postinst=(control/"postinst").read_text()
            for forbidden in ("apt-get", "docker ", "systemctl start", "finish.py"):
                if forbidden != 'systemctl start':
                    self.assertNotIn(forbidden,postinst)
            self.assertIn('systemctl start aquahub-agent.service',postinst)
            self.assertIn('systemctl enable --now aquahub-agent.service',postinst)
            self.assertNotIn('systemctl start docker',postinst)
            self.assertIn('maintenance.lock',(control/'preinst').read_text())
            prerm=(control/"prerm").read_text()
            self.assertIn("aquahub-agent.service",prerm)
            self.assertNotIn("stop docker",prerm)
            self.assertIn('systemctl disable --now aquahub-agent.service',(control/'postrm').read_text())
            # No setuid bit: all elevation requires the named PolicyKit action.
            helper=unpacked/"usr/lib/aquahub/setup-helper"
            self.assertEqual(helper.stat().st_mode&0o7777,0o755)
            refusal=subprocess.run([str(helper),"unexpected-argument"],capture_output=True,text=True)
            self.assertNotEqual(refusal.returncode,0)
            self.assertIn("n’accepte pas",refusal.stderr)
            if os.geteuid()!=0:
                refusal=subprocess.run([str(helper)],capture_output=True,text=True)
                self.assertNotEqual(refusal.returncode,0)
                self.assertIn("autorisation administrateur",refusal.stderr)
            desktop=unpacked/"usr/share/applications/com.aquadrone.aquahub.desktop"
            if shutil.which("desktop-file-validate"):
                subprocess.run(["desktop-file-validate",str(desktop)],check=True)


if __name__=="__main__":
    unittest.main()
