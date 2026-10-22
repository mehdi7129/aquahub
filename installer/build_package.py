#!/usr/bin/env python3
"""Build a self-contained Debian GNOME setup package; never install it here.

The payload is prepared separately on a trusted build workstation. Client PCs
need neither a compiler, Node, pip, nor access to a package mirror.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PAYLOAD = {"SHA256SUMS", "repo-key.gpg", "package-list.txt", "bundle.tar.gz", "images.tar.gz", "portainer.tar.gz", "kit.json", "repository/Packages.gz", "repository/Release", "repository/InRelease"}
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[+~.-][a-zA-Z0-9]+)*$")
CHECKSUM = re.compile(r"^([a-f0-9]{64}) [ *](.+)$")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            result.update(block)
    return result.hexdigest()


def validate_payload(payload: Path, private_kit: bool) -> dict:
    payload = payload.resolve(strict=True)
    if not payload.is_dir():
        raise ValueError("Le payload doit être un dossier.")
    files = {}
    for path in payload.rglob("*"):
        if path.is_symlink():
            raise ValueError("Les liens symboliques sont interdits dans le kit.")
        if not path.is_file() and not path.is_dir():
            raise ValueError("Le kit contient un fichier spécial interdit.")
        if path.is_file():
            files[path.relative_to(payload).as_posix()] = path
    if missing := REQUIRED_PAYLOAD - set(files):
        raise ValueError("Payload incomplet : " + ", ".join(sorted(missing)))
    expected = {}
    for row in files["SHA256SUMS"].read_text(encoding="utf-8").splitlines():
        match = CHECKSUM.fullmatch(row)
        if not match:
            raise ValueError("Format SHA256SUMS invalide.")
        checksum, name = match.groups()
        if name.startswith("./"):
            name = name[2:]
        parsed = PurePosixPath(name)
        if parsed.is_absolute() or ".." in parsed.parts or str(parsed) != name or "\\" in name or any(ord(c)<32 for c in name):
            raise ValueError("Chemin dangereux dans SHA256SUMS.")
        if name == "SHA256SUMS" or name in expected:
            raise ValueError("Entrée en double ou récursive dans SHA256SUMS.")
        expected[name] = checksum
    if set(expected) != set(files) - {"SHA256SUMS"}:
        raise ValueError("SHA256SUMS doit couvrir exactement tous les fichiers du kit.")
    for name, checksum in expected.items():
        if digest(files[name]) != checksum:
            raise ValueError(f"Le fichier {name} ne correspond pas à son empreinte.")
    metadata = json.loads(files["kit.json"].read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("kit.json doit être un objet JSON.")
    # Existing supplier archives contain WireGuard private keys. Never label such a
    # local test kit as redistributable or make its .deb world-readable.
    contains_credentials = False
    with tarfile.open(files["bundle.tar.gz"], "r:*") as archive:
        for member in archive:
            name = member.name.lower()
            if "wireguard" in name and name.endswith(".conf") or PurePosixPath(name).name == ".env" or name.endswith((".key", ".pem", ".p12", ".pfx")):
                contains_credentials = True
                break
            if member.isfile() and member.size <= 2*1024**2:
                stream = archive.extractfile(member)
                sample = stream.read(65536) if stream else b""
                if re.search(rb"(?i)(?:private[_-]?key|preshared[_-]?key)\s*[:=]|-----BEGIN [^-]*PRIVATE KEY-----", sample):
                    contains_credentials = True
                    break
    if contains_credentials and not private_kit:
        raise ValueError("Le bundle contient des configurations privées. Utiliser --private-kit pour un paquet local non redistribuable.")
    if metadata.get('private') is True and not private_kit:
        raise ValueError("Ce payload est déclaré privé. Utiliser --private-kit pour le conserver comme kit local non redistribuable.")
    return {"metadata": metadata, "private": private_kit or contains_credentials or metadata.get('private') is True, "files": len(files), "bytes": sum(path.stat().st_size for path in files.values())}


def write(path: Path, content: str, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def copy_sources(source: Path, destination: Path):
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache"))
    for path in destination.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)


def compile_binaries(package: Path):
    library = package / "usr/lib/aquahub"
    library.mkdir(parents=True, exist_ok=True)
    flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "gtk4"], text=True).split()
    common = ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2", "-Wl,-z,relro,-z,now"]
    subprocess.run([*common, str(ROOT/"installer/bootstrap.c"), *flags, "-o", str(library/"aquahub-bootstrap")], check=True)
    subprocess.run([*common, str(ROOT/"installer/setup_helper.c"), "-o", str(library/"setup-helper")], check=True)
    for filename in ("aquahub-bootstrap", "setup-helper"):
        (library/filename).chmod(0o755)


def populate(package: Path, payload: Path | None, version: str, info: dict, *, application_only=False):
    compile_binaries(package)
    copy_sources(ROOT/"agent", package/"usr/lib/aquahub/agent")
    copy_sources(ROOT/"desktop", package/"usr/lib/aquahub/desktop")
    for init in (package/"usr/lib/aquahub/agent/aquahub_agent/__init__.py", package/"usr/lib/aquahub/desktop/aquahub_desktop/__init__.py"):
        text = init.read_text()
        text = re.sub(r'__version__\s*=\s*"[^"]+"', '__version__ = ' + json.dumps(version), text)
        init.write_text(text)
    finish = ROOT/"installer/finish.py"
    if not finish.is_file():
        raise ValueError("installer/finish.py est requis avant construction.")
    finish_source = re.sub(r'^VERSION = "[^"]+"', 'VERSION = ' + json.dumps(version), finish.read_text(), flags=re.M)
    write(package/"usr/lib/aquahub/installer/finish.py", finish_source)
    upgrade_helper = ROOT/"installer/application_upgrade.py"
    if application_only and not upgrade_helper.is_file():
        raise ValueError("L’assistant indépendant de mise à jour de l’application est requis.")
    if upgrade_helper.is_file():
        write(package/"usr/lib/aquahub/application-upgrade.py", upgrade_helper.read_text())
    for filename in ("index.html", "app.js", "styles.css"):
        source = ROOT/"frontend/dist"/filename
        if not source.is_file():
            raise ValueError("Compiler l’interface avant de construire le paquet.")
        destination = package/"usr/share/aquahub/frontend"/filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o644)
    if not application_only:
        offline = package/"usr/share/aquahub/offline"
        shutil.copytree(payload, offline)
        offline.chmod(0o755)
        for path in offline.rglob("*"):
            relative = path.relative_to(offline)
            if path.is_dir():
                path.chmod(0o755)
            else:
                public = relative.parts[0] == "repository" or relative.as_posix() in {"repo-key.gpg", "package-list.txt", "SHA256SUMS"}
                path.chmod(0o644 if public else 0o600)
    write(package/"usr/bin/aquahub", """#!/bin/sh
