"""Which keys count as toggle keys while one of them holds the keyboard. No X server
is needed: the events are built by hand."""

import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.dont_write_bytecode = True

import xim_server  # noqa: E402

HANGUL, SPACE, LETTER = 130, 65, 38
SHIFT = xim_server.SHIFT_MASK


class ToggleKeyTest(unittest.TestCase):
    def setUp(self):
        self.log = []
        server = self.server = object.__new__(xim_server.XimServer)
        server._grabbed = {HANGUL: 0, SPACE: SHIFT}
        server._toggle_keycode = None
        server._toggle_down = set()
        server._toggle_debounce = xim_server.TOGGLE_DEBOUNCE_SECONDS
        server._last_toggle = 0.0
        server.on_toggle = lambda: self.log.append("toggle")
        server.on_toggle_released = lambda: self.log.append("released")
        server.on_grabbed_key = lambda keycode, state, pressed: self.log.append((keycode, pressed))

    def key(self, keycode, state=0, pressed=True):
        raw = bytearray(xim_server.EVENT_SIZE)
        struct.pack_into("<II", raw, xim_server.KEY_EVENT_STATE_OFFSET, state, keycode)
        self.server._on_grabbed_event(bytes(raw), pressed)

    def test_plain_space_while_the_hangul_key_is_down_is_typed(self):
        self.key(HANGUL)
        self.key(SPACE)
        self.key(SPACE, pressed=False)
        self.key(LETTER)
        self.assertEqual(self.log, ["toggle", (SPACE, True), (SPACE, False), (LETTER, True)])

    def test_shift_space_toggles(self):
        self.key(SPACE, SHIFT)
        self.key(SPACE, SHIFT, pressed=False)
        self.assertEqual(self.log, ["toggle", "released"])

    def test_space_that_toggled_types_nothing_once_shift_is_up(self):
        self.key(SPACE, SHIFT)
        self.key(SPACE)  # repeating, with Shift let go first
        self.key(SPACE, pressed=False)
        self.assertEqual(self.log, ["toggle", "released"])

    def test_letting_the_keys_go_forgets_the_key_that_toggled(self):
        self.key(SPACE, SHIFT)
        self.server._x = type("X", (), {"__getattr__": lambda _self, _name: lambda *args: None})()
        self.server._display = self.server._root = None
        self.server.ungrab_toggle_keys()
        self.assertIsNone(self.server._toggle_keycode)
        self.assertEqual(self.server._toggle_down, set())

    def test_a_held_toggle_key_toggles_once(self):
        self.key(HANGUL)
        self.server._last_toggle = -10.0  # well past the debounce
        self.key(HANGUL)  # repeats come as presses alone
        self.key(HANGUL)
        self.key(HANGUL, pressed=False)
        self.assertEqual(self.log, ["toggle", "released"])

    def test_pressed_again_after_letting_go_toggles_again(self):
        self.key(HANGUL)
        self.key(HANGUL, pressed=False)
        self.server._last_toggle = -10.0
        self.key(HANGUL)
        self.assertEqual(self.log, ["toggle", "released", "toggle"])

    def test_shift_space_held_toggles_once(self):
        self.key(SPACE, SHIFT)
        self.server._last_toggle = -10.0
        self.key(SPACE, SHIFT)
        self.key(SPACE)  # Shift let go, Space still repeating
        self.assertEqual(self.log, ["toggle"])

    def test_two_quick_presses_toggle_twice(self):
        self.key(SPACE, SHIFT)
        self.key(SPACE, SHIFT, pressed=False)
        self.server._last_toggle -= 0.1  # the second press, a tenth of a second later
        self.key(SPACE, SHIFT)
        self.assertEqual(self.log, ["toggle", "released", "toggle"])

    def test_a_press_sent_twice_toggles_once(self):
        self.key(HANGUL)
        self.key(HANGUL, pressed=False)
        self.key(HANGUL)  # at once
        self.assertEqual(self.log, ["toggle", "released"])

    def test_the_longer_debounce_stays_where_repeats_look_like_presses(self):
        self.server._toggle_debounce = xim_server.TOGGLE_DEBOUNCE_SECONDS_WITHOUT_REPEATS
        self.key(HANGUL)
        self.key(HANGUL, pressed=False)
        self.server._last_toggle -= 0.1
        self.key(HANGUL)
        self.assertEqual(self.log, ["toggle", "released"])

    def test_lock_keys_do_not_matter(self):
        self.key(SPACE, SHIFT | 0x12)
        self.assertEqual(self.log, ["toggle"])


if __name__ == "__main__":
    unittest.main()
