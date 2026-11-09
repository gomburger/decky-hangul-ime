#!/usr/bin/env python3
"""Runs the XIM server for one game display, and does all the Hangul typing there.

The server has to exist before a game starts, since Wine looks for it then, so this
process runs while any game is registered.

Wine passes a game's keys here before acting on them only while something here needs
them: while Hangul mode is on for physical keys, or while the Steam keyboard is open
in a game using it. Keys from the Steam keyboard come in already composed and are
read by SteamTyping. Physical keys are composed here while Hangul mode is on.
Everything else goes back to the game unchanged.
"""

import collections
import heapq
import itertools
import os
import select
import signal
import sys
import time

import state_files
from keymap_watch import KeymapWatch
from paste_output import DEFAULT_GAP_SECONDS, PasteOutput, Paster
from state_files import state_path
from steam_focus import SteamFocus
from steam_typing import BACKSPACE_KEYSYM, SteamTyping, hangul_text
from text_batch import BatchDisplay, TextBatch
from text_overlay import TextOverlay
from typing_session import BACKSPACE_CODE, OTHER_MODIFIERS, PHYSICAL_JAMO, TypingSession
from xim_server import TOGGLE_KEYS, XimServer

SERVER_NAME = "hangulbridge"
EVDEV_TO_X11_KEYCODE = 8
KEY_PRESS = 2
CONTROL_MASK = 0x04
MOD1_MASK = 0x08
# Shift, Control, Alt, Caps Lock and Super, which never become text.
MODIFIER_CODES = frozenset((29, 42, 54, 56, 58, 97, 100, 125, 126))
STATE_CHECK_SECONDS = 0.25
# Long enough for the game to take Backspace in before the text after it, which
# takes a frame, and for games that read keys once a frame to see it at all.
BACKSPACE_HOLD_SECONDS = 0.05
# Waking a game's IME: how long the wake key is held, longer than a frame; how long
# before it may be pressed again; how long its events are waited for back.
WAKE_HOLD_SECONDS = 0.035
WAKE_RETURN_SECONDS = 1.0
# After the wake key, time for the game to turn its IME on, two frames at 30 fps,
# and the longest the text waits for that.
WAKE_SETTLE_SECONDS = 0.07
WAKE_GIVE_UP_SECONDS = 0.4
# Enter, which sends a line and closes the text field; opened again, some games
# leave their IME off until a key.
ENTER_CODES = frozenset((28, 96))  # Enter, keypad Enter
ESCAPE_CODE = 1
# While a toggle key is held, X sends every key to this, so a paste would miss the
# game. Once it is up, a moment for a Shift held with it too.
TOGGLE_SETTLE_SECONDS = 0.08
TOGGLE_RELEASE_GIVE_UP_SECONDS = 1.0
FIRST_PRINTABLE_KEYSYM, LAST_PRINTABLE_KEYSYM = 0x20, 0x7E
# The wake key goes this long after Enter at least: before, the field opened by
# Enter does not take the key yet.
ENTER_SETTLE_SECONDS = 0.5
# After text kept back goes out, before the composition that follows it.
HELD_PREEDIT_SECONDS = 0.04
# The display Steam's own UI runs on, where gamescope shows what has focus.
STEAM_DISPLAY = os.environ.get("HANGUL_BRIDGE_STEAM_DISPLAY", "")
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")


def read_milliseconds(path, default, minimum=1):
    try:
        with open(path, encoding="utf-8") as value:
            return max(minimum, int(value.read().strip())) / 1000
    except (OSError, ValueError):
        return default


def read_toggle_keys(path):
    """The toggle keys chosen, all of them until the plugin says otherwise."""
    try:
        with open(path, encoding="utf-8") as names:
            text = names.read().strip()
    except OSError:
        return tuple(TOGGLE_KEYS)
    return tuple(name for name in TOGGLE_KEYS if name in text.split(","))


def read_flag(path):
    try:
        with open(path, encoding="utf-8") as flag:
            return flag.read().strip() == "1"
    except OSError:
        return False


class Timer:
    def __init__(self, function):
        self.function = function

    def cancel(self):
        self.function = None


