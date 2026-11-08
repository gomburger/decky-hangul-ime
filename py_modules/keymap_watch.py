"""Remembers which keys the Steam keyboard maps to Hangul, as it maps them.

The Steam keyboard types a character by putting a key map of its own in place,
with the character on a key, pressing that key, and putting the old map back. The
key reaches this process only once the game has passed it on, which at a low frame
rate can be after the old map is back: looked up then, the key reads as what it
usually is, a 1 for instance, and that is what the game would get.

So the map is watched here. Each time it changes, the keys that now carry Hangul
are noted, in order. A key that comes in and no longer reads as Hangul is matched
with the oldest note for that key, which is what the Steam keyboard meant by it.
"""

import collections
import ctypes
import ctypes.util
import struct
import time

XKB_USE_CORE_KEYBOARD = 0x100
# A whole new key map, as the Steam keyboard brings through the compositor, comes as
# a new keyboard; a change to some keys, as a map change.
XKB_NEW_KEYBOARD_NOTIFY = 0
XKB_MAP_NOTIFY = 1
XKB_NOTIFY_MASK = (1 << XKB_NEW_KEYBOARD_NOTIFY) | (1 << XKB_MAP_NOTIFY)
OFFSET_XKB_TYPE = 40
EVENT_SIZE = 192
QUEUED_ALREADY = 0
# A note that no key came in for is dropped after this long.
NOTE_SECONDS = 1.0
MAX_NOTES = 64


class KeymapWatch:
    """is_hangul tells whether a keysym types Hangul."""

    def __init__(self, display_name, is_hangul):
        x = self._x = ctypes.CDLL(ctypes.util.find_library("X11"))
        void, ulong, intp = ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_int)
        x.XOpenDisplay.restype = void
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XkbQueryExtension.argtypes = [void, intp, intp, intp, intp, intp]
        x.XkbSelectEvents.argtypes = [void, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
        x.XDisplayKeycodes.argtypes = [void, intp, intp]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ulong)
        x.XGetKeyboardMapping.argtypes = [void, ctypes.c_ubyte, ctypes.c_int, intp]
        x.XFree.argtypes = [void]
        x.XPending.argtypes = [void]
        x.XEventsQueued.argtypes = [void, ctypes.c_int]
        x.XNextEvent.argtypes = [void, void]
        x.XFlush.argtypes = [void]
        x.XConnectionNumber.argtypes = [void]
        x.XCloseDisplay.argtypes = [void]
        self._is_hangul = is_hangul
        self._display = x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open display {display_name}")
        opcode, event_base, error_base = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        major, minor = ctypes.c_int(1), ctypes.c_int(0)
        if not x.XkbQueryExtension(self._display, ctypes.byref(opcode), ctypes.byref(event_base),
                                   ctypes.byref(error_base), ctypes.byref(major), ctypes.byref(minor)):
            x.XCloseDisplay(self._display)
            raise OSError("no XKB")
        self._xkb_event = event_base.value
        x.XkbSelectEvents(self._display, XKB_USE_CORE_KEYBOARD, XKB_NOTIFY_MASK, XKB_NOTIFY_MASK)
        x.XFlush(self._display)
        low, high = ctypes.c_int(), ctypes.c_int()
        x.XDisplayKeycodes(self._display, ctypes.byref(low), ctypes.byref(high))
        self._low, self._count = low.value, high.value - low.value + 1
        self._hangul_keys = {}  # keycode -> keysym, in the map in place
        self._notes = collections.deque()  # (time, keycode, keysym)
        self._event = ctypes.create_string_buffer(EVENT_SIZE)
        self._read_map()

    def fileno(self):
        return self._x.XConnectionNumber(self._display)

    def queued(self):
        """Whether events already read from the connection wait here. select does
        not see those, so a loop must not sleep while there are any."""
        return self._x.XEventsQueued(self._display, QUEUED_ALREADY) > 0

    def _read_map(self):
        """Note each key that newly carries Hangul."""
        x = self._x
        per = ctypes.c_int()
        table = x.XGetKeyboardMapping(self._display, self._low, self._count, ctypes.byref(per))
        if not table:
            return
        try:
            found = {}
            for index in range(self._count):
                keysym = table[index * per.value]
                if keysym and self._is_hangul(keysym):
                    found[self._low + index] = keysym
        finally:
            x.XFree(table)
        now = time.monotonic()
        for keycode, keysym in found.items():
            if self._hangul_keys.get(keycode) != keysym:
                self._notes.append((now, keycode, keysym))
        while len(self._notes) > MAX_NOTES:
            self._notes.popleft()
        self._hangul_keys = found

    def process_events(self):
        """Read the map again after each change. Reading it can bring in the next
        change, so this goes on until none is left."""
        x = self._x
        while True:
            changed = False
            while x.XPending(self._display):
                x.XNextEvent(self._display, self._event)
                kind = struct.unpack_from("<i", self._event.raw, 0)[0]
                if kind == self._xkb_event:
                    xkb_type = struct.unpack_from("<i", self._event.raw, OFFSET_XKB_TYPE)[0]
                    changed = changed or xkb_type in (XKB_NEW_KEYBOARD_NOTIFY, XKB_MAP_NOTIFY)
            if not changed:
                return
            self._read_map()

    def _expire(self):
        limit = time.monotonic() - NOTE_SECONDS
        while self._notes and self._notes[0][0] < limit:
            self._notes.popleft()

    def take(self, keycode, keysym):
        """The keysym a key came in as, given what it reads as now. The oldest note
        for the key wins, since the map may have moved on since, back to the usual
        one or to the next character. With no note, the key is what it reads as."""
        self.process_events()
        self._expire()
        for index, (_when, noted_keycode, noted_keysym) in enumerate(self._notes):
            if noted_keycode == keycode:
                del self._notes[index]
                return noted_keysym
        return keysym

    def close(self):
        self._x.XCloseDisplay(self._display)