if [ "${1-}" = "--setup" ]; then
    shift
    exec /usr/lib/aquahub/aquahub-bootstrap "$@"
fi
if [ ! -f /var/lib/aquahub/setup.complete ]; then
    exec /usr/lib/aquahub/aquahub-bootstrap "$@"
fi
export PYTHONPATH=/usr/lib/aquahub/desktop:/usr/lib/aquahub/agent
export PYTHONDONTWRITEBYTECODE=1
exec /usr/bin/python3 -m aquahub_desktop "$@"
""", 0o755)
    write(package/"usr/share/applications/com.aquadrone.aquahub.desktop", """[Desktop Entry]
Type=Application
Name=AquaHub
Comment=Supervision et maintenance Aqua Hub
Exec=/usr/bin/aquahub
Icon=com.aquadrone.aquahub
Terminal=false
Categories=System;
StartupNotify=true
StartupWMClass=Com.aquadrone.aquahub
""")
    # The first-run assistant is a separate GTK application. Keep it resolvable
    # by the desktop shell without adding a second entry to the applications menu.
    write(package/"usr/share/applications/com.aquadrone.aquahub.setup.desktop", """[Desktop Entry]
Type=Application
Name=AquaHub — Installation
Comment=Installation et réparation du poste Aqua Hub
Exec=/usr/bin/aquahub --setup
Icon=com.aquadrone.aquahub
Terminal=false
Categories=System;
StartupNotify=true
StartupWMClass=com.aquadrone.aquahub.setup
NoDisplay=true
""")
    write(package/"usr/share/icons/hicolor/scalable/apps/com.aquadrone.aquahub.svg", '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128"><rect width="128" height="128" rx="30" fill="#111"/><path d="M25 91 54 32h20l29 59H82l-6-14H51l-6 14zm33-31h12l-6-15z" fill="#fff"/></svg>\n')
    write(package/"usr/share/metainfo/com.aquadrone.aquahub.metainfo.xml", f"""<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
 <id>com.aquadrone.aquahub</id><name>AquaHub</name>
 <summary>Supervision et maintenance du bundle Aqua Hub</summary>
 <metadata_license>CC0-1.0</metadata_license><project_license>MIT</project_license>
 <description><p>Application locale de supervision, journaux, maintenance et configuration Aqua_Drones. Version de préproduction à qualifier.</p></description>
 <launchable type="desktop-id">com.aquadrone.aquahub.desktop</launchable>
 <provides><binary>aquahub</binary></provides>
 <releases><release version="{version}" date="2026-10-20" type="development"/></releases>
