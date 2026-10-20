#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Install a staged AquaHub application update in a separate systemd process.

The agent already authenticated the GitHub release and requested PolicyKit
authorization. This helper accepts no paths or command-line arguments and
checks the fixed, root-private staging area again before stopping the agent.
It never runs the offline setup or operates on Docker or the supplier bundle.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

STATE = Path('/var/lib/aquahub/agent')
STAGE = STATE/'app-updates'
RUNTIME_VERSION = Path('/usr/lib/aquahub/agent/aquahub_agent/__init__.py')
SETUP_MARKER = Path('/var/lib/aquahub/setup.complete')
SERVICE = 'aquahub-agent.service'
MAX_PACKAGE_BYTES = 256 * 1024**2
VERSION = re.compile(r'^[0-9]+(?:\.[0-9]+){1,3}(?:[+~.-][a-zA-Z0-9]+)*$')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/root', 'DEBIAN_FRONTEND': 'noninteractive'}


class UpgradeError(Exception):
    pass


def trusted_directory(path, *, private=False):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o022):
        raise UpgradeError('Le dossier de mise à jour AquaHub n’est pas protégé.')


def trusted_file(path, *, private=True):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o022):
        raise UpgradeError('Un fichier de mise à jour AquaHub n’est pas protégé.')
    return info