class Timers:
    """Timers for a loop built on select, returning the time until the next one."""

    def __init__(self):
        self._heap = []
        self._order = itertools.count()

    def call_later(self, seconds, function):
        timer = Timer(function)
        heapq.heappush(self._heap, (time.monotonic() + seconds, next(self._order), timer))
        return timer

    def run_due(self):
        now = time.monotonic()
        while self._heap and self._heap[0][0] <= now:
            timer = heapq.heappop(self._heap)[2]
            if timer.function:
                timer.function()

    def seconds_until_next(self):
        return max(0.0, self._heap[0][0] - time.monotonic()) if self._heap else None


class GameOutput:
    """Sends text and keys to the game, keeping text if the game has no input focus.

    In direct mode the composition is typed as text, and erased with Backspace,
    one per character, when it changes. Finished text that starts with what is
    already typed only needs the rest typed after it.

    A game handles the IME's text before keyboard input that arrived at the same
    time, as Windows hands a program its posted messages first. So in direct mode
    everything goes out in turn: Backspace is held, let go, and only then is the
    text after it sent. Keys going on to the game wait their turn too, so Enter
    cannot overtake the last syllable.

    With text erasing on, direct mode erases with Backspace characters at the start
    of the text instead of Backspace keys. Erasing and text then go as one message,
    which keeps their order with no waiting, in games that take a Backspace
    character as erasing.

    Text the game has no IME to take waits until it has; erasing meanwhile takes
    from what waits, so nothing already in the field is erased in its place. While
    a key that wakes the game's IME is on its way, hold() keeps everything back, in
    order, until release().
    """

    def __init__(self, server, timers, backspace_events):
        self.server = server
        self.timers = timers
        self.backspace_events = backspace_events
        self.waiting = ""
        self.direct = False
        self.erase_text = False
        self.backspace_hold = BACKSPACE_HOLD_SECONDS
        self.typed = ""
        self.queue = collections.deque()
        self.held = None  # the release of a Backspace being held
        self.holding = False
        self.deferred = []  # (method name, arguments) kept back while holding

    def hold(self):
        self.holding = True

    def release(self):
        """Send what was kept back. Sent one by one it would all land in one frame,
        and some games keep only the last text of a frame: text between keys goes
        as one."""
        self.holding = False
        deferred, self.deferred = self.deferred, []
        text, shown = "", None
        for name, arguments in deferred:
            if name == "commit":
                text += arguments[0]
                shown = None
            elif name == "preedit":
                shown = arguments[0]
            else:
                self._send_held(text, shown, False)
                text, shown = "", None
                self.send_key(*arguments)
        self._send_held(text, shown, True)

    def _send_held(self, text, shown, last):
        if self.direct:
            if text or shown:
                self.commit(text + (shown or ""))
                self.typed = shown or ""
            return
        if text:
            self.commit(text)
        if shown is None:
            return
        if text and last:
            # The composition after the text, in a later frame.
            self.holding = True
            self.deferred = [("preedit", (shown,))]
            self.timers.call_later(HELD_PREEDIT_SECONDS, self.release)
        else:
            self.preedit(shown)

    def _send_text(self, text, backspaces=0):
        if self.waiting or not self.server.commit(text, backspaces):
            kept = len(self.waiting) - min(backspaces, len(self.waiting))
            self.waiting = self.waiting[:kept] + text

    def _erase(self, count):
        for _ in range(count):
            self.queue.append(("erase", None))

    def _queue(self, kind, item):
        self.queue.append((kind, item))
        self._run()

    def _run(self):
        while self.queue and not self.held:
            kind, item = self.queue.popleft()
            if kind == "text":
                self._send_text(item)
            elif kind == "key":
                self.server.send_key_back(*item)
            elif self.erase_text:
                # Every erase in a row, and the text after them, as one message.
                backspaces = 1
                while self.queue and self.queue[0][0] == "erase":
                    self.queue.popleft()
                    backspaces += 1
                text = self.queue.popleft()[1] if self.queue and self.queue[0][0] == "text" else ""
                self._send_text(text, backspaces)
            elif self.waiting:
                self.waiting = self.waiting[:-1]
            else:
                events = self.backspace_events()
                if not events:
                    continue
                context, serial, press, release = events
                self.server.send_key_back(context, serial, press)
                self.held = (context, serial, release)
                self.timers.call_later(self.backspace_hold, self._let_go)

    def _let_go(self):
        context, serial, release = self.held
        self.held = None
        self.server.send_key_back(context, serial, release)
        self._run()

    def send_key(self, context, serial, event):
        """Pass a key on to the game, after anything still on its way."""
        if self.holding:
            self.deferred.append(("send_key", (context, serial, event)))
            return
        if self.queue or self.held:
            self._queue("key", (context, serial, event))
        else:
            self.server.send_key_back(context, serial, event)

    def commit(self, text):
        if self.holding:
            self.deferred.append(("commit", (text,)))
            return
        if not self.direct:
            self._send_text(text)
            return
        if self.typed:
            if text.startswith(self.typed):
                text = text[len(self.typed):]
            else:
                self._erase(len(self.typed))
            self.typed = ""
        if text:
            self._queue("text", text)
        else:
            self._run()

    def preedit(self, text):
        if self.holding:
            self.deferred.append(("preedit", (text,)))
            return
        if not self.direct:
            # With no IME on, it shows once the game has one, as the daemon then
            # shows it again.
            self.server.preedit(text)
            return
        if text == self.typed:
            return
        self._erase(len(self.typed))
        self.typed = text
        if text:
            self._queue("text", text)
        else:
            self._run()

    def forget_typed(self):
        """Stop tracking what was typed, where the game has just left the field.
        Erasing still on its way is dropped; text still on its way is sent."""
        self.typed = ""
        kept = [(kind, item) for kind, item in self.queue if kind != "erase"]
        self.queue = collections.deque(kept)

    def on_focus(self, focused):
        if focused and self.waiting:
            text, self.waiting = self.waiting, ""
            self.server.commit(text)


