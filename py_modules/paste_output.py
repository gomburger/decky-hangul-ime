"""Pasting finished text into a game, for games the IME cannot reach.

Some games never get the IME's text through Wine, and take Hangul typed as keys
for nothing, yet paste it fine. For those, finished text is put on the game
display's clipboard and Ctrl+V is pressed with XTest.

The game reads keys once a frame, so every step of the paste lasts longer than a
frame: the clipboard change, Ctrl before V, V held, Ctrl after V, and the pause
after Ctrl before anything else reaches the game. A shorter step lets a frame
see Ctrl already up, and the whole paste is lost.

V is pressed on a spare key mapped to v, not on the keyboard's V. Both pressing
one key at once, the X server would pass on only one press, and the player's ㅍ
or the paste would be lost. The Steam keyboard swaps in a whole key map of its own
for each key it types, and puts back the one it saw before, which wipes the spare
key's mapping, so it is mapped again whenever it is found wiped.

Ctrl is the keyboard's own. The keys pressed here come back through XIM while keys
are being forwarded, and go straight back to the game. They are told from the
player's by count: each key pressed here is expected back once. Keys the player
types while the paste holds Ctrl lose Ctrl, which is the paste's and not theirs.

Text held until Enter is pasted once, so a clash is unlikely, and it uses the real V:
some games read keys by their place on the keyboard and take the spare key for
nothing. It waits while the player is holding V.

A key the player pressed after a syllable waits until the paste is over, so the
order is kept. Text finished meanwhile goes in the next paste, so fast typing does
not fall behind.
"""

import collections
import ctypes
import ctypes.util
import struct

CONTROL_L, LETTER_V, LETTER_V_UPPER = 0xFFE3, 0x0076, 0x0056
# A key games leave alone, pressed to have a game turn its IME back on.
WAKE_KEYSYM = 0xFFD5  # F24
# Ctrl's and V's keycodes in the usual map, for when the map in place is someone else's.
CONTROL_KEYCODE = 37
REAL_V_KEYCODE = 55
SELECTION_REQUEST, SELECTION_NOTIFY = 30, 31
XA_ATOM, XA_STRING = 4, 31
EVENT_SIZE = 192
DEFAULT_GAP_SECONDS = 0.035
# How long to wait for the pressed V to come back through XIM before going on.
RETURN_TIMEOUT_SECONDS = 0.3
# While the Steam keyboard's key map is in place, how often to look again, and for
# how long at most before pressing anyway.
KEYS_READY_POLL_SECONDS = 0.01
KEYS_READY_TRIES = 50
# How many times a paste presses Ctrl again, after the key map changed under it.
MAX_RESTARTS = 3


