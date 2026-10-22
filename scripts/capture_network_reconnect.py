#!/usr/bin/env python3
"""Bounded private Ethernet incident capture; never changes network settings.

Developer utility. The operator does not need to run a terminal command.
No configuration is changed, no service restarted and no drone command sent.
Optional probes open bounded TCP/DNS/HTTPS connections only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import itertools
import json
import os
from pathlib import Path
import signal
import selectors
import socket
import stat
import subprocess
import time

COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
MAX_COMMAND_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 64 * 1024**2
MAX_FINGERPRINT_PATHS = 1024
SERVICES = ("NetworkManager.service", "aqua-hub.service", "aquahub-agent.service")


def now():
    return datetime.now(timezone.utc).isoformat()


def command(arguments, timeout=3):
    process = None
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    outcome = None
    try:
        deadline = time.monotonic() + timeout
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=COMMAND_ENV,
            start_new_session=True)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() and outcome is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                break
            for key, _events in selector.select(remaining):
                name = key.data
                limit = MAX_COMMAND_BYTES if name == "stdout" else 4096
                block = os.read(key.fileobj.fileno(), min(65536, limit - len(buffers[name]) + 1))
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                available = limit - len(buffers[name])
                buffers[name].extend(block[:available])
                if len(block) > available:
                    outcome = "truncated"
                    break
        if outcome is None:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                outcome = "timeout"
        if outcome is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        return {"status": outcome or ("ok" if process.returncode == 0 else "failed"),
                "returncode": process.returncode,
                "stdout": buffers["stdout"].decode("utf-8", "replace"),
                "stderr": buffers["stderr"].decode("utf-8", "replace"),
                "truncated": outcome == "truncated"}
    except OSError:
        return {"status": "unavailable"}
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            process.stdout.close()
            process.stderr.close()


def read_regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Un fichier de diagnostic attendu n’est pas régulier.")
        return stream.read(limit), info


def json_file(path, value):
    raw = json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def physical_links():
    links = {}
    for entry in Path("/sys/class/net").iterdir():
        if not (entry / "device").exists():
            continue
        values = {}
        for field in ("carrier", "operstate", "address"):
            try:
                values[field] = read_regular(entry / field, 256)[0].decode("utf-8", "replace").strip()
            except (OSError, ValueError):
                values[field] = None
        values["wireless"] = (entry / "wireless").exists()
        driver = entry / "device/driver"
        values["driver"] = driver.resolve().name if driver.exists() else None
        links[entry.name] = values
    return links


def resolvers():
    result = {}
    for name in ("/etc/resolv.conf", "/run/NetworkManager/resolv.conf"):
        path = Path(name)
        try:
            target = path.resolve(strict=True)
            raw, _info = read_regular(target, 65537)
            result[name] = {"target": str(target), "content": raw[:65536].decode("utf-8", "replace"),
                            "truncated": len(raw) > 65536}
        except (OSError, ValueError):
            result[name] = {"unavailable": True}
    return result


def fingerprints(bundle):
    fixed = [Path("/etc/NetworkManager/NetworkManager.conf")]
    directory = Path("/etc/NetworkManager/system-connections")
    iterators = [iter(fixed), directory.glob("*.nmconnection")]
    if bundle:
        iterators.extend([(bundle / name for name in ("docker-compose.yml", ".env", "install.sh", "deploy.py")),
            (bundle / "config").rglob("*"), (bundle / "mavlink-config-generator").rglob("*.py")])
    result = {}
    for index, path in enumerate(itertools.chain.from_iterable(iterators)):
        if index >= MAX_FINGERPRINT_PATHS:
            result["$scan_limit"] = True
            break
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > 4 * 1024**2:
                result[str(path)] = {"too_large": True}
                continue
            content, info = read_regular(path, 4 * 1024**2 + 1)
            if len(content) > 4 * 1024**2:
                result[str(path)] = {"too_large": True}
                continue
            result[str(path)] = {"sha256": hashlib.sha256(content).hexdigest(),
                "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid}
        except (OSError, ValueError):
            result[str(path)] = {"unavailable": True}
    return result


def compare_fingerprints(before, after):
    if before != after:
        return False
    if "$scan_limit" in before or any(isinstance(value, dict) and
            (value.get("unavailable") or value.get("too_large")) for value in before.values()):
        return None
    return True


def connectivity_probe(drone_number):
    result = {}
    destinations = {"internet_tcp": ("1.1.1.1", 443)}
    if drone_number is not None:
        # Existing bundle profile: only observe reachability, never assign it.
        destinations["drone_tcp"] = (f"192.168.{200 + drone_number}.2", 22)
    for name, destination in destinations.items():
        start = time.monotonic()
        try:
            with socket.create_connection(destination, timeout=1):
                result[name] = {"reachable": True}
        except OSError:
            result[name] = {"reachable": False}
        result[name]["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    result["dns"] = command(["getent", "ahostsv4", "www.debian.org"], timeout=2)
    result["https"] = command(["curl", "--disable", "--noproxy", "*", "--silent", "--show-error", "--head",
        "--connect-timeout", "1", "--max-time", "2", "https://www.debian.org/"], timeout=3)
    return result


def snapshot(probe=False, drone_number=None, journal_since=None):
    calls = {
        "nm_general": ["nmcli", "-t", "-f", "STATE,CONNECTIVITY", "general"],
        "nm_devices": ["nmcli", "-t", "device", "status"],
        "nm_details": ["nmcli", "device", "show"],
        "links": ["ip", "-j", "-d", "link", "show"],
        "addresses": ["ip", "-j", "-d", "address", "show"],
        "routes": ["ip", "-j", "route", "show", "table", "all"],
        "routes6": ["ip", "-j", "-6", "route", "show", "table", "all"],
        "rules4": ["ip", "-j", "rule", "show"],
        "rules6": ["ip", "-j", "-6", "rule", "show"],
        "services": ["systemctl", "show", "--property=Id,ActiveState,SubState,ActiveEnterTimestamp", *SERVICES],
    }
    if journal_since is not None:
        common = ["--since", journal_since, "--no-pager", "-o", "json", "-n", "200"]
        calls["networkmanager_journal"] = ["journalctl", "-b", "-u", "NetworkManager.service", *common]
        calls["kernel_journal"] = ["journalctl", "-b", "-k", *common]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {name: pool.submit(command, args) for name, args in calls.items()}
        results = {name: future.result() for name, future in futures.items()}
    return {"time": now(), "physical_links": physical_links(), "resolvers": resolvers(),
            "observations": results, "journal_since": journal_since,
            "probes": connectivity_probe(drone_number) if probe else None}


def private_destination(parent):
    parent = parent.expanduser().resolve(strict=True)
    info = parent.stat()
    if not parent.is_dir() or info.st_uid not in (os.getuid(), 0) or info.st_mode & 0o077:
        raise ValueError("La destination doit être un dossier privé existant (mode 0700).")
    destination = parent / ("capture-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + str(os.getpid()))
    destination.mkdir(mode=0o700)
    return destination


def capture(args):
    os.umask(0o077)
    destination = private_destination(args.output)
    stop = False
    def stopped(_signal, _frame):
        nonlocal stop
        stop = True
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stopped)
    initial = fingerprints(args.bundle)
    started = now()
    try:
        boot_id = read_regular(Path("/proc/sys/kernel/random/boot_id"), 128)[0].decode().strip()
    except (OSError, ValueError):
        boot_id = None
    json_file(destination / "metadata.json", {"schema": 1, "started": started,
        "boot_id": boot_id,
        "seconds": args.seconds, "interval": args.interval, "probe_interval": args.probe_interval,
        "network_settings_changed": False, "sensitive": True,
        "notice": "Capture privée. Ne pas publier les journaux, adresses et identités.",
        "fingerprints_before": initial})
    deadline = time.monotonic() + args.seconds
    next_probe = 0
    next_journal = 0
    journal_since = started
    count = 0
    reason = "duration"
    path = destination / "samples.jsonl"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    print(json.dumps({"capture_started": True, "directory": str(destination), "seconds": args.seconds}), flush=True)
    with os.fdopen(descriptor, "wb") as stream:
        while not stop and time.monotonic() < deadline:
            tick = time.monotonic()
            probe = args.probes and tick >= next_probe
            include_journal = tick >= next_journal
            journal_until = now()
            sample = snapshot(probe=probe, drone_number=args.drone_number,
                journal_since=journal_since if include_journal else None)
            if include_journal:
                journal_since = journal_until
                next_journal = time.monotonic() + 10
            if probe:
                next_probe = time.monotonic() + args.probe_interval
            raw = json.dumps(sample, ensure_ascii=False).encode() + b"\n"
            if stream.tell() + len(raw) > MAX_CAPTURE_BYTES:
                reason = "size_limit"
                break
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            count += 1
            remaining = max(0, min(args.interval - (time.monotonic() - tick), deadline - time.monotonic()))
            if remaining:
                time.sleep(remaining)
    if stop:
        reason = "stopped"
    final = fingerprints(args.bundle)
    unchanged = compare_fingerprints(initial, final)
    json_file(destination / "result.json", {"finished": now(), "samples": count, "reason": reason,
        "fingerprints_after": final, "configuration_fingerprints_unchanged": unchanged})
    for name, arguments in {
        "networkmanager": ["journalctl", "-b", "-u", "NetworkManager.service"],
        "kernel": ["journalctl", "-b", "-k"],
    }.items():
        json_file(destination / (name + ".json"), command([*arguments, "--since", started,
            "--no-pager", "-o", "json", "-n", "2000"], timeout=5))
    print(json.dumps({"capture_finished": True, "samples": count, "reason": reason,
        "configuration_fingerprints_unchanged": unchanged}), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Existing private parent directory (0700).")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--probe-interval", type=float, default=10)
    parser.add_argument("--probes", action="store_true", help="Enable bounded TCP, DNS and HTTPS probes.")
    parser.add_argument("--drone-number", type=int, choices=range(1, 7))
    parser.add_argument("--bundle", type=Path, help="Only hash existing supplier files; never modify them.")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.interval <= 60 or not 5 <= args.probe_interval <= 300:
        parser.error("Durée 1..3600 s, intervalle 1..60 s et sondes 5..300 s requis.")
    try:
        return capture(args)
    except (OSError, ValueError) as error:
        parser.exit(2, "Capture impossible : " + str(error) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
