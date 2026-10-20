"""GTK/WebKit desktop shell. Renderer and agent never share a TCP listener."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

from .bridge import BridgeError, RpcClient, fixed_url, is_app_uri, portainer_url, agent_connection, portainer_credentials

BRIDGE_SOURCE = r"""(() => {
  if (window !== window.top) return;
  const pending = new Map();
  let sequence = 0;
  const nonce = __NONCE__;
  Object.defineProperty(window, '__aquahubDeliver', {value: (id, result, error) => {
    const item = pending.get(id);
    if (!item) return;
    clearTimeout(item.timer);
    pending.delete(id);
    if (error) {
      const failure = new Error(error.message || 'La demande a échoué.');
      failure.code = error.code;
      item.reject(failure);
    } else item.resolve(result);
  }});
  Object.defineProperty(window, 'aquahub', {value: Object.freeze({
    request(method, params = {}) {
      if (pending.size >= 32) return Promise.reject(new Error('Trop de demandes simultanées.'));
      return new Promise((resolve, reject) => {
        const id = String(++sequence);
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error('La demande a expiré. Vérifiez le résultat avant de réessayer.'));
        }, 190000);
        pending.set(id, {resolve, reject, timer});
        try { window.webkit.messageHandlers.aquahub.postMessage(JSON.stringify({id, method, params, nonce})); }
        catch (error) { clearTimeout(timer); pending.delete(id); reject(error); }
      });
    }
  })});
})();
"""


def runtime_paths(state_dir: Path | None = None) -> tuple[Path, Path]:
    state = state_dir or Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "aquahub"
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/aquahub-{os.getuid()}"))
    runtime = base / "aquahub" if base.name != f"aquahub-{os.getuid()}" else base
    for directory in (state, runtime):
        if directory.is_symlink():
            raise BridgeError("unsafe_path", "Le répertoire AquaHub ne doit pas être un lien symbolique.")
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.stat().st_uid != os.getuid():
            raise BridgeError("unsafe_path", "Le répertoire AquaHub appartient à un autre utilisateur.")
        directory.chmod(0o700)
    return runtime, state


def ensure_agent(path: Path, state_dir: Path, source_root: Path) -> None:
    """An owned development/session agent outlives the window and its jobs."""
    lock = path.parent / "launch.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            RpcClient(path, timeout=2).request("app.version")
            return
        except BridgeError as error:
            if error.code not in {"agent_unavailable", "agent_timeout"}:
                raise
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONPATH"] = str(source_root / "agent")
        log = state_dir / "agent.log"
        handle = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(handle, "ab") as stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "aquahub_agent", "--socket", str(path), "--state-dir", str(state_dir), "--allow-uid", str(os.getuid())],
                env=environment, stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                start_new_session=True,
            )
        for _ in range(60):
            if process.poll() is not None:
                raise BridgeError("agent_start_failed", "L’agent AquaHub n’a pas démarré. Consultez le diagnostic local.")
            time.sleep(0.1)
            try:
                RpcClient(path, timeout=1).request("app.version")
                return
            except BridgeError:
                pass
        raise BridgeError("agent_start_failed", "Le démarrage de l’agent AquaHub dépasse le délai prévu.")
    finally:
        os.close(descriptor)


def main(argv=None) -> int:
    def preview_integer(minimum, maximum):
        def parse(value):
            result = int(value)
            if not minimum <= result <= maximum:
                raise argparse.ArgumentTypeError(f"Valeur attendue entre {minimum} et {maximum}.")
            return result
        return parse
    def preview_zoom(value):
        result = float(value)
        if not .5 <= result <= 3:
            raise argparse.ArgumentTypeError("Le zoom d’aperçu doit être compris entre 0,5 et 3.")
        return result
    parser = argparse.ArgumentParser(description="AquaHub — application de maintenance locale")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--frontend", type=Path)
    parser.add_argument("--no-agent", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--test-script", type=Path)
    parser.add_argument("--test-output", type=Path)
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--quit-after", type=int)
    parser.add_argument("--width", type=preview_integer(1024, 3840), default=1440, help="Largeur d’aperçu, en pixels")
    parser.add_argument("--height", type=preview_integer(650, 2160), default=940, help="Hauteur d’aperçu, en pixels")
    parser.add_argument("--zoom", type=preview_zoom, default=1.0, help="Facteur de zoom d’aperçu, de 0,5 à 3")
    options = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("La fenêtre AquaHub doit être lancée depuis une session utilisateur, sans root.", file=sys.stderr)
        return 2

    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gio, GLib, Gtk, WebKit2 as WebKit

    source_root = Path(__file__).resolve().parents[2]
    installed_share = Path("/usr/share/aquahub/frontend")
    installed = source_root == Path('/usr/lib/aquahub') or Path('/var/lib/aquahub/setup.complete').is_file()
    frontend = options.frontend or (installed_share if source_root == Path('/usr/lib/aquahub') else source_root / "frontend/dist")
    index = frontend.resolve() / "index.html"
    if not index.is_file():
        dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text="AquaHub ne peut pas ouvrir son interface.")
        dialog.format_secondary_text("Les ressources de l’application sont absentes. Réinstallez le paquet AquaHub.")
        dialog.run()
        dialog.destroy()
        return 2

    runtime, state = runtime_paths(options.state_dir)
    system_socket = Path("/run/aquahub/agent.sock")
    path, autostart = agent_connection(installed, runtime/'agent.sock',system_socket,options.socket)
    if autostart and not options.no_agent and not options.demo:
        try:
            ensure_agent(path, state, source_root)
        except BridgeError as error:
            dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text=error.message)
            dialog.run()
            dialog.destroy()
            return 2
    client = RpcClient(path)
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="aquahub-desktop")

    class Window(Gtk.ApplicationWindow):
        def __init__(self, app):
            super().__init__(application=app, title="AquaHub")
            self.set_default_size(options.width, options.height)
            self.set_size_request(1024, 650)
            self.set_icon_name("aquahub")
            self.nonce = secrets.token_urlsafe(32)
            self.alive = True
            self.pending = set()
            self.manager = WebKit.UserContentManager()
            self.manager.connect("script-message-received::aquahub", self.message)
            self.manager.register_script_message_handler("aquahub")
            self.manager.add_script(WebKit.UserScript.new(
                BRIDGE_SOURCE.replace("__NONCE__", json.dumps(self.nonce)),
                WebKit.UserContentInjectedFrames.TOP_FRAME, WebKit.UserScriptInjectionTime.START, None, None,
            ))
            web_data = state / "webkit"
            web_cache = state / "webkit-cache"
            for folder in (web_data, web_cache):
                folder.mkdir(mode=0o700, exist_ok=True)
            storage = WebKit.WebsiteDataManager(base_data_directory=str(web_data), base_cache_directory=str(web_cache))
            context = WebKit.WebContext.new_with_website_data_manager(storage)
            self.web = WebKit.WebView(web_context=context, user_content_manager=self.manager)
            self.web.set_zoom_level(options.zoom)
            settings = self.web.get_settings()
            settings.set_enable_developer_extras(bool(options.test_script))
            settings.set_enable_write_console_messages_to_stdout(bool(options.test_script))
            settings.set_allow_file_access_from_file_urls(False)
            settings.set_allow_universal_access_from_file_urls(False)
            self.web.connect("decide-policy", self.policy)
            self.web.connect("permission-request", lambda _view, request: request.deny() or True)
            self.web.connect("load-changed", self.loaded)
            self.web.connect("web-process-terminated", self.process_terminated)
            self.connect("destroy", self.closed)
            self.add(self.web)
            self.web.load_uri(index.as_uri() + ("?demo=1" if options.demo else ""))
            self.show_all()
            if options.quit_after:
                GLib.timeout_add_seconds(options.quit_after, lambda: self.get_application().quit() or False)

        def closed(self, *_args):
            self.alive = False

        def policy(self, _view, decision, kind):
            if kind in (WebKit.PolicyDecisionType.NAVIGATION_ACTION, WebKit.PolicyDecisionType.NEW_WINDOW_ACTION):
                action = decision.get_navigation_action()
                uri = action.get_request().get_uri()
                if not is_app_uri(uri, index) or kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
                    decision.ignore()
                    return True
            return False

        def loaded(self, _view, event):
            if event == WebKit.LoadEvent.FINISHED and is_app_uri(self.web.get_uri(), index):
                if options.test_script:
                    script = options.test_script.read_text(encoding="utf-8")
                    GLib.timeout_add(1200, lambda: self.evaluate(script) or False)
                elif options.screenshot:
                    GLib.timeout_add_seconds(3, self.snapshot)

        def process_terminated(self, *_args):
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text="L’affichage AquaHub a été interrompu.")
            dialog.format_secondary_text("Les tâches de maintenance continuent dans l’agent. Rouvrez l’application pour retrouver leur résultat.")
            dialog.run()
            dialog.destroy()

        def evaluate(self, script):
            if self.alive:
                self.web.evaluate_javascript(script, -1, None, None, None, None, None)

        def deliver(self, request_id, result=None, error=None):
            self.pending.discard(request_id)
            if self.alive:
                args = ",".join(json.dumps(item, ensure_ascii=True) for item in (request_id, result, error))
                self.evaluate(f"window.__aquahubDeliver({args});")
            return False

        def message(self, _manager, value):
            try:
                if not is_app_uri(self.web.get_uri(), index):
                    return
                raw = value.get_js_value().to_string()
                if len(raw) > 256 * 1024:
                    return
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("nonce") != self.nonce:
                    return
                request_id = data.get("id")
                if not isinstance(request_id, str) or not request_id.isdecimal() or len(request_id) > 20 or request_id in self.pending:
                    return
                if len(self.pending) >= 32:
                    self.deliver(request_id, error={"code": "busy", "message": "Trop de demandes simultanées."})
                    return
                method = data.get("method")
                params = data.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    raise BridgeError("invalid_request", "La demande est invalide.")
                self.pending.add(request_id)
                if method.startswith("native."):
                    self.native(request_id, method, params)
                    return
                if method == "test.report" and options.test_output:
                    options.test_output.parent.mkdir(parents=True, exist_ok=True)
                    options.test_output.write_text(json.dumps(params, ensure_ascii=False, indent=2) + "\n")
                    self.deliver(request_id, {"saved": True})
                    if options.screenshot:
                        GLib.timeout_add(400, self.snapshot)
                    return
                future = executor.submit(client.request, method, params)
                def complete(done):
                    try:
                        result = done.result()
                        if method == "diagnostics.export" and isinstance(result, dict) and isinstance(result.get("content"), str):
                            folder = state / "diagnostics"
                            folder.mkdir(mode=0o700, exist_ok=True)
                            content = result["content"].encode("utf-8")
                            filename = f"aquahub-diagnostic-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.json"
                            target = folder / filename
                            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                            with os.fdopen(descriptor, "wb") as output:
                                output.write(content)
                            result = {"path": str(target), "filename": filename, "bytes": len(content)}
                        GLib.idle_add(self.deliver, request_id, result, None)
                    except BridgeError as error:
                        GLib.idle_add(self.deliver, request_id, None, {"code": error.code, "message": error.message})
                    except Exception:
                        GLib.idle_add(self.deliver, request_id, None, {"code": "desktop_error", "message": "La demande n’a pas pu être traitée."})
                future.add_done_callback(complete)
            except (ValueError, TypeError, AttributeError, BridgeError):
                return

        def native(self, request_id, method, params):
            try:
                if method=='native.source_credentials':
                    self.source_credentials_dialog(request_id)
                    return
                if method=='native.portainer_setup':
                    self.prepare_portainer(request_id)
                    return
                if method == "native.open":
                    if params.get('target')=='portainer':
                        self.prepare_portainer(request_id)
                        return
                    if params.get("target") == "diagnostic":
                        folder = state / "diagnostics"
                        folder.mkdir(mode=0o700, exist_ok=True)
                        uri = folder.as_uri()
                    else:
                        uri = fixed_url(params.get("target"))
                    Gio.AppInfo.launch_default_for_uri(uri, self.get_display().get_app_launch_context())
                    self.deliver(request_id, {"opened": True})
                elif method == "native.choose_file":
                    chooser = Gtk.FileChooserNative.new("Choisir une archive Aqua Hub", self, Gtk.FileChooserAction.OPEN, "Choisir", "Annuler")
                    archive_filter = Gtk.FileFilter()
                    archive_filter.set_name("Livraisons Aqua Hub (*.tar.gz, *.tar, *.zip)")
                    for pattern in ("*.tar.gz", "*.tgz", "*.tar", "*.zip"):
                        archive_filter.add_pattern(pattern)
                    chooser.add_filter(archive_filter)
                    def chosen(dialog, response):
                        filename = dialog.get_filename() if response == Gtk.ResponseType.ACCEPT else None
                        self.deliver(request_id, {"path": filename, "canceled": filename is None})
                        dialog.destroy()
                    chooser.connect("response", chosen)
                    chooser.show()
                elif method == "native.show_folder":
                    folder = state / "diagnostics"
                    folder.mkdir(mode=0o700, exist_ok=True)
                    Gio.AppInfo.launch_default_for_uri(folder.as_uri(), self.get_display().get_app_launch_context())
                    self.deliver(request_id, {"opened": True})
                else:
                    raise BridgeError("unknown_method", "Cette fonction native est inconnue.")
            except (BridgeError, GLib.Error) as error:
                self.deliver(request_id, error={"code": getattr(error, "code", "open_failed"), "message": str(error)})

        def prepare_portainer(self, request_id):
            future=executor.submit(client.request,'portainer.status')
            def resolved(done):
                def show():
                    if not self.alive: return False
                    try:
                        status=done.result()
                        if status.get('setup_required'):
                            self.portainer_dialog(request_id)
                        elif status.get('available') and status.get('url'):
                            Gio.AppInfo.launch_default_for_uri(portainer_url(status['url']),self.get_display().get_app_launch_context())
                            self.deliver(request_id,{'opened':True})
                        else:
                            raise BridgeError('portainer_unavailable',status.get('reason') or 'Portainer est indisponible. Vérifiez son installation.')
                    except Exception as error:
                        self.deliver(request_id,error={'code':getattr(error,'code','portainer_unavailable'),'message':str(error)})
                    return False
                GLib.idle_add(show)
            future.add_done_callback(resolved)

        def source_credentials_dialog(self, request_id):
            dialog=Gtk.Dialog(title='Accès au dépôt privé',transient_for=self,modal=True)
            dialog.add_button('Annuler',Gtk.ResponseType.CANCEL)
            dialog.add_button('Enregistrer',Gtk.ResponseType.OK)
            dialog.set_default_response(Gtk.ResponseType.OK)
            box=dialog.get_content_area();box.set_spacing(14);box.set_border_width(24)
            label=Gtk.Label(label='Saisissez le jeton d’accès fourni pour votre dépôt.\nLaissez le champ vide pour supprimer l’accès enregistré.')
            label.set_line_wrap(True);label.set_xalign(0)
            entry=Gtk.Entry();entry.set_visibility(False);entry.set_max_length(512)
            entry.set_input_purpose(Gtk.InputPurpose.PASSWORD);entry.set_activates_default(True)
            box.pack_start(label,False,False,0);box.pack_start(entry,False,False,0)
            def response(_dialog,code):
                if code!=Gtk.ResponseType.OK:
                    self.deliver(request_id,{'canceled':True});dialog.destroy();return
                token=entry.get_text();entry.set_text('');dialog.destroy()
                future=executor.submit(client.request,'updates.settings',{'token':token})
                def saved(done):
                    try:
                        done.result()
                        GLib.idle_add(self.deliver,request_id,{'saved':True,'configured':bool(token)},None)
                    except BridgeError as error:
                        GLib.idle_add(self.deliver,request_id,None,{'code':error.code,'message':error.message})
                future.add_done_callback(saved)
            dialog.connect('response',response);dialog.show_all();entry.grab_focus()

        def portainer_dialog(self, request_id):
            dialog=Gtk.Dialog(title='Créer le compte Portainer',transient_for=self,modal=True)
            dialog.add_button('Annuler',Gtk.ResponseType.CANCEL)
            dialog.add_button('Créer le compte',Gtk.ResponseType.OK)
            dialog.set_default_response(Gtk.ResponseType.OK)
            dialog.set_default_size(480,360)
            box=dialog.get_content_area()
            box.set_spacing(10)
            box.set_border_width(24)
            explanation=Gtk.Label(label='Choisissez le compte administrateur de Portainer.\nIl sera conservé lors des réinstallations.')
            explanation.set_line_wrap(True); explanation.set_xalign(0)
            box.pack_start(explanation,False,False,4)
            entries=[]
            for label,secret in (('Identifiant',False),('Mot de passe (12 caractères minimum)',True),('Confirmer le mot de passe',True)):
                heading=Gtk.Label(label=label);heading.set_xalign(0)
                entry=Gtk.Entry();entry.set_visibility(not secret);entry.set_max_length(128 if secret else 64)
                entry.set_activates_default(True)
                if secret: entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
                box.pack_start(heading,False,False,0);box.pack_start(entry,False,False,0);entries.append(entry)
            entries[0].set_text('admin')
            error_label=Gtk.Label();error_label.set_line_wrap(True);error_label.set_xalign(0)
            box.pack_start(error_label,False,False,2)
            def response(_dialog,code):
                if code!=Gtk.ResponseType.OK:
                    self.deliver(request_id,{'canceled':True});dialog.destroy();return
                try:
                    credentials=portainer_credentials(*(entry.get_text() for entry in entries))
                except BridgeError as error:
                    error_label.set_text(error.message);return
                for entry in entries: entry.set_text('')
                dialog.destroy()
                future=executor.submit(client.request,'portainer.initialize',credentials)
                def created(done):
                    try:
                        job=done.result()
                        GLib.idle_add(self.deliver,request_id,job,None)
                        if self.alive: self.watch_portainer(job['id'])
                    except BridgeError as error:
                        GLib.idle_add(self.deliver,request_id,None,{'code':error.code,'message':error.message})
                    except Exception:
                        GLib.idle_add(self.deliver,request_id,None,{'code':'portainer_unavailable','message':'La création du compte n’a pas été confirmée. Consultez l’état de la tâche avant de réessayer.'})
                future.add_done_callback(created)
            dialog.connect('response',response)
            dialog.show_all();entries[1].grab_focus()

        def watch_portainer(self, job_id):
            def await_job():
                deadline=time.monotonic()+150
                while self.alive and time.monotonic()<deadline:
                    job=client.request('jobs.get',{'id':job_id})
                    if job['status']=='succeeded': return True
                    if job['status'] not in {'queued','running'}: return False
                    time.sleep(1)
                return False
            future=executor.submit(await_job)
            def finished(done):
                def open_ready():
                    try:
                        if self.alive and done.result():
                            Gio.AppInfo.launch_default_for_uri(fixed_url('portainer'),self.get_display().get_app_launch_context())
                    except (BridgeError,GLib.Error): pass
                    return False
                GLib.idle_add(open_ready)
            future.add_done_callback(finished)

        def snapshot(self):
            if not self.alive or not options.screenshot:
                return False
            def saved(webview, task, _data):
                try:
                    surface = webview.get_snapshot_finish(task)
                    options.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    surface.write_to_png(str(options.screenshot))
                except Exception as error:
                    print(f"Capture impossible : {error}", file=sys.stderr)
            self.web.get_snapshot(WebKit.SnapshotRegion.VISIBLE, WebKit.SnapshotOptions.NONE, None, saved, None)
            return False

    application = Gtk.Application(application_id="com.aquadrone.AquaHub", flags=Gio.ApplicationFlags.NON_UNIQUE if options.test_script or options.quit_after else Gio.ApplicationFlags.FLAGS_NONE)
    def activate(app):
        if app.get_active_window():
            app.get_active_window().present()
        else:
            Window(app)
    application.connect("activate", activate)
    result = application.run([])
    executor.shutdown(wait=False, cancel_futures=True)
    return result
