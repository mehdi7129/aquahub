"""Code embedded in preinst: serialize package replacement with live jobs."""
from pathlib import Path
import fcntl
import os
import sqlite3
import subprocess
import sys


def guard(state=Path('/var/lib/aquahub/agent'), stop=None):
    if not state.is_dir():
        return
    if state.is_symlink() or state.stat().st_uid != os.geteuid():
        raise RuntimeError('Le répertoire AquaHub possède des permissions inattendues.')
    descriptor = os.open(state/'maintenance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Une opération AquaHub est en cours. Attendez sa fin puis relancez l’installation.') from None
        database = state/'aquahub.sqlite3'
        if database.is_symlink():
            raise RuntimeError('La base AquaHub ne doit pas être un lien symbolique.')
        if database.is_file():
            with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=5) as connection:
                if connection.execute("SELECT count(*) FROM jobs WHERE status IN ('running','queued')").fetchone()[0]:
                    raise RuntimeError('Une opération AquaHub n’est pas terminée. Vérifiez son état dans l’application avant de relancer l’installation.')
        if stop is not None:
            stop()
        else:
            active = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'aquahub-agent.service']).returncode == 0
            if active:
                subprocess.run(['/usr/bin/systemctl', 'stop', 'aquahub-agent.service'], check=True, timeout=75)
                marker=state.parent/'installer/agent-upgrade.pending'
                marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                marker.write_text('restart AquaHub agent after package configuration\n')
                marker.chmod(0o600)
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    try:
        guard()
    except (RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError) as error:
        print('AquaHub : '+str(error), file=sys.stderr)
        raise SystemExit(1)
