"""Native WebKit layout and trusted keyboard QA; run inside xvfb-run."""
import argparse
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--zoom', type=float, choices=(1, 2), required=True)
parser.add_argument('--scale', type=int, choices=(1, 2), default=1)
args = parser.parse_args()
if not os.environ.get('DISPLAY'):
    raise SystemExit('Ce test doit être lancé dans un serveur X isolé (xvfb-run).')

x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
Display = ctypes.c_void_p
Window = ctypes.c_ulong
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XOpenDisplay.restype = Display
x11.XDefaultRootWindow.argtypes = [Display]
x11.XDefaultRootWindow.restype = Window
x11.XQueryTree.argtypes = [Display, Window, ctypes.POINTER(Window), ctypes.POINTER(Window), ctypes.POINTER(ctypes.POINTER(Window)), ctypes.POINTER(ctypes.c_uint)]
x11.XFetchName.argtypes = [Display, Window, ctypes.POINTER(ctypes.c_char_p)]
x11.XFree.argtypes = [ctypes.c_void_p]
x11.XSetInputFocus.argtypes = [Display, Window, ctypes.c_int, ctypes.c_ulong]
x11.XRaiseWindow.argtypes = [Display, Window]
x11.XFlush.argtypes = [Display]
x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
x11.XStringToKeysym.restype = ctypes.c_ulong
x11.XKeysymToKeycode.argtypes = [Display, ctypes.c_ulong]
x11.XKeysymToKeycode.restype = ctypes.c_ubyte
xtst.XTestFakeKeyEvent.argtypes = [Display, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
display = x11.XOpenDisplay(None)
if not display:
    raise SystemExit('Impossible d’ouvrir le serveur X de test.')


def find_window(parent=None):
    parent = parent or x11.XDefaultRootWindow(display)
    root, actual_parent = Window(), Window()
    children = ctypes.POINTER(Window)()
    count = ctypes.c_uint()
    if not x11.XQueryTree(display, parent, ctypes.byref(root), ctypes.byref(actual_parent), ctypes.byref(children), ctypes.byref(count)):
        return None
    found = None
    try:
        for index in range(count.value):
            child = children[index]
            name = ctypes.c_char_p()
            if x11.XFetchName(display, child, ctypes.byref(name)) and name.value:
                title = name.value.decode('utf-8', 'replace')
                x11.XFree(name)
                if title == 'AquaHub':
                    return child
            found = find_window(child)
            if found:
                return found
    finally:
        if children:
            x11.XFree(children)
    return found


def press(key):
    window = find_window()
    if not window:
        raise RuntimeError('Fenêtre AquaHub introuvable pour le clavier natif.')
    x11.XRaiseWindow(display, window)
    x11.XSetInputFocus(display, window, 2, 0)
    key_name = {'Enter': 'Return', ' ': 'space'}.get(key, key)
    code = x11.XKeysymToKeycode(display, x11.XStringToKeysym(key_name.encode()))
    if not code:
        raise RuntimeError('Touche X inconnue : ' + key_name)
    xtst.XTestFakeKeyEvent(display, code, 1, 0)
    xtst.XTestFakeKeyEvent(display, code, 0, 0)
    x11.XFlush(display)


name = f'ui-layout-1366x768-{round(args.zoom * 100)}' + ('-hidpi' if args.scale == 2 else '')
report = ROOT / 'tests' / (name + '.json')
report.unlink(missing_ok=True)
environment = {**os.environ, 'PYTHONPATH': str(ROOT.parent / 'desktop'), 'GDK_BACKEND': 'x11', 'GDK_SCALE': str(args.scale)}
environment.pop('WAYLAND_DISPLAY', None)
command = ['python3', '-m', 'aquahub_desktop', '--demo', '--frontend', str(ROOT/'dist'),
           '--state-dir', str(ROOT/'tests/.runtime'/name), '--width', '1366', '--height', '768', '--zoom', str(args.zoom),
           '--test-script', str(ROOT/'tests/ui-layout.js'), '--test-output', str(report),
           '--screenshot', str(ROOT/'tests/screenshots'/(name+'.png')), '--quit-after', '45']
process = subprocess.Popen(command, env=environment, cwd=ROOT)
sent = set()
deadline = time.monotonic() + 40
result = None
try:
    while time.monotonic() < deadline and process.poll() is None:
        if report.is_file():
            try:
                data = json.loads(report.read_text())
            except (OSError, ValueError):
                time.sleep(.05)
                continue
            if data.get('phase') == 'complete':
                result = data
                time.sleep(1)
                break
            if data.get('phase') == 'await_key' and data['sequence'] not in sent:
                press(data['key'])
                sent.add(data['sequence'])
        time.sleep(.04)
finally:
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
if result is None:
    raise SystemExit('Le test de disposition natif n’a pas produit de résultat complet.')
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result['passed'] else 1)
