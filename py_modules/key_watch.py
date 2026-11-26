"""Seeing the Hangul/English key pressed on a physical keyboard during Remote Play.

The streaming client runs on Steam's own display and sends the keys on to the host
PC, whose IME switches between Hangul and English. The plugin never handles those
keys, so it watches them here, as XInput2 raw events, which come for every key on
the display whichever window has focus. Repeats of a key held down are left out.
Keys pressed through XTest are left out too where the X server tells them apart;
gamescope's Xwayland does not, as they come back through the Wayland keyboard, so a
caller that presses one of these keys itself has to leave its own press alone.

RootPropertyWatch follows properties of a display's root window as they change,
such as gamescope's focused app, so the plugin need not look again and again.

Both run in the plugin's own process, where Xlib would end the whole process when
the display goes away, as gamescope restarting does: a watch's thread is parked for
good instead (see park_on_lost_display), marked lost, and the plugin goes on.
"""

import ctypes
import ctypes.util
import threading

GENERIC_EVENT = 35
XI_RAW_KEY_PRESS = 13
XI_ALL_MASTER_DEVICES = 1
XI_ALL_DEVICES = 0
XI_KEY_REPEAT = 1 << 16
EVENT_SIZE = 192
# Set on a thread that only watches keys: it may be parked if its display goes away.
WATCH_THREAD_FLAG = "hangul_key_watch"


# Set on such a thread once its display went away.
LOST_FLAG = "hangul_display_lost"


def _on_lost_display(_display):
    """Xlib's handler for a display that went away; Xlib ends the process once it
    returns. A watch's own thread never returns from here, and the rest of the
    process goes on; any other thread gets Xlib's own way."""
    thread = threading.current_thread()
    if getattr(thread, WATCH_THREAD_FLAG, False):
        setattr(thread, LOST_FLAG, True)
        on_lost = getattr(thread, WATCH_THREAD_FLAG)
        if callable(on_lost):
            on_lost()
        threading.Event().wait()
    return 0


def display_lost(thread):
    return getattr(thread, LOST_FLAG, False)


_LOST_DISPLAY_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(_on_lost_display)


def park_on_lost_display(thread, on_lost=None):
    """Mark a thread that runs a watch here, to be parked rather than end the
    process if its display goes away; on_lost, if given, is called first, from that
    thread."""
    setattr(thread, WATCH_THREAD_FLAG, on_lost or True)
    return thread


class EventMask(ctypes.Structure):
    _fields_ = [("deviceid", ctypes.c_int), ("mask_len", ctypes.c_int), ("mask", ctypes.POINTER(ctypes.c_ubyte))]


class DeviceInfo(ctypes.Structure):
    _fields_ = [
        ("deviceid", ctypes.c_int), ("name", ctypes.c_char_p), ("use", ctypes.c_int),
        ("attachment", ctypes.c_int), ("enabled", ctypes.c_int), ("num_classes", ctypes.c_int),
        ("classes", ctypes.c_void_p),
    ]