</component>
""")
    write(package/"usr/share/doc/aquahub-setup/copyright", (ROOT/"LICENSE").read_text(encoding="utf-8") + """

Scope and third-party components
-------------------------------
The MIT license above covers the AquaHub code and documentation in this project.
It does not relicense third-party dependencies, the supplier's Aqua Hub bundle,
container images, Docker, Portainer, or the Debian packages included in an offline
kit. Each of those components retains its original copyright and license terms.
Consult the component's own license and the Debian package copyright files in
/usr/share/doc/<package>/copyright. Redistributing an offline kit requires checking
the license and source-distribution obligations for every included component.
""")
    write(package/"usr/share/polkit-1/actions/com.aquadrone.aquahub.policy", """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN" "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
 <vendor>AquaHub</vendor>
 <action id="com.aquadrone.aquahub.install">
  <description>Installer ou réparer AquaHub et ses composants hors ligne</description>
  <message>Une autorisation administrateur est nécessaire pour installer AquaHub.</message>
  <defaults><allow_any>no</allow_any><allow_inactive>no</allow_inactive><allow_active>auth_admin_keep</allow_active></defaults>
  <annotate key="org.freedesktop.policykit.exec.path">/usr/lib/aquahub/setup-helper</annotate>
 </action>
 <action id="com.aquadrone.aquahub.maintain">
  <description>Maintenir les services AquaHub</description>
  <message>Une autorisation administrateur est nécessaire pour cette opération AquaHub.</message>
  <defaults><allow_any>no</allow_any><allow_inactive>no</allow_inactive><allow_active>auth_admin_keep</allow_active></defaults>
 </action>
</policyconfig>
""")
    installed_size = (sum(path.stat().st_size for path in package.rglob("*") if path.is_file())+1023)//1024
    dependencies = "libc6 (>= 2.41), libgtk-4-1 (>= 4.18)"
    description = " Contains a signed offline runtime repository and supplier payload."
    if application_only:
        dependencies += ", python3 (>= 3.13), python3-gi, python3-cairo, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-webkit2-4.1, ca-certificates, openssh-client, iproute2, polkitd, pkexec"
        description = " Application-only update; preserves the existing offline kit and supplier installation."
    write(package/"DEBIAN/control", f"""Package: aquahub-setup
Version: {version}
Architecture: amd64
Maintainer: AquaHub Development <support@localhost>
Section: admin
Priority: optional
Depends: {dependencies}
X-AquaHub-Update: {'application-only' if application_only else 'offline-kit'}
Installed-Size: {installed_size}
Description: AquaHub desktop application and local maintenance (preproduction)
 Local graphical supervision and maintenance of Aqua Hub services.
{description}
 Qualification is required before commercial distribution.
""")
    write(package/"DEBIAN/postinst", """#!/bin/sh
set -e
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database -q /usr/share/applications || true; fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true; fi
if [ -f /var/lib/aquahub/installer/agent-upgrade.pending ]; then
    systemctl daemon-reload
    systemctl enable aquahub-agent.service
    systemctl start aquahub-agent.service
    rm -f /var/lib/aquahub/installer/agent-upgrade.pending
elif [ -f /var/lib/aquahub/setup.complete ] && [ -f /etc/systemd/system/aquahub-agent.service ]; then
    systemctl daemon-reload
    systemctl enable --now aquahub-agent.service
