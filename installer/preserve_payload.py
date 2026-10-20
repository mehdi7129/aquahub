# SPDX-License-Identifier: MIT
"""Preinst migration of the old offline kit out of dpkg-owned application files.

Application-only updates must retain a customer's private repair kit. This code
is embedded in the package preinst and runs only after the maintenance guard.
No supplier configuration, container, volume, service, or network is changed.
"""
from pathlib import Path
import errno
import hashlib
import os
import shutil
import stat
import tempfile


def _trusted_tree(root):
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise RuntimeError('Le kit hors ligne doit être un dossier ordinaire.')
    result = {}
    for path in [root, *root.rglob('*')]:
        info = path.lstat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise RuntimeError('Le kit hors ligne possède des permissions ou des fichiers inattendus. Il a été conservé.')
        if path.is_file():
            result[path.relative_to(root).as_posix()] = (path, info.st_size)
    return result


def _equal(left, right):
    if set(left) != set(right):
        return False
    for name, (path, size) in left.items():
        other, other_size = right[name]
        if size != other_size:
            return False
        with path.open('rb') as stream, other.open('rb') as copy:
            if hashlib.file_digest(stream, 'sha256').digest() != hashlib.file_digest(copy, 'sha256').digest():
                return False
    return True


def preserve_payload(source=Path('/usr/share/aquahub/offline'), destination=Path('/var/lib/aquahub/offline')):
    if not source.exists() and not source.is_symlink():
        return False
    original = _trusted_tree(source)
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError('Le dossier de conservation du kit hors ligne n’est pas protégé.')
    if destination.exists() or destination.is_symlink():
        if not _equal(original, _trusted_tree(destination)):
            raise RuntimeError('Deux kits hors ligne différents existent. Ils ont été conservés pour vérification.')
        # Resume an interrupted cross-filesystem migration after publishing its
        # fully verified copy, before dpkg can delete the former package files.
        shutil.rmtree(source)
        return True
    try:
        source.rename(destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        required = sum(size for _, size in original.values()) + 64 * 1024**2
        if shutil.disk_usage(parent).free < required:
            raise RuntimeError('L’espace disque est insuffisant pour conserver le kit hors ligne. Aucun kit n’a été supprimé.') from None
        stage = Path(tempfile.mkdtemp(prefix='.offline-preserve-', dir=parent))
        try:
            shutil.copytree(source, stage, dirs_exist_ok=True, copy_function=shutil.copy2)
            stage.chmod(source.stat().st_mode & 0o777)
            copied = _trusted_tree(stage)
            if not _equal(original, copied):
                raise RuntimeError('La copie de conservation du kit hors ligne n’a pas pu être vérifiée.')
            for path, _ in copied.values():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
            for directory in sorted([stage, *(path for path in stage.rglob('*') if path.is_dir())], key=lambda path: len(path.parts), reverse=True):
                descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            stage.rename(destination)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            shutil.rmtree(source)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return True


if __name__ == '__main__':
    preserve_payload()
