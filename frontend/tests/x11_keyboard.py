"""Trusted keyboard input for isolated native GTK tests; no subprocess tools."""
import ctypes
import ctypes.util


class Keyboard:
    def __init__(self):
        self.x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
        self.xtst = ctypes.CDLL(ctypes.util.find_library('Xtst'))
        self.Window = ctypes.c_ulong
        Display, Window = ctypes.c_void_p, self.Window
        x11 = self.x11
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]; x11.XOpenDisplay.restype = Display
        x11.XDefaultRootWindow.argtypes = [Display]; x11.XDefaultRootWindow.restype = Window
        x11.XQueryTree.argtypes = [Display, Window, ctypes.POINTER(Window), ctypes.POINTER(Window), ctypes.POINTER(ctypes.POINTER(Window)), ctypes.POINTER(ctypes.c_uint)]
        x11.XFetchName.argtypes = [Display, Window, ctypes.POINTER(ctypes.c_char_p)]
        x11.XFree.argtypes = [ctypes.c_void_p]
        x11.XSetInputFocus.argtypes = [Display, Window, ctypes.c_int, ctypes.c_ulong]
        x11.XRaiseWindow.argtypes = [Display, Window]
        x11.XFlush.argtypes = [Display]
        x11.XStringToKeysym.argtypes = [ctypes.c_char_p]; x11.XStringToKeysym.restype = ctypes.c_ulong
        x11.XKeysymToKeycode.argtypes = [Display, ctypes.c_ulong]; x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self.xtst.XTestFakeKeyEvent.argtypes = [Display, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        self.display = x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError('Serveur X de test indisponible.')

    def find(self, title, parent=None):
        x11, Window = self.x11, self.Window
        parent = parent or x11.XDefaultRootWindow(self.display)
        root, actual_parent, count = Window(), Window(), ctypes.c_uint()
        children = ctypes.POINTER(Window)()
        if not x11.XQueryTree(self.display, parent, ctypes.byref(root), ctypes.byref(actual_parent), ctypes.byref(children), ctypes.byref(count)):
            return None
        try:
            for index in range(count.value):
                child, name = children[index], ctypes.c_char_p()
                if x11.XFetchName(self.display, child, ctypes.byref(name)) and name.value:
                    names = [name.value.decode('utf-8', 'replace'), name.value.decode('latin1')]
                    x11.XFree(name)
                    if any(actual == title or actual.endswith(title) for actual in names):
                        return child
                found = self.find(title, child)
                if found:
                    return found
        finally:
            if children:
                x11.XFree(children)
        return None

    def press(self, window, key):
        self.x11.XRaiseWindow(self.display, window)
        self.x11.XSetInputFocus(self.display, window, 2, 0)
        name = {'Enter': 'Return', ' ': 'space', '-': 'minus', '_': 'underscore'}.get(key, key)
        code = self.x11.XKeysymToKeycode(self.display, self.x11.XStringToKeysym(name.encode()))
        if not code:
            raise RuntimeError('Touche non reconnue : ' + name)
        self.xtst.XTestFakeKeyEvent(self.display, code, 1, 0)
        self.xtst.XTestFakeKeyEvent(self.display, code, 0, 0)
        self.x11.XFlush(self.display)