class RawKeyWatch:
    """Raw key presses on one display. fileno() to wait on, then presses() returns
    the keycodes pressed since, in order."""

    def __init__(self, display_name):
        x = self._x = ctypes.CDLL(ctypes.util.find_library("X11"))
        xi_name = ctypes.util.find_library("Xi")
        if not xi_name:
            raise OSError("no XInput library")
        xi = self._xi = ctypes.CDLL(xi_name)
        void = ctypes.c_void_p
        x.XOpenDisplay.restype = void
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [void]
        x.XQueryExtension.argtypes = [void, ctypes.c_char_p] + [ctypes.POINTER(ctypes.c_int)] * 3
        x.XPending.argtypes = [void]
        x.XNextEvent.argtypes = [void, void]
        x.XGetEventData.argtypes = [void, void]
        x.XFreeEventData.argtypes = [void, void]
        x.XConnectionNumber.argtypes = [void]
        x.XFlush.argtypes = [void]
        x.XCloseDisplay.argtypes = [void]
        x.XSetIOErrorHandler.restype = void
        x.XSetIOErrorHandler.argtypes = [void]
        x.XSetIOErrorHandler(ctypes.cast(_LOST_DISPLAY_HANDLER, void))
        xi.XISelectEvents.argtypes = [void, ctypes.c_ulong, ctypes.POINTER(EventMask), ctypes.c_int]
        xi.XIQueryDevice.restype = ctypes.POINTER(DeviceInfo)
        xi.XIQueryDevice.argtypes = [void, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        xi.XIFreeDeviceInfo.argtypes = [ctypes.POINTER(DeviceInfo)]
        self._display = x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open display {display_name}")
        opcode, first_event, first_error = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if not x.XQueryExtension(
            self._display, b"XInputExtension", ctypes.byref(opcode), ctypes.byref(first_event), ctypes.byref(first_error)
        ):
            x.XCloseDisplay(self._display)
            raise OSError("no XInput extension")
        self._opcode = opcode.value
        self._xtest = self._xtest_devices()
        bits = (ctypes.c_ubyte * 4)()
        bits[XI_RAW_KEY_PRESS >> 3] |= 1 << (XI_RAW_KEY_PRESS & 7)
        mask = EventMask(XI_ALL_MASTER_DEVICES, len(bits), bits)
        xi.XISelectEvents(self._display, x.XDefaultRootWindow(self._display), ctypes.byref(mask), 1)
        x.XFlush(self._display)

    def _xtest_devices(self):
        count = ctypes.c_int()
        devices = self._xi.XIQueryDevice(self._display, XI_ALL_DEVICES, ctypes.byref(count))
        try:
            return {
                devices[i].deviceid for i in range(count.value)
                if b"XTEST" in (devices[i].name or b"")
            }
        finally:
            self._xi.XIFreeDeviceInfo(devices)

    def fileno(self):
        return self._x.XConnectionNumber(self._display)

    def presses(self):
        x = self._x
        found = []
        event = ctypes.create_string_buffer(EVENT_SIZE)
        while x.XPending(self._display):
            x.XNextEvent(self._display, event)
            kind, extension, evtype = (
                int.from_bytes(event.raw[0:4], "little"),
                int.from_bytes(event.raw[32:36], "little"),
                int.from_bytes(event.raw[36:40], "little"),
            )
            if kind != GENERIC_EVENT or extension != self._opcode or evtype != XI_RAW_KEY_PRESS:
                continue
            if not x.XGetEventData(self._display, event):
                continue
            try:
                data = ctypes.c_void_p.from_buffer_copy(event.raw[48:56]).value
                raw = ctypes.string_at(data, 64)
                source = int.from_bytes(raw[52:56], "little")
                keycode = int.from_bytes(raw[56:60], "little")
                flags = int.from_bytes(raw[60:64], "little")
            finally:
                x.XFreeEventData(self._display, event)
            if source in self._xtest or flags & XI_KEY_REPEAT:
                continue
            found.append(keycode)
        return found

    def close(self):
        if self._display:
            self._x.XCloseDisplay(self._display)
            self._display = None


PROPERTY_NOTIFY = 28
PROPERTY_CHANGE_MASK = 1 << 22
XA_CARDINAL = 6


class RootPropertyWatch:
    """Numbers held in properties of a display's root window, such as gamescope's
    focused app. fileno() to wait on, then changed() tells whether any of them
    changed since, and values() reads them all."""

    def __init__(self, display_name, names):
        x = self._x = ctypes.CDLL(ctypes.util.find_library("X11"))
        void, ulong = ctypes.c_void_p, ctypes.c_ulong
        x.XOpenDisplay.restype = void
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ulong
        x.XDefaultRootWindow.argtypes = [void]
        x.XInternAtom.restype = ulong
        x.XInternAtom.argtypes = [void, ctypes.c_char_p, ctypes.c_int]
        x.XSelectInput.argtypes = [void, ulong, ctypes.c_long]
        x.XGetWindowProperty.argtypes = [
            void, ulong, ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ulong, ctypes.POINTER(ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ulong), ctypes.POINTER(ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        x.XFree.argtypes = [void]
        x.XPending.argtypes = [void]
        x.XNextEvent.argtypes = [void, void]
        x.XConnectionNumber.argtypes = [void]
        x.XFlush.argtypes = [void]
        x.XCloseDisplay.argtypes = [void]
        x.XSetIOErrorHandler.restype = void
        x.XSetIOErrorHandler.argtypes = [void]
        x.XSetIOErrorHandler(ctypes.cast(_LOST_DISPLAY_HANDLER, void))
        self._display = x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open display {display_name}")
        self._root = x.XDefaultRootWindow(self._display)
        self._atoms = {name: x.XInternAtom(self._display, name.encode(), 0) for name in names}
        x.XSelectInput(self._display, self._root, PROPERTY_CHANGE_MASK)
        x.XFlush(self._display)

    def fileno(self):
        return self._x.XConnectionNumber(self._display)

    def changed(self):
        x = self._x
        found = False
        event = ctypes.create_string_buffer(EVENT_SIZE)
        watched = set(self._atoms.values())
        while x.XPending(self._display):
            x.XNextEvent(self._display, event)
            if int.from_bytes(event.raw[0:4], "little") != PROPERTY_NOTIFY:
                continue
            # XPropertyEvent: type, serial, send_event, display, window, atom
            if int.from_bytes(event.raw[40:48], "little") in watched:
                found = True
        return found

    def values(self):
        """Each property's number, or None where it is missing."""
        return {name: self._read(atom) for name, atom in self._atoms.items()}

    def _read(self, atom):
        x = self._x
        kind, size, count, after = ctypes.c_ulong(), ctypes.c_int(), ctypes.c_ulong(), ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        if x.XGetWindowProperty(
            self._display, self._root, atom, 0, 1, 0, XA_CARDINAL,
            ctypes.byref(kind), ctypes.byref(size), ctypes.byref(count), ctypes.byref(after), ctypes.byref(data),
        ) != 0 or not data:
            return None
        try:
            if size.value != 32 or count.value < 1:
                return None
            # Format 32 comes as C longs, sign extended: the number is the low 32 bits.
            return ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0] & 0xFFFFFFFF
        finally:
            x.XFree(data)

    def close(self):
        if self._display:
            self._x.XCloseDisplay(self._display)
            self._display = None