def atomic_json(path, value):
    descriptor, name = tempfile.mkstemp(prefix='.status-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def read_pending(stage):
    path = stage/'pending.json'
    info = trusted_file(path)
    if info.st_size > 16 * 1024:
        raise UpgradeError('Les métadonnées de mise à jour sont trop volumineuses.')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise UpgradeError('Les métadonnées de mise à jour sont invalides.') from None
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1 or not isinstance(value.get('version'), str) or not VERSION.fullmatch(value['version']):
        raise UpgradeError('La version à installer n’est pas valide.')
    if not isinstance(value.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['sha256']):
        raise UpgradeError('L’empreinte de mise à jour est invalide.')
    if type(value.get('bytes')) is not int or not 0 < value['bytes'] <= MAX_PACKAGE_BYTES:
        raise UpgradeError('La taille du paquet de mise à jour est invalide.')
    package = stage/'package.deb'
    if trusted_file(package).st_size != value['bytes']:
        raise UpgradeError('Le paquet de mise à jour ne possède pas la taille attendue.')
    with package.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != value['sha256']:
            raise UpgradeError('Le paquet de mise à jour ne correspond plus à son empreinte vérifiée.')
    return value


def command(arguments, *, output=False, log=None, timeout=90):
    result = subprocess.run(arguments, env=ENV, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if output else log or subprocess.DEVNULL,
                            stderr=subprocess.PIPE if output else log or subprocess.DEVNULL,
                            text=output, timeout=timeout)
    if result.returncode:
        raise UpgradeError('Une étape de mise à jour AquaHub a échoué. Consultez le journal local de mise à jour.')
    return result.stdout.strip() if output else ''


def verify_package(stage, metadata, run=command):
    package = str(stage/'package.deb')
    expected = {'Package': 'aquahub-setup', 'Version': metadata['version'], 'Architecture': 'amd64', 'X-AquaHub-Update': 'application-only'}
    for field, value in expected.items():
        if run(['/usr/bin/dpkg-deb', '--field', package, field], output=True) != value:
            raise UpgradeError('Ce fichier n’est pas une mise à jour compatible de l’application AquaHub.')
    installed = run(['/usr/bin/dpkg-query', '--show', '--showformat=${Version}', 'aquahub-setup'], output=True)
    run(['/usr/bin/dpkg', '--compare-versions', metadata['version'], 'gt', installed])


def acquire_maintenance(state, *, timeout=30):
    descriptor = os.open(state/'maintenance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise UpgradeError('Le verrou de maintenance AquaHub n’est pas protégé.')
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise UpgradeError('Une opération AquaHub est en cours. Réessayez après sa fin.') from None
            time.sleep(.1)


def no_active_jobs(state):
    database = state/'aquahub.sqlite3'
    if database.exists() or database.is_symlink():
        trusted_file(database, private=False)
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=5)) as connection:
            if connection.execute("SELECT count(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0]:
                raise UpgradeError('Une opération AquaHub n’est pas terminée. Attendez avant de mettre à jour l’application.')


def install(*, state=STATE, stage=STAGE, runtime_version=RUNTIME_VERSION, setup_marker=SETUP_MARKER, run=command):
    trusted_directory(state.parent)
    trusted_directory(state, private=True)
    trusted_directory(stage, private=True)
    version = ''
    def status(value, message):
        atomic_json(stage/'status.json', {'status': value, 'version': version, 'message': message,
                                        'updated_at': datetime.now(timezone.utc).isoformat()})
    descriptor = None
    stopped = False
    try:
        metadata = read_pending(stage)
        version = metadata['version']
        verify_package(stage, metadata, run)
        descriptor = acquire_maintenance(state)
        no_active_jobs(state)
        status('installing', 'Installation de la nouvelle version d’AquaHub…')
        log_path = stage/'installation.log'
        if log_path.exists() or log_path.is_symlink():
            trusted_file(log_path)
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(log_fd, 'a') as log:
            # Test the existing dependency/lock situation without executing
            # maintainer scripts, unpacking files or stopping a working agent.
            run(['/usr/bin/dpkg', '--no-act', '--install', str(stage/'package.deb')], log=log, timeout=90)
            stopped = True
            run(['/usr/bin/systemctl', 'stop', SERVICE], timeout=75)
            # The stopped agent cannot accept new work. dpkg's own preinst will
            # acquire this same lock, so release it before invoking dpkg.
            os.close(descriptor)
            descriptor = None
            run(['/usr/bin/dpkg', '--force-confold', '--install', str(stage/'package.deb')], log=log, timeout=1200)
        run(['/usr/bin/systemctl', 'start', SERVICE], timeout=75)
        run(['/usr/bin/systemctl', 'is-active', '--quiet', SERVICE])
        actual = run(['/usr/bin/dpkg-query', '--show', '--showformat=${Version}', 'aquahub-setup'], output=True)
        trusted_file(runtime_version, private=False)
        source = runtime_version.read_text(encoding='utf-8')
        if actual != version or not re.search(r'__version__\s*=\s*'+re.escape(json.dumps(version)), source):
            raise UpgradeError('La nouvelle version installée n’a pas pu être confirmée.')
        trusted_file(setup_marker, private=False)
        descriptor_marker, name = tempfile.mkstemp(prefix='.setup-complete-', dir=setup_marker.parent)
        try:
            with os.fdopen(descriptor_marker, 'w') as stream:
                stream.write(version+'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o644)
            os.replace(name, setup_marker)
        finally:
            Path(name).unlink(missing_ok=True)
        status('installed', 'AquaHub a été mis à jour. Fermez puis rouvrez l’application.')
        return True
    except (UpgradeError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
        if stopped:
            try:
                run(['/usr/bin/systemctl', 'start', SERVICE], timeout=75)
            except (UpgradeError, OSError, subprocess.SubprocessError):
                pass
        status('failed', 'La mise à jour d’AquaHub n’a pas pu être terminée. Les données du Hub sont conservées ; consultez le journal local.')
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    if len(sys.argv) != 1 or os.geteuid() != 0:
        print('AquaHub : cet assistant système ne reçoit aucun argument et nécessite une autorisation administrateur.', file=sys.stderr)
        return 1
    try:
        return 0 if install() else 1
    except (UpgradeError, OSError, ValueError, subprocess.SubprocessError) as error:
        # No stage contents, credentials, external command output or package
        # maintainer-script output is echoed into the graphical bridge.
        print('AquaHub : la préparation de la mise à jour n’est pas valide.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