fi
exit 0
""", 0o755)
    guard_source=(ROOT/'installer/upgrade_guard.py').read_text()
    preservation = ''
    prerequisite = ''
    if application_only:
        prerequisite = '''
if [ ! -f /var/lib/aquahub/setup.complete ]; then
    echo "AquaHub : ce paquet met à jour une installation existante. Utilisez le kit complet pour la première installation." >&2
    exit 1
fi
'''
        preservation = '''
/usr/bin/python3 -I - <<'AQUAHUB_PRESERVE_OFFLINE_KIT'
'''+(ROOT/'installer/preserve_payload.py').read_text()+'''
AQUAHUB_PRESERVE_OFFLINE_KIT
'''
    write(package/'DEBIAN/preinst', '''#!/bin/sh
set -e
case "${1-}" in
 upgrade|install)
'''+prerequisite+'''
 if [ -f /var/lib/aquahub/setup.complete ] && [ -x /usr/bin/python3 ]; then
 /usr/bin/python3 -I - <<'AQUAHUB_UPGRADE_GUARD'
'''+guard_source+'''
AQUAHUB_UPGRADE_GUARD
 fi
'''+preservation+''' ;;
esac
exit 0
''', 0o755)
    write(package/"DEBIAN/prerm", """#!/bin/sh
set -e
case "${1-}" in
 remove|deconfigure)
 if [ -d /var/lib/aquahub/agent ] && [ -x /usr/bin/python3 ]; then
 /usr/bin/python3 -I - <<'AQUAHUB_REMOVE_GUARD'
"""+guard_source+"""
AQUAHUB_REMOVE_GUARD
 fi ;;
esac
exit 0
""", 0o755)
    write(package/"DEBIAN/postrm", """#!/bin/sh
# Operator data, Docker, Portainer and the supplier bundle are deliberately preserved.
case "${1-}" in
 remove|purge)
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now aquahub-agent.service 2>/dev/null || true
        systemctl daemon-reload 2>/dev/null || true
    fi
    rm -f /var/lib/aquahub/installer/agent-upgrade.pending ;;
esac
exit 0
""", 0o755)


def build(payload: Path | None, output: Path, *, version="0.1.0", private_kit=False, application_only=False) -> dict:
    if not VERSION.fullmatch(version):
        raise ValueError("Numéro de version invalide.")
    if subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip() != "amd64":
        raise ValueError("Construire ce paquet sur Debian 13 amd64.")
    platform=dict(row.split('=',1) for row in Path('/etc/os-release').read_text().splitlines() if '=' in row)
    if platform.get('ID','').strip('"')!='debian' or platform.get('VERSION_ID','').strip('"')!='13':
        raise ValueError("Construire ce paquet sur Debian 13 amd64.")
    if application_only and (payload is not None or private_kit):
        raise ValueError("Une mise à jour de l’application ne doit inclure aucun payload ni kit privé.")
    if not application_only and payload is None:
        raise ValueError("Le kit complet requiert --payload ; utiliser --application-only pour une mise à jour de l’application.")
    info = {"private": False} if application_only else validate_payload(payload, private_kit)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("Le fichier de sortie existe déjà ; choisissez un nouveau nom.")
    with tempfile.TemporaryDirectory(prefix=".aquahub-package-", dir=output.parent) as directory:
        package = Path(directory)/"root"
        package.mkdir()
        populate(package,payload,version,info,application_only=application_only)
        unpublished=Path(directory)/'aquahub-setup.deb'
        subprocess.run(["dpkg-deb","--build","--root-owner-group","--threads-max=2","-Zzstd","-z3",str(package),str(unpublished)],check=True)
        unpublished.chmod(0o600 if info["private"] else 0o644)
        # Publish only a complete, permissioned artifact, without overwriting a
        # concurrent build. The staging directory is private and on this disk.
        os.link(unpublished,output)
    return {"path":str(output),"sha256":digest(output),"bytes":output.stat().st_size,
            "version":version,"private":info["private"],"kind":"application" if application_only else "offline-kit","stage":"preproduction"}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--version",default="0.1.0")
    parser.add_argument("--private-kit",action="store_true",help="Kit local contenant des configurations privées, non redistribuable")
    parser.add_argument("--application-only",action="store_true",help="Mise à jour publique de l’application, sans bundle ni images Docker")
    args=parser.parse_args(argv)
    try:
        print(json.dumps(build(args.payload,args.output,version=args.version,private_kit=args.private_kit,application_only=args.application_only),ensure_ascii=False,indent=2))
    except (ValueError,OSError,subprocess.CalledProcessError,tarfile.TarError) as error:
        parser.exit(1,f"AquaHub : construction refusée : {error}\n")


if __name__=="__main__":
    main()