class Paster:
    """Owns the clipboard of one display, and presses Ctrl+V there with XTest."""

    def __init__(self, display_name):
        x = self._x = ctypes.CDLL(ctypes.util.find_library("X11"))
        xtst = ctypes.util.find_library("Xtst")
        if not xtst:
            raise OSError("no XTest library")
        t = self._t = ctypes.CDLL(xtst)
        void, ulong = ctypes.c_void_p, ctypes.c_ulong
        x.XOpenDisplay.restype = void
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ulong
        x.XDefaultRootWindow.argtypes = [void]
        x.XInternAtom.restype = ulong
        x.XInternAtom.argtypes = [void, ctypes.c_char_p, ctypes.c_int]
        x.XCreateSimpleWindow.restype = ulong
        x.XCreateSimpleWindow.argtypes = [void, ulong] + [ctypes.c_int] * 2 + [ctypes.c_uint] * 3 + [ulong] * 2
        x.XSetSelectionOwner.argtypes = [void, ulong, ulong, ulong]
        x.XChangeProperty.argtypes = [void, ulong, ulong, ulong, ctypes.c_int, ctypes.c_int, void, ctypes.c_int]
        x.XSendEvent.argtypes = [void, ulong, ctypes.c_int, ctypes.c_long, void]
        x.XNextEvent.argtypes = [void, void]
        x.XPending.argtypes = [void]
        x.XFlush.argtypes = [void]
        x.XSync.argtypes = [void, ctypes.c_int]
        x.XConnectionNumber.argtypes = [void]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [void, ulong]
        x.XFree.argtypes = [void]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ulong)
        x.XGetKeyboardMapping.argtypes = [void, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        x.XDisplayKeycodes.argtypes = [void, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        x.XChangeKeyboardMapping.argtypes = [void, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ulong), ctypes.c_int]
        x.XCloseDisplay.argtypes = [void]
        x.XQueryKeymap.argtypes = [void, ctypes.c_char_p]
        t.XTestFakeKeyEvent.argtypes = [void, ctypes.c_uint, ctypes.c_int, ulong]

        self._display = x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open display {display_name}")
        self._clipboard = self._atom("CLIPBOARD")
        self._targets = self._atom("TARGETS")
        self._text_types = {
            self._atom("UTF8_STRING"), self._atom("TEXT"), self._atom("text/plain;charset=utf-8"), XA_STRING,
        }
        self._window = x.XCreateSimpleWindow(
            self._display, x.XDefaultRootWindow(self._display), 0, 0, 1, 1, 0, 0, 0
        )
        self._text = b""
        self.control = x.XKeysymToKeycode(self._display, CONTROL_L) or CONTROL_KEYCODE
        self.real_v = x.XKeysymToKeycode(self._display, LETTER_V) or REAL_V_KEYCODE
        self.v = self._spare_keycode()
        self._map_v(LETTER_V, LETTER_V_UPPER)
        self.wake = x.XKeysymToKeycode(self._display, WAKE_KEYSYM)

    def _atom(self, name):
        return self._x.XInternAtom(self._display, name.encode(), 0)

    def _keysym(self, keycode):
        per = ctypes.c_int()
        table = self._x.XGetKeyboardMapping(self._display, keycode, 1, ctypes.byref(per))
        if not table:
            return 0
        try:
            return table[0]
        finally:
            self._x.XFree(table)

    def _spare_keycode(self):
        """The highest keycode with nothing mapped, as far as possible from keys a
        layout uses."""
        x = self._x
        low, high = ctypes.c_int(), ctypes.c_int()
        x.XDisplayKeycodes(self._display, ctypes.byref(low), ctypes.byref(high))
        per = ctypes.c_int()
        count = high.value - low.value + 1
        table = x.XGetKeyboardMapping(self._display, low.value, count, ctypes.byref(per))
        if not table:
            raise OSError("cannot read the key map")
        try:
            free = [
                low.value + i for i in range(count)
                if all(table[i * per.value + j] == 0 for j in range(per.value))
            ]
        finally:
            x.XFree(table)
        free = [code for code in free if code > 8]
        if not free:
            raise OSError("no spare keycode")
        return free[-1]

    def _map_v(self, lower, upper):
        symbols = (ctypes.c_ulong * 2)(lower, upper)
        self._x.XChangeKeyboardMapping(self._display, self.v, 2, symbols, 1)
        self._x.XSync(self._display, 0)

    def keys_ready(self):
        """Whether Ctrl is Ctrl and the spare key is V right now. While the Steam
        keyboard types a key, the key map in place is its own. Once the usual map
        is back, the spare key is found wiped, and is mapped again; the game needs
        a moment to read that, so the keys are not ready yet then."""
        if self._keysym(self.control) != CONTROL_L:
            return False
        keysym = self._keysym(self.v)
        if keysym == LETTER_V:
            return True
        if keysym == 0:
            self._map_v(LETTER_V, LETTER_V_UPPER)
        return False

    def real_v_ready(self, must_be_up):
        """Whether Ctrl is Ctrl and the real V is V, and up if that matters. Pressed
        already, the X server would pass on no press of ours, and the paste would be
        lost."""
        if self._keysym(self.control) != CONTROL_L or self._keysym(self.real_v) != LETTER_V:
            return False
        if not must_be_up:
            return True
        keys = ctypes.create_string_buffer(32)
        self._x.XQueryKeymap(self._display, keys)
        return not keys.raw[self.real_v // 8] & (1 << (self.real_v % 8))

    def wake_ready(self):
        """Whether the wake key is F24 right now, as the Steam keyboard's own key
        map may be in place."""
        return bool(self.wake) and self._keysym(self.wake) == WAKE_KEYSYM

    def take_clipboard(self, text):
        self._text = text.encode()
        self._x.XSetSelectionOwner(self._display, self._clipboard, self._window, 0)
        self._x.XFlush(self._display)

    def key(self, keycode, pressed):
        self._t.XTestFakeKeyEvent(self._display, keycode, 1 if pressed else 0, 0)
        self._x.XFlush(self._display)

    def fileno(self):
        return self._x.XConnectionNumber(self._display)

    def process_events(self):
        """Answer anyone reading the clipboard."""
        x = self._x
        event = ctypes.create_string_buffer(EVENT_SIZE)
        while x.XPending(self._display):
            x.XNextEvent(self._display, event)
            if struct.unpack_from("<i", event.raw, 0)[0] == SELECTION_REQUEST:
                self._answer(event.raw)
        x.XFlush(self._display)

    def _answer(self, raw):
        x = self._x
        requestor, selection, target, prop, when = struct.unpack_from("<QQQQQ", raw, 40)
        if prop == 0:
            prop = target
        if target == self._targets:
            atoms = [self._targets] + sorted(self._text_types)
            data = (ctypes.c_ulong * len(atoms))(*atoms)
            x.XChangeProperty(self._display, requestor, prop, XA_ATOM, 32, 0, data, len(atoms))
        elif target in self._text_types:
            data = ctypes.create_string_buffer(self._text)
            x.XChangeProperty(self._display, requestor, prop, target, 8, 0, data, len(self._text))
        else:
            prop = 0
        reply = ctypes.create_string_buffer(EVENT_SIZE)
        struct.pack_into("<i", reply, 0, SELECTION_NOTIFY)
        struct.pack_into("<QQQQQ", reply, 32, requestor, selection, target, prop, when)
        x.XSendEvent(self._display, requestor, 0, 0, reply)

    def close(self):
        self._map_v(0, 0)
        self._x.XCloseDisplay(self._display)


class PasteOutput:
    """Takes GameOutput's place for a game that gets its text pasted. Finished text
    is pasted; the composition does not show in the game.

    server      has send_key_back(context, serial, event)
    timers      has call_later(seconds, function), returning a timer with cancel()
    paster      has take_clipboard(text), key(keycode, pressed), keys_ready(), control
                and v
    forwarding  tells whether keys are being forwarded here right now
    """

    def __init__(self, server, timers, paster, forwarding):
        self.server = server
        self.timers = timers
        self.paster = paster
        self.forwarding = forwarding
        self.on_idle = lambda: None
        self.gap = DEFAULT_GAP_SECONDS
        self.queue = collections.deque()
        self.busy = False
        self.paused = False
        self.v = paster.v  # the key this paste presses
        self.pasting = False  # from pressing Ctrl until the paste is over
        self.ctrl_held = False  # while the paste holds Ctrl down
        self.expected = collections.Counter()  # (keycode, pressed) still to come back
        self.v_back = 0  # V events of this paste that came back
        self.v_done = False  # V of this paste is down and up, or no longer waited for
        self.after_ctrl = False  # a step has passed since Ctrl went up
        self.map_changed = False  # another key map was in place while Ctrl was down
        self.restarts = 0
        self.wait_timer = None

    @property
    def idle(self):
        return not self.busy and not self.queue

    def commit(self, text, real_v=False):
        kind = "text with real v" if real_v else "text"
        if self.queue and self.queue[-1][0] == kind:
            self.queue[-1] = (kind, self.queue[-1][1] + text)
        else:
            self.queue.append((kind, text))
        self._run()

    def preedit(self, _text):
        pass

    def pause(self):
        """Keep what comes next until resume(). A paste in progress is left to
        finish."""
        if not self.busy:
            self.busy = self.paused = True

    def resume(self):
        if self.paused:
            self.busy = self.paused = False
            self._run()

    def send_key(self, context, serial, event):
        """Pass a key on to the game, after any paste still on its way."""
        if self.queue or self.busy:
            self.queue.append(("key", (context, serial, event)))
        else:
            self.server.send_key_back(context, serial, event)

    def forget_typed(self):
        pass

    def on_focus(self, _focused):
        pass

    def take_own_key(self, keycode, pressed):
        """Tell whether a key that came back through XIM is one this pressed. If it
        is, it counts as back, and the caller hands it straight to the game."""
        if not self.pasting or not self.expected[(keycode, pressed)]:
            return False
        self.expected[(keycode, pressed)] -= 1
        if keycode == self.v and not self.v_done:
            self.v_back += 1
            if self.v_back == 2 and self.wait_timer:
                self._v_back()
        return True

    def _run(self):
        while self.queue and not self.busy:
            kind, item = self.queue.popleft()
            if kind == "key":
                self.server.send_key_back(*item)
                # Let out together, a press and its release would land in one
                # frame, and the game would never see the key down.
                self.busy = True
                self.timers.call_later(self.gap, self._key_spaced)
                continue
            self.busy = True
            self.v = self.paster.real_v if kind == "text with real v" else self.paster.v
            self.paster.take_clipboard(item)
            self.timers.call_later(self.gap, self._paste)
        if self.idle:
            self.on_idle()

    def _key_spaced(self):
        self.busy = False
        self._run()

    def _press(self, keycode, pressed):
        if self.forwarding():
            self.expected[(keycode, pressed)] += 1
        if keycode == self.paster.control:
            self.ctrl_held = pressed
        self.paster.key(keycode, pressed)

    def _paste(self):
        self.pasting = True
        self.map_changed = False
        self.restarts = 0
        self.v_back = 0
        self.v_done = False
        self.after_ctrl = False
        self._step(0, 0)

    def _step(self, index, tries):
        """Press or let go of the next key, once the keys are what they should be,
        then wait a step before the one after."""
        real_v = self.v != self.paster.v
        # Once this paste has pressed V, V is down because of it.
        map_ready = self.paster.real_v_ready(index < 2) if real_v else self.paster.keys_ready()
        if tries < KEYS_READY_TRIES and not map_ready:
            if index == 1:
                self.map_changed = True
            self.timers.call_later(KEYS_READY_POLL_SECONDS, lambda: self._step(index, tries + 1))
            return
        control, v = self.paster.control, self.v
        if index == 1 and self.map_changed and self.restarts < MAX_RESTARTS:
            # Another key map came and went while Ctrl was down, which may have
            # lost it: press it again, a step before V.
            self.map_changed = False
            self.restarts += 1
            self._press(control, False)
            self.timers.call_later(self.gap, lambda: self._step(0, 0))
            return
        keycode, pressed = ((control, True), (v, True), (v, False), (control, False))[index]
        self._press(keycode, pressed)
        if index == 2:
            # V is down and up. Wait for both to be handed to the game, if keys
            # come through here; otherwise there is nothing to wait for.
            if self.forwarding() and self.v_back < 2:
                self.wait_timer = self.timers.call_later(RETURN_TIMEOUT_SECONDS, self._v_back)
            else:
                self._v_back()
        if index < 3:
            self.timers.call_later(self.gap, lambda: self._step(index + 1, 0))
        else:
            self.timers.call_later(self.gap, self._ctrl_settled)

    def _v_back(self):
        if self.wait_timer:
            self.wait_timer.cancel()
            self.wait_timer = None
        if not self.pasting or self.v_done:
            return
        self.v_done = True
        self._finish()

    def _ctrl_settled(self):
        self.after_ctrl = True
        self._finish()

    def _finish(self):
        if self.v_done and self.after_ctrl:
            self._over()

    def _over(self):
        # Keys that never came back are not waited for any more, so that the
        # player's own are not taken for them.
        self.expected.clear()
        self.pasting = False
        self.busy = False
        self._run()