class Daemon:
    def __init__(self, display, server_name=SERVER_NAME):
        self.server = XimServer(display, server_name)
        self.timers = Timers()
        self.game_output = GameOutput(self.server, self.timers, self._backspace_events)
        # The output in use: game_output, or paste_output for a game that pastes.
        self.output = self.game_output
        self.wake_expected = collections.Counter()  # wake key events still to come back
        self.entered = False  # Enter reached the game in Hangul mode, and no jamo since
        self.last_enter = -ENTER_SETTLE_SECONDS  # when Enter last went to the game
        self.release_timer = None
        self.paste_output = None
        try:
            self.paster = Paster(display)
        except OSError:
            self.paster = None  # pasting is not possible here; the IME is used instead
        else:
            self.paste_output = PasteOutput(self.server, self.timers, self.paster, lambda: self.forwarding)
            self.paste_output.on_idle = self._update_forwarding
        try:
            self.keymap = KeymapWatch(display, lambda keysym: hangul_text(keysym) is not None)
        except OSError:
            self.keymap = None  # keys are read as the map is when they come in
        self.session = TypingSession(self.output, self.timers)
        self.overlay = None
        self.batch = None  # the text held until sent
        self.batch_display = None
        self.paste_batch = False
        self.batch_row_missing = False
        self.was_steam_typing = False
        if self.paste_output and STEAM_DISPLAY:
            try:
                self.overlay = TextOverlay(STEAM_DISPLAY, self.timers)
            except OSError:
                pass  # physical keys' text is then pasted as it is typed
        self.steam = SteamTyping(self.session, self.timers)
        self.server.on_reset = self.session.on_reset
        self.server.on_focus = self._on_focus
        self.server.on_key = self._on_key
        self.server.on_toggle = self._toggle_mode
        self.server.on_toggle_released = self._toggle_released
        self.server.on_grabbed_key = self._grabbed_key
        self.toggle_timer = None

        def path(name):
            return state_path(RUNTIME_DIR, display, name)

        self.mode_file = path(state_files.MODE)
        self.mode_request_file = path(state_files.MODE_REQUEST)
        self.physical_file = path(state_files.PHYSICAL)
        self.virtual_file = path(state_files.VIRTUAL)
        self.direct_file = path(state_files.DIRECT)
        self.erase_text_file = path(state_files.ERASE_TEXT)
        self.backspace_hold_file = path(state_files.BACKSPACE_HOLD_MS)
        self.paste_file = path(state_files.PASTE)
        self.paste_batch_file = path(state_files.PASTE_BATCH)
        self.batch_row_missing_file = path(state_files.BATCH_ROW_MISSING)
        if self.paste_output:
            self.batch_display = BatchDisplay(path(state_files.BATCH_TEXT), lambda: self.steam_typing, self.overlay)
            self.batch = TextBatch(self.paste_output, self.batch_display)
        self.toggle_keys_file = path(state_files.TOGGLE_KEYS)
        self.toggle_keys = tuple(TOGGLE_KEYS)
        self.paste_gap_file = path(state_files.PASTE_GAP_MS)
        self.wake_wait_file = path(state_files.WAKE_WAIT_MS)
        self.ime_wake_file = path(state_files.IME_WAKE)
        self.ime_wake = False
        self.wake_wait = ENTER_SETTLE_SECONDS
        self.physical_active = False
        self.virtual_active = False
        self.hangul_mode = False
        self.forwarding = False
        self.used_codes = set()
        self.pressed_in_game = set()
        self.parent = os.getppid()
        self.last_key = None
        self.focus = None
        if STEAM_DISPLAY:
            try:
                self.focus = SteamFocus(STEAM_DISPLAY)
            except OSError:
                pass

    @property
    def keyboard_open(self):
        """Whether the Steam keyboard is open. Without a way to tell, it counts as
        open, so its typing is never missed."""
        return self.focus.keyboard_open if self.focus else True

    @property
    def steam_typing(self):
        return self.virtual_active and self.keyboard_open

    def _update_forwarding(self):
        """Have Wine pass keys here only while something here needs them, since each
        key then makes a round trip."""
        # Also until a paste is over, so that keys typed after it stay behind it.
        pasting = self.paste_output is not None and not self.paste_output.idle
        forward = self.hangul_mode or self.steam_typing or pasting
        if forward != self.forwarding:
            self.forwarding = forward
            self.server.set_forward_keys(forward)

    def _set_hangul_mode(self, enabled):
        if enabled == self.hangul_mode:
            return
        self.hangul_mode = enabled
        if not enabled:
            self.session.finish()
            self._send_batch()
        self._update_forwarding()
        with open(self.mode_file, "w", encoding="utf-8") as mode:
            mode.write("1" if enabled else "0")

    def _toggle_mode(self):
        if self.hangul_mode and self.output is self.paste_output and self._has_text_to_send():
            self.paste_output.pause()
            self._wait_for_toggle(TOGGLE_RELEASE_GIVE_UP_SECONDS)
        self._set_hangul_mode(not self.hangul_mode)

    def _has_text_to_send(self):
        return bool(self.session.composing or self.session.pending or (self.batching and not self.batch.empty))

    def _wait_for_toggle(self, seconds):
        if self.toggle_timer:
            self.toggle_timer.cancel()
        self.toggle_timer = self.timers.call_later(seconds, self._toggle_is_up)

    def _toggle_released(self):
        if self.toggle_timer:
            self._wait_for_toggle(TOGGLE_SETTLE_SECONDS)

    def _grabbed_key(self, keycode, state, pressed):
        """A key typed while a toggle key is still down, which X sends here instead
        of to the game. It goes the way any key does, in the new mode. An Alt in its
        state is the toggle key's."""
        state &= ~MOD1_MASK
        if not self.last_key:
            # Nothing to hand a key to the game with: only a jamo can be kept.
            if pressed:
                self._take_key(keycode, keycode - EVDEV_TO_X11_KEYCODE, state)
            return
        context, serial, event = self.last_key
        event = bytearray(event)
        event[0] = KEY_PRESS if pressed else KEY_PRESS + 1
        event[1] = keycode
        event[28:30] = (state & 0xFFFF).to_bytes(2, "little")
        self._on_key(context, serial, bytes(event))

    def _toggle_is_up(self):
        self.toggle_timer = None
        self.paste_output.resume()

    def _check_state(self):
        """Follow which keyboards the focused game uses. The toggle keys are held
        only while a game using physical keys has focus, so that other games still
        receive those keys."""
        physical = read_flag(self.physical_file)
        toggle_keys = read_toggle_keys(self.toggle_keys_file)
        if toggle_keys != self.toggle_keys:
            self.toggle_keys = toggle_keys
            if self.physical_active:
                self.server.ungrab_toggle_keys()
                self.server.grab_toggle_keys(toggle_keys)
        if physical != self.physical_active:
            self.physical_active = physical
            if physical:
                self.server.grab_toggle_keys(self.toggle_keys)
            else:
                self.server.ungrab_toggle_keys()
                # The game has lost focus, and the text would go nowhere.
                self._drop_batch()
                self._set_hangul_mode(False)
        virtual = read_flag(self.virtual_file)
        if virtual != self.virtual_active:
            self.virtual_active = virtual
            self._steam_typing_changed()
        self._set_paste(read_flag(self.paste_file))
        self.paste_batch = read_flag(self.paste_batch_file)
        self.batch_row_missing = read_flag(self.batch_row_missing_file)
        self._update_batch()
        self._set_direct(read_flag(self.direct_file))
        self.game_output.erase_text = read_flag(self.erase_text_file)
        self.game_output.backspace_hold = read_milliseconds(self.backspace_hold_file, BACKSPACE_HOLD_SECONDS)
        if self.paste_output:
            self.paste_output.gap = read_milliseconds(self.paste_gap_file, DEFAULT_GAP_SECONDS)
        self.wake_wait = read_milliseconds(self.wake_wait_file, ENTER_SETTLE_SECONDS, minimum=0)
        self.ime_wake = read_flag(self.ime_wake_file)

    def _steam_typing_changed(self):
        if not self.steam_typing:
            self.steam.finish()
        self._update_batch()
        self._update_forwarding()

    def _set_direct(self, direct):
        if direct == self.game_output.direct:
            return
        self.steam.finish()
        self.session.finish()
        self.game_output.direct = direct
        self.game_output.forget_typed()
        if self.output is self.game_output:
            self.session.direct = direct

    def _set_paste(self, paste):
        """Switch to pasting, or back. Text finished before the switch still goes
        the way it was going. Pasting commits at once, as direct mode does, since
        no composition shows in the game to wait for."""
        paste = paste and self.paste_output is not None
        if paste == (self.output is self.paste_output):
            return
        self.steam.finish()
        self.session.finish()
        self._send_batch()
        self.output = self.paste_output if paste else self.game_output
        self.session.output = self.output
        self.session.direct = paste or self.game_output.direct

    @property
    def batching(self):
        return self.batch is not None and self.session.output is self.batch

    def _send_batch(self):
        if self.batch:
            self.batch.flush()

    def _drop_batch(self):
        if self.batching:
            self.session.drop_composing()
            self.batch.clear()

    def _update_batch(self):
        """Hold typed text, where that is wanted and there is a place to show it.
        Where there is none, text is pasted as it is typed. Closing the Steam
        keyboard sends what is held, as the typing is over."""
        steam_typing = self.steam_typing
        keyboard_closed = self.was_steam_typing and not steam_typing
        moved = self.was_steam_typing != steam_typing
        self.was_steam_typing = steam_typing
        shown = not self.batch_row_missing if steam_typing else self.overlay is not None
        use = self.batch is not None and self.paste_batch and self.output is self.paste_output and shown
        if use == self.batching:
            if self.batching and keyboard_closed:
                self.session.finish()
                self.batch.flush()
            elif self.batching and moved:
                self.batch.refresh()
            return
        self.session.finish()
        if use:
            self.session.output = self.batch
        else:
            self.batch.flush()
            self.session.output = self.output

    def _batch_key(self, keycode, code, state, keysym):
        """Handle a key that edits the held text, returning whether it did. Before
        the first jamo nothing is held, and such keys go to the game as usual."""
        if self.batch.empty and self.session.composer.empty:
            return False
        if state & OTHER_MODIFIERS:
            return False
        if code == ESCAPE_CODE:
            self._drop_batch()
            return True
        if code == BACKSPACE_CODE:
            if not self.session.composer.empty:
                return False  # the session takes it off the syllable
            self.batch.erase()
            return True
        if code in PHYSICAL_JAMO and self.hangul_mode:
            return False
        if keysym is None:
            keysym = self.server.lookup_keysym(keycode, state)
        if keysym and FIRST_PRINTABLE_KEYSYM <= keysym <= LAST_PRINTABLE_KEYSYM:
            self.session.finish()
            self.batch.commit(chr(keysym))
            return True
        return False

    def _on_focus(self, focused):
        self.game_output.on_focus(focused)
        if focused and self.output is self.game_output and not self.game_output.direct:
            # The composition, if the game had no IME as it changed, shows now.
            composing = self.session.composing
            if composing and not self.session.hold:
                self.game_output.preedit(composing)
        if not focused and self.output is self.game_output and self.game_output.direct:
            # What was typed stays in the field the game left, and must not be
            # erased from wherever the game goes next.
            self.game_output.forget_typed()
            self.session.drop_composing()
            self.steam.drop()

    def _key_events(self, keycode):
        """Build a key press and release to send through XIM like keys coming back,
        from the last key Wine passed here, which carries the window."""
        if not self.last_key:
            return None
        context, serial, event = self.last_key
        press = bytearray(event)
        press[0] = KEY_PRESS
        press[1] = keycode
        press[28:30] = b"\0\0"
        release = bytearray(press)
        release[0] = KEY_PRESS + 1
        return context, serial, bytes(press), bytes(release)

    def _backspace_events(self):
        return self._key_events(BACKSPACE_CODE + EVDEV_TO_X11_KEYCODE)

    def _check_mode_request(self):
        """Follow the plugin's mode button, if a game using physical keys has focus."""
        try:
            with open(self.mode_request_file, encoding="utf-8") as request:
                wanted = request.read().strip()
            os.unlink(self.mode_request_file)
        except OSError:
            return
        if self.physical_active:
            self._set_hangul_mode(wanted == "1")

    def _take_key(self, keycode, code, state):
        """Handle a key press here, returning False if it should go to the game."""
        if code in MODIFIER_CODES:
            return False
        keysym = None
        if self.steam_typing:
            keysym = self.server.lookup_keysym(keycode, state)
            if self.keymap:
                keysym = self.keymap.take(keycode, keysym)
            text = hangul_text(keysym)
            if text:
                self.steam.on_text(text)
                return True
            if keysym == BACKSPACE_KEYSYM and self.steam.on_backspace():
                return True
            self.steam.finish()
        if self.batching and self._batch_key(keycode, code, state, keysym):
            return True
        if self.hangul_mode and self.session.physical_key(code, state):
            return True
        if self.batching:
            self.batch.flush()  # before the key, which then follows the paste
        return False

    def _wake_game(self):
        """Press the wake key, for a game that may have its IME off: some games turn
        it back on, as their text field opens again, only at a key, and every jamo
        key is taken here. The text waits until the game has had the key, so it is
        not lost before the IME is on."""
        if self.output is not self.game_output or not self.paster:
            return
        self.game_output.hold()
        # Typed fast, the jamo comes before the field takes keys: the key waits.
        delay = max(0.0, self.last_enter + self.wake_wait - time.monotonic())
        self.release_timer = self.timers.call_later(delay + WAKE_GIVE_UP_SECONDS, self._end_wake_hold)
        if delay:
            self.timers.call_later(delay, self._press_wake_key)
        else:
            self._press_wake_key()

    def _press_wake_key(self):
        if not self.paster.wake_ready():
            self._end_wake_hold()
            return
        wake = self.paster.wake
        if self.forwarding:
            self.wake_expected[(wake, True)] += 1
            self.wake_expected[(wake, False)] += 1
            self.timers.call_later(WAKE_RETURN_SECONDS, self.wake_expected.clear)
        else:
            # Without keys coming through here, the release is not seen: wait as if
            # it came back at once.
            if self.release_timer:
                self.release_timer.cancel()
            self.release_timer = self.timers.call_later(WAKE_HOLD_SECONDS + WAKE_SETTLE_SECONDS, self._end_wake_hold)
        self.paster.key(wake, True)
        self.timers.call_later(WAKE_HOLD_SECONDS, lambda: self.paster.key(wake, False))

    def _wake_key_back(self):
        """The wake key is up and handed to the game: give it the time to act on it."""
        if self.release_timer:
            self.release_timer.cancel()
            self.release_timer = self.timers.call_later(WAKE_SETTLE_SECONDS, self._end_wake_hold)

    def _end_wake_hold(self):
        if self.release_timer:
            self.release_timer.cancel()
            self.release_timer = None
        self.game_output.release()

    def _before_key(self, code, state):
        """Wake the game before the first jamo after Enter in Hangul mode, which
        is typed into a text field opened again."""
        if not self.ime_wake or not self.entered or not self.hangul_mode or code not in PHYSICAL_JAMO or state & OTHER_MODIFIERS:
            return
        self.entered = False
        self._wake_game()

    def _on_key(self, context, serial, event):
        if self.paster and event[1] == self.paster.wake:
            pressed = event[0] & 0x7F == KEY_PRESS
            if self.wake_expected[(event[1], pressed)]:
                # The wake key pressed here: straight to the game, which is what it
                # is for, leaving the composition alone.
                self.wake_expected[(event[1], pressed)] -= 1
                self.server.send_key_back(context, serial, event)
                if not pressed:
                    self._wake_key_back()
                return
        paste = self.paste_output
        if paste:
            if paste.take_own_key(event[1], event[0] & 0x7F == KEY_PRESS):
                # The Ctrl+V a paste pressed: straight back to the game.
                self.server.send_key_back(context, serial, event)
                return
            if paste.ctrl_held:
                # The paste holds Ctrl, not the player: the key goes without it.
                event = bytearray(event)
                event[28] &= ~CONTROL_MASK & 0xFF
                event = bytes(event)
        self.last_key = (context, serial, event)
        pressed = event[0] & 0x7F == KEY_PRESS
        keycode = event[1]
        code = keycode - EVDEV_TO_X11_KEYCODE
        state = int.from_bytes(event[28:30], "little")

        if pressed:
            self._before_key(code, state)
            if self._take_key(keycode, code, state):
                if code not in self.pressed_in_game:
                    self.used_codes.add(code)
                return
            if code in ENTER_CODES and self.hangul_mode and self.ime_wake:
                self.entered = True
            if code in ENTER_CODES:
                self.last_enter = time.monotonic()
            self.used_codes.discard(code)
            self.pressed_in_game.add(code)
        else:
            # A release is kept back only when the game never saw a press of that key.
            # Holding a key repeats its press, so Backspace can delete a jamo here and
            # then go to the game: dropping its release at that point would leave the
            # key held down in the game.
            if code in ENTER_CODES:
                self.last_enter = time.monotonic()
            if code in self.pressed_in_game:
                self.pressed_in_game.discard(code)
            elif code in self.used_codes:
                self.used_codes.discard(code)
                return
        self.output.send_key(context, serial, event)

    def run(self):
        """Serve until the plugin that started this is gone. A server left behind
        would hold the toggle keys and leave games talking to the wrong one."""
        next_check = 0.0
        while True:
            now = time.monotonic()
            if now >= next_check:
                if os.getppid() != self.parent:
                    return
                self._check_state()
                self._check_mode_request()
                next_check = now + STATE_CHECK_SECONDS
            wait = STATE_CHECK_SECONDS
            due = self.timers.seconds_until_next()
            if due is not None:
                wait = min(wait, due)
            if self.keymap:
                # A change of key map has to be read at once, while the Steam
                # keyboard's map is still in place.
                self.keymap.process_events()
                if self.keymap.queued():
                    wait = 0
            sources = [self.server.fileno()] + ([self.focus.fileno()] if self.focus else [])
            if self.paster:
                sources.append(self.paster.fileno())
            if self.keymap:
                sources.append(self.keymap.fileno())
            select.select(sources, [], [], wait)
            self.server.process_events()
            if self.paster:
                self.paster.process_events()
            if self.keymap:
                self.keymap.process_events()
            if self.focus and self.focus.process_events():
                self._steam_typing_changed()
            self.timers.run_due()

    def close(self):
        self.server.close()
        if self.focus:
            self.focus.close()
        if self.paster:
            self.paster.close()
        if self.batch_display:
            self.batch_display.show("")
        if self.overlay:
            self.overlay.close()
        if self.keymap:
            self.keymap.close()
        for path in (self.mode_file, self.mode_request_file):
            if os.path.exists(path):
                os.unlink(path)


def main():
    display = sys.argv[1] if len(sys.argv) > 1 else os.environ["DISPLAY"]
    server_name = sys.argv[2] if len(sys.argv) > 2 else SERVER_NAME
    daemon = Daemon(display, server_name)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        daemon.run()
    finally:
        daemon.close()


if __name__ == "__main__":
    main()
