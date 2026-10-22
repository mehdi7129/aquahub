#!/usr/bin/env python3
"""Real GTK/X11 identity and normal single-instance launch, with no live agent.

Run from the repository root with --package-root pointing at an extracted
application package. Alternatively, --icon-file supplies the candidate icon
while testing the source desktop/frontend. A private D-Bus session and Xvfb
prevent activation of an AquaHub window in the user's desktop session.

The probe imports only Gio/GLib before app.main: importing Gtk earlier would
change the initialization order this test is intended to qualify. Neither
launch uses --test-script or --quit-after (those deliberately disable normal
application uniqueness). The probe only observes native metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
APP_ID = "com.aquadrone.aquahub"
REPORT = ROOT / "frontend/tests/ui-desktop-identity-result.json"

PROBE = r'''
import json
import os
from pathlib import Path
import gi
# Do not import Gtk/Gdk here: app.main must define GLib's identity first.
from gi.repository import Gio, GLib

destination = Path(os.environ["AQUAHUB_IDENTITY_PROBE"])
observed = {}
attached = False
activations = 0

def activated(*_args):
    global activations
    activations += 1

def inspect_native():
    global attached, activations
    application = Gio.Application.get_default()
    early_dialog = os.environ.get("AQUAHUB_IDENTITY_MODE") == "missing-frontend"
    if not early_dialog and (application is None or not application.get_is_registered()):
        return True
    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkX11", "3.0")
    from gi.repository import Gtk, GdkX11
    windows = [window for window in Gtk.Window.list_toplevels() if window.get_visible()] if early_dialog else application.get_windows()
    if not windows or not windows[0].get_realized():
        return True
    if not early_dialog and not attached:
        # The initial activation created this realized window before the probe.
        activations = 1
        application.connect("activate", activated)
        attached = True
    window = windows[0]
    theme = Gtk.IconTheme.get_default()
    effective_icon = window.get_icon_name() or Gtk.Window.get_default_icon_name()
    info = theme.lookup_icon(effective_icon, 32, Gtk.IconLookupFlags.FORCE_SIZE)
    icons = []
    for size in (32, 64):
        try:
            pixbuf = theme.load_icon(effective_icon, size, Gtk.IconLookupFlags.FORCE_SIZE)
            icons.append([pixbuf.get_width(), pixbuf.get_height()])
        except GLib.Error:
            icons.append(None)
    observed.update({
        "application_id": application.get_application_id() if application else None,
        "application_name": GLib.get_application_name(),
        "program_name": GLib.get_prgname(),
        "title": window.get_title(),
        "icon_name": window.get_icon_name(),
        "default_icon_name": Gtk.Window.get_default_icon_name(),
        "is_remote": application.get_is_remote() if application else False,
        "non_unique": bool(application.get_flags() & Gio.ApplicationFlags.NON_UNIQUE) if application else False,
        "window_count": len(windows),
        "activations": activations,
        "xid": GdkX11.X11Window.get_xid(window.get_window()),
        "icon_file": info.get_filename() if info is not None else None,
        "icon_sizes": icons,
    })
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(observed))
    temporary.replace(destination)
    return True

GLib.timeout_add(100, inspect_native)
from aquahub_desktop.app import main
raise SystemExit(main())
'''


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, text=True, check=True, capture_output=True, **kwargs)


def read_probe(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def wait_until(predicate, process: subprocess.Popen, seconds: float = 15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        if process.poll() is not None:
            raise RuntimeError("La fenêtre native a quitté avant la fin du contrôle.")
        time.sleep(0.05)
    raise RuntimeError("Le contrôle natif n’a pas terminé dans le délai prévu.")


def parse_xprop(output: str) -> dict:
    wm_class = re.search(r'WM_CLASS\(STRING\) = "([^"]*)", "([^"]*)"', output)
    app_id = re.search(r'_GTK_APPLICATION_ID\([^)]*\) = "([^"]*)"', output)
    title = re.search(r'(?:^|\n)(?:_NET_WM_NAME|WM_NAME)\([^)]*\) = "([^"]*)"', output)
    return {
        "wm_class": list(wm_class.groups()) if wm_class else None,
        "gtk_application_id": app_id.group(1) if app_id else None,
        "window_title": title.group(1) if title else None,
    }


def isolated(args) -> int:
    """Worker: its display and session bus belong only to this invocation."""
    with tempfile.TemporaryDirectory(prefix="aquahub-identity-") as directory:
        private = Path(directory)
        private.chmod(0o700)
        runtime = private / "runtime"
        runtime.mkdir(mode=0o700)
        share = private / "share"
        theme = share / "icons/hicolor"
        (theme / "scalable/apps").mkdir(parents=True)
        # GTK needs the shared MIME definitions to select its SVG pixbuf loader.
        # This is generic system data, without any application icon or user data.
        (share / "mime").symlink_to("/usr/share/mime", target_is_directory=True)
        icon = theme / f"scalable/apps/{APP_ID}.svg"
        shutil.copyfile(args.icon_file, icon)
        # Theme metadata is a fixture; the SVG bytes are the candidate's own.
        (theme / "index.theme").write_text(
            "[Icon Theme]\nName=hicolor\nDirectories=scalable/apps\n"
            "[scalable/apps]\nSize=48\nType=Scalable\nMinSize=8\nMaxSize=512\nContext=Applications\n"
        )
        launcher = private / "probe.py"
        launcher.write_text(PROBE)
        probe = private / "first.json"
        socket_path = runtime / "unused-agent.sock"
        state = private / "state"
        environment = {
            **os.environ,
            "PYTHONPATH": str(args.desktop_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GDK_BACKEND": "x11",
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_DATA_HOME": str(share),
            "XDG_DATA_DIRS": str(share),
            "XDG_CACHE_HOME": str(private / "cache"),
            "XDG_CONFIG_HOME": str(private / "config"),
            "GSETTINGS_SCHEMA_DIR": "/usr/share/glib-2.0/schemas",
            "GSETTINGS_BACKEND": "memory",
            "AQUAHUB_IDENTITY_PROBE": str(probe),
            "NO_AT_BRIDGE": "1",
            "GTK_USE_PORTAL": "0",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "WEBKIT_DISABLE_DMABUF_RENDERER": "1",
            # Identity tests do not exercise media codecs or GPU drivers.
            "GST_PLUGIN_SYSTEM_PATH_1_0": "",
            "GST_PLUGIN_PATH_1_0": "",
        }
        environment.pop("WAYLAND_DISPLAY", None)
        command = [
            sys.executable, str(launcher), "--demo", "--no-agent",
            "--socket", str(socket_path), "--state-dir", str(state),
            "--frontend", str(args.frontend),
        ]
        checks = []

        def check(name: str, passed: bool):
            checks.append({"name": name, "passed": bool(passed)})

        log = private / "native.log"
        processes = []
        try:
            with log.open("wb") as stream:
                first = subprocess.Popen(command, env=environment, stdout=stream, stderr=stream, start_new_session=True)
                processes.append(first)
                before = wait_until(lambda: read_probe(probe), first)
                first_xid = before["xid"]
                props = parse_xprop(run([
                    "xprop", "-id", hex(first_xid), "WM_CLASS", "_GTK_APPLICATION_ID", "WM_NAME",
                ], env=environment).stdout)
                check("human_application_name", before["application_name"] == "AquaHub")
                check("canonical_program_name", before["program_name"] == APP_ID)
                check("canonical_application_id", before["application_id"] == APP_ID)
                check("canonical_window_icon", before["icon_name"] == APP_ID and before["default_icon_name"] == APP_ID)
                check("window_title", before["title"] == "AquaHub" and props["window_title"] == "AquaHub")
                check("normal_unique_application_flags", not before["non_unique"] and not before["is_remote"])
                check("native_gtk_application_id", props["gtk_application_id"] == APP_ID)
                check("native_wm_class", props["wm_class"] == [APP_ID, "Com.aquadrone.aquahub"])
                resolved = Path(before["icon_file"]).resolve() if before["icon_file"] else None
                check("candidate_icon_resolved_without_system_icon", resolved == icon.resolve())
                check("candidate_icon_rasterized_at_two_sizes", before["icon_sizes"] == [[32, 32], [64, 64]])
                check("normal_launch_arguments", "--test-script" not in command and "--quit-after" not in command)
                second_environment = {**environment, "AQUAHUB_IDENTITY_PROBE": str(private / "second.json")}
                second = subprocess.Popen(command, env=second_environment, stdout=stream, stderr=stream, start_new_session=True)
                processes.append(second)
                second.wait(timeout=12)
                check("second_normal_launch_exits_successfully", second.returncode == 0)

                def reactivated():
                    current = read_probe(probe)
                    return current if current and current.get("activations", 0) >= 2 else None

                after = wait_until(reactivated, first, 8)
                check("second_launch_activates_original_process", first.poll() is None and after["activations"] == 2)
                check("one_original_native_window", after["window_count"] == 1 and after["xid"] == first_xid)
                tree = run(["xwininfo", "-root", "-tree"], env=environment).stdout
                native_windows = []
                for line in tree.splitlines():
                    if '"AquaHub":' in line:
                        xid = re.search(r'0x[0-9a-f]+', line).group(0)
                        detail = run(["xwininfo", "-id", xid], env=environment).stdout
                        if "Map State: IsViewable" in detail:
                            native_windows.append(line)
                check("one_x11_window_after_two_launches", len(native_windows) == 1)
                check("no_agent_socket_started", not socket_path.exists())

                # The resource-missing path runs a modal Gtk.MessageDialog
                # before Gtk.Application exists. Its inherited identity matters too.
                error_probe = private / "error.json"
                error_environment = {
                    **environment, "AQUAHUB_IDENTITY_PROBE": str(error_probe),
                    "AQUAHUB_IDENTITY_MODE": "missing-frontend",
                }
                error_command = [*command[:-1], str(private / "missing-frontend")]
                error_process = subprocess.Popen(error_command, env=error_environment, stdout=stream, stderr=stream, start_new_session=True)
                processes.append(error_process)
                error_identity = wait_until(lambda: read_probe(error_probe), error_process)
                check("early_error_dialog_human_name", error_identity["application_name"] == "AquaHub" and error_identity["title"] == "AquaHub")
                check("early_error_dialog_program_identity", error_identity["program_name"] == APP_ID)
                check("early_error_dialog_inherits_candidate_icon", error_identity["default_icon_name"] == APP_ID and bool(error_identity["icon_file"]) and Path(error_identity["icon_file"]).resolve() == icon.resolve())

                assistant_props = None
                if args.package_root:
                    from gi.repository import Gio
                    entries = {}
                    for entry_id in (APP_ID, APP_ID + ".setup"):
                        entry = Gio.DesktopAppInfo.new_from_filename(str(args.package_root / f"usr/share/applications/{entry_id}.desktop"))
                        if entry is None:
                            raise RuntimeError("Le lanceur du paquet extrait n’est pas lisible par Gio.")
                        entries[entry_id] = entry
                    check("main_launcher_name_icon_and_visibility", entries[APP_ID].get_name() == "AquaHub" and entries[APP_ID].get_icon().to_string() == APP_ID and entries[APP_ID].should_show())
                    check("main_launcher_matches_measured_x11_class", entries[APP_ID].get_startup_wm_class() == props["wm_class"][1])
                    check("assistant_launcher_hidden_with_shared_icon", not entries[APP_ID + ".setup"].should_show() and entries[APP_ID + ".setup"].get_icon().to_string() == APP_ID)
                    bootstrap = args.package_root / "usr/lib/aquahub/aquahub-bootstrap"
                    assistant = subprocess.Popen([str(bootstrap)], env=environment, stdout=stream, stderr=stream, start_new_session=True)
                    processes.append(assistant)

                    def assistant_window():
                        tree = run(["xwininfo", "-root", "-tree"], env=environment).stdout
                        for line in tree.splitlines():
                            dimensions = re.search(r'\)\s+(\d+)x(\d+)', line)
                            if '"AquaHub — Installation"' not in line or not dimensions or not all(int(value) > 1 for value in dimensions.groups()):
                                continue
                            xid = re.search(r'0x[0-9a-f]+', line).group(0)
                            actual = parse_xprop(run(["xprop", "-id", xid, "WM_CLASS", "_GTK_APPLICATION_ID", "_NET_WM_NAME"], env=environment).stdout)
                            if actual["gtk_application_id"] == APP_ID + ".setup":
                                return actual
                        return None

                    assistant_props = wait_until(assistant_window, assistant)
                    check("assistant_native_identity", assistant_props["wm_class"] == [APP_ID + ".setup", APP_ID + ".setup"] and assistant_props["window_title"] == "AquaHub — Installation")
                    check("assistant_launcher_matches_measured_x11_class", entries[APP_ID + ".setup"].get_startup_wm_class() == assistant_props["wm_class"][1])
            summary = {
                "passed": all(item["passed"] for item in checks),
                "mode": "extracted-package" if args.package_root else "source-with-supplied-candidate-icon",
                "display_backend": "X11 on isolated Xvfb",
                "session_bus": "isolated dbus-run-session",
                "uses_live_agent": False,
                "uses_normal_uniqueness_flags": True,
                "native_identity": {key: before[key] for key in (
                    "application_id", "application_name", "program_name", "title", "icon_name", "default_icon_name", "icon_sizes",
                )},
                "x11_properties": props,
                "early_error_dialog": {key: error_identity[key] for key in ("application_name", "program_name", "title", "default_icon_name")},
                "assistant_x11_properties": assistant_props,
                "candidate_icon_sha256": hashlib.sha256(args.icon_file.read_bytes()).hexdigest(),
                "checks": checks,
                "limits": [
                    "X11 native properties and GTK icon lookup do not prove Wayland app_id or GNOME grouping.",
                    "The normal process is instrumented only by an observational GLib timer.",
                    "No bundle, Docker, drone, installed launcher or user's desktop preferences are changed.",
                ],
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if summary["passed"] else 1
        except Exception:
            if log.is_file():
                # Diagnostics stay on the test runner's terminal, never in the public report.
                print(log.read_text(errors="replace")[-8000:], file=sys.stderr)
            raise
        finally:
            # Each process owns a fresh group; never inspect or terminate another session.
            for process in reversed(processes):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, help="Root of an extracted candidate .deb (never installed).")
    parser.add_argument("--icon-file", type=Path, help="Candidate SVG for source-only testing.")
    parser.add_argument("--desktop-root", type=Path, default=ROOT / "desktop")
    parser.add_argument("--frontend", type=Path, default=ROOT / "frontend/dist")
    parser.add_argument("--report", type=Path, default=REPORT)
    parser.add_argument("--isolated-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.package_root:
        root = args.package_root.resolve()
        args.desktop_root = root / "usr/lib/aquahub/desktop"
        args.frontend = root / "usr/share/aquahub/frontend"
        args.icon_file = root / f"usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg"
    if args.icon_file is None or not args.icon_file.is_file():
        parser.error("Fournir --package-root ou --icon-file avec l’icône du candidat.")
    for path in (args.desktop_root / "aquahub_desktop/app.py", args.frontend / "index.html"):
        if not path.is_file():
            parser.error("Le desktop Python ou le frontend du candidat est absent.")
    args.icon_file = args.icon_file.resolve()
    args.desktop_root = args.desktop_root.resolve()
    args.frontend = args.frontend.resolve()
    args.report = args.report.resolve()
    if args.isolated_worker:
        return isolated(args)
    for command in ("dbus-run-session", "xvfb-run", "xprop", "xwininfo"):
        if shutil.which(command) is None:
            parser.error(f"Dépendance native absente : {command}.")
    with tempfile.TemporaryDirectory(prefix="aquahub-identity-bus-") as directory:
        # No service directory: the fixture must not auto-start desktop portals,
        # keyrings or user-session services through the system's D-Bus catalog.
        config = Path(directory) / "session.conf"
        config.write_text(
            '<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen>'
            '<policy context="default"><allow send_destination="*"/>'
            '<allow own="*"/><allow eavesdrop="true"/></policy></busconfig>'
        )
        command = [
            "dbus-run-session", "--config-file", str(config), "--", "xvfb-run", "-a", "-s", "-screen 0 1440x960x24 -nolisten tcp",
            sys.executable, str(Path(__file__).resolve()), "--isolated-worker",
            "--desktop-root", str(args.desktop_root), "--frontend", str(args.frontend),
            "--icon-file", str(args.icon_file), "--report", str(args.report),
        ]
        if args.package_root:
            command.extend(("--package-root", str(args.package_root.resolve())))
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
