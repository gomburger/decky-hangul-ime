"""Tells whether the Steam keyboard is open, from gamescope's focus on Steam's display.

While the Steam keyboard is open, gamescope gives focus to Steam's own app id (769),
and sets it on the root window of Steam's display. This watches that property, so a
change is known as soon as it happens rather than on the next poll.
"""

import ctypes
import ctypes.util

STEAM_APP_ID = 769
FOCUSED_APP = "GAMESCOPE_FOCUSED_APP"
PROPERTY_CHANGE_MASK = 1 << 22
PROPERTY_NOTIFY = 28
XA_CARDINAL = 6
EVENT_SIZE = 192
OFFSET_ATOM = 40


class SteamFocus:
    """window and property are only for tests; they default to the root window's
    focused app property."""

    def __init__(self, display_name, window=None, property_name=FOCUSED_APP):
        x = self._x = ctypes.CDLL(ctypes.util.find_library("X11"))
        ulong, void = ctypes.c_ulong, ctypes.c_void_p
        x.XOpenDisplay.restype = void
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ulong
        x.XDefaultRootWindow.argtypes = [void]
        x.XInternAtom.restype = ulong
        x.XInternAtom.argtypes = [void, ctypes.c_char_p, ctypes.c_int]
        x.XSelectInput.argtypes = [void, ulong, ctypes.c_long]
        x.XGetWindowProperty.argtypes = [
            void, ulong, ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ulong,
            ctypes.POINTER(ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ulong),
            ctypes.POINTER(ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        x.XPending.argtypes = [void]
        x.XNextEvent.argtypes = [void, void]
        x.XFlush.argtypes = [void]
        x.XFree.argtypes = [void]
        x.XConnectionNumber.argtypes = [void]
        x.XCloseDisplay.argtypes = [void]

        self._display = x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open display {display_name}")
        self._window = window or x.XDefaultRootWindow(self._display)
        self._atom = x.XInternAtom(self._display, property_name.encode(), 0)
        x.XSelectInput(self._display, self._window, PROPERTY_CHANGE_MASK)
        x.XFlush(self._display)
        self._event = ctypes.create_string_buffer(EVENT_SIZE)
        self.keyboard_open = self._read()

    def fileno(self):
        return self._x.XConnectionNumber(self._display)

    def _read(self):
        x = self._x
        kind, size_format = ctypes.c_ulong(), ctypes.c_int()
        count, remaining = ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        status = x.XGetWindowProperty(
            self._display, self._window, self._atom, 0, 1, 0, XA_CARDINAL,
            ctypes.byref(kind), ctypes.byref(size_format), ctypes.byref(count),
            ctypes.byref(remaining), ctypes.byref(data),
        )
        try:
            if status != 0 or not data or count.value < 1 or size_format.value != 32:
                return False
            # Xlib hands back 32-bit items as C longs.
            return ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0] == STEAM_APP_ID
        finally:
            if data:
                x.XFree(data)

    def process_events(self):
        """Read pending events, and return True if the keyboard opened or closed."""
        x = self._x
        changed = False
        while x.XPending(self._display):
            x.XNextEvent(self._display, self._event)
            kind = int.from_bytes(self._event.raw[:4], "little")
            atom = int.from_bytes(self._event.raw[OFFSET_ATOM:OFFSET_ATOM + 8], "little")
            if kind == PROPERTY_NOTIFY and atom == self._atom:
                changed = True
        if not changed:
            return False
        was_open, self.keyboard_open = self.keyboard_open, self._read()
        return was_open != self.keyboard_open

    def close(self):
        self._x.XCloseDisplay(self._display)
