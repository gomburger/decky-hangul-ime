"""Pasting: the order of text and keys, the timing of each step, and the switch
between pasting and the IME in the daemon. No X server is needed: the server and
the paster are fakes, and time is a fake clock."""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.dont_write_bytecode = True

import paste_output  # noqa: E402
import xim_daemon  # noqa: E402
from typing_session import TypingSession  # noqa: E402

CONTROL, V, PLAYER_V, SPACE, WAKE = 37, 248, 55, 65, 93
GAP = 0.035


class Clock:
    """Timers that run when told to, in order of time."""

    def __init__(self):
        self.now = 0.0
        self.timers = []

    def call_later(self, seconds, function):
        timer = type("Timer", (), {})()
        timer.function, timer.when = function, self.now + seconds
        timer.cancel = lambda: setattr(timer, "function", None)
        self.timers.append(timer)
        return timer

    def run(self):
        while True:
            live = [t for t in self.timers if t.function]
            if not live:
                return
            timer = min(live, key=lambda t: t.when)
            self.timers.remove(timer)
            self.now = timer.when
            function, timer.function = timer.function, None
            function()


class FakePaster:
    control, v, wake, real_v = CONTROL, V, WAKE, PLAYER_V

    def __init__(self, clock, log, game=None):
        self.clock, self.log, self.game = clock, log, game

    ready = True

    def keys_ready(self):
        return self.ready

    real_v_up = True

    def real_v_ready(self, must_be_up):
        return self.ready and (self.real_v_up or not must_be_up)

    def wake_ready(self):
        return self.ready

    def take_clipboard(self, text):
        self.log.append((round(self.clock.now, 3), "clipboard", text))

    def key(self, keycode, pressed):
        self.log.append((round(self.clock.now, 3), "press" if pressed else "release", keycode))
        if keycode == self.real_v:
            self.real_v_up = not pressed
        if self.game:
            self.game(keycode, pressed)


class FakeServer:
    def __init__(self, log):
        self.log = log

    def send_key_back(self, context, serial, event):
        self.log.append(("back", event))


def kinds(log):
    return [entry[1] if entry[0] != "back" else ("back", entry[1]) for entry in log]


class PasteOutputTest(unittest.TestCase):
    def make(self, forwarding=True):
        self.clock, self.log = Clock(), []
        self.paster = FakePaster(self.clock, self.log)
        self.output = paste_output.PasteOutput(FakeServer(self.log), self.clock, self.paster, lambda: forwarding)
        self.output.gap = GAP
        if forwarding:
            # The game hands each V back through XIM as soon as it is pressed.
            self.paster.game = lambda keycode, pressed: self.output.take_own_key(keycode, pressed)

    def test_each_step_lasts_a_gap(self):
        self.make()
        self.output.commit("가")
        self.clock.run()
        steps = [entry for entry in self.log if entry[1] != "back"]
        self.assertEqual(steps, [
            (0.0, "clipboard", "가"),
            (0.035, "press", CONTROL),
            (0.07, "press", V),
            (0.105, "release", V),
            (0.14, "release", CONTROL),
        ])
        # Nothing may follow until a gap after Ctrl is up.
        self.output.send_key(None, 0, "next")
        self.assertEqual(self.log[-1], ("back", "next"))
        self.assertEqual(round(self.clock.now, 3), 0.175)

    def presses(self):
        return [entry[2] for entry in self.log if entry[1] == "press"]

    def test_held_text_is_pasted_with_the_real_v(self):
        self.make()
        self.output.commit("가나", real_v=True)
        self.clock.run()
        self.assertEqual(self.presses(), [CONTROL, PLAYER_V])

    def test_the_paste_does_not_wait_for_its_own_v(self):
        self.make()
        self.output.commit("가", real_v=True)
        self.clock.run()
        self.assertEqual([entry for entry in self.log if entry[1] != "back"], [
            (0.0, "clipboard", "가"),
            (0.035, "press", CONTROL),
            (0.07, "press", PLAYER_V),
            (0.105, "release", PLAYER_V),
            (0.14, "release", CONTROL),
        ])

    def test_keys_behind_a_paste_are_a_gap_apart(self):
        self.make()
        self.output.commit("가")
        self.output.send_key(None, 0, "press")
        self.output.send_key(None, 0, "release")
        self.clock.run()
        back = [entry[1] for entry in self.log if entry[0] == "back"]
        self.assertEqual(back, ["press", "release"])
        # Press at 0.175, release a gap later, and a gap after each before the next.
        self.assertEqual(round(self.clock.now, 3), round(0.175 + 2 * GAP, 3))

    def test_a_release_right_after_the_paste_still_waits_a_gap(self):
        self.make()
        self.output.commit("가")
        self.output.send_key(None, 0, "press")
        for _ in range(100):
            if ("back", "press") in self.log:
                break
            self.step()
        self.output.send_key(None, 0, "release")
        self.assertNotIn(("back", "release"), self.log)
        self.clock.run()
        self.assertIn(("back", "release"), self.log)

    def test_keys_with_no_paste_go_at_once(self):
        self.make()
        self.output.send_key(None, 0, "press")
        self.output.send_key(None, 0, "release")
        self.assertEqual(self.log, [("back", "press"), ("back", "release")])

    def test_text_a_killed_server_left_behind_is_removed(self):
        path = os.path.join(tempfile.mkdtemp(), "batch-text")
        with open(path, "w", encoding="utf-8") as held:
            held.write("left behind")
        xim_daemon.BatchDisplay(path, lambda: False, None)
        self.assertFalse(os.path.exists(path))

    def test_the_spare_key_stays_for_text_as_typed(self):
        self.make()
        self.output.commit("가", real_v=True)
        self.clock.run()
        self.output.commit("나")
        self.clock.run()
        self.assertEqual(self.presses(), [CONTROL, PLAYER_V, CONTROL, V])

    def test_the_real_v_waits_while_the_player_holds_it(self):
        self.make()
        self.paster.real_v_up = False
        self.output.commit("가", real_v=True)
        for _ in range(4):
            self.step()
        self.assertEqual(self.presses(), [])
        self.paster.real_v_up = True
        self.clock.run()
        self.assertEqual(self.presses(), [CONTROL, PLAYER_V])

    def test_text_for_each_key_is_not_merged(self):
        self.make()
        self.output.busy = True
        self.output.commit("가")
        self.output.commit("나", real_v=True)
        self.assertEqual(len(self.output.queue), 2)

    def test_key_after_text_waits_for_the_paste(self):
        self.make()
        self.output.commit("안녕")
        self.output.send_key(None, 0, "space")
        self.clock.run()
        self.assertEqual(kinds(self.log), [
            "clipboard", "press", "press", "release", "release", ("back", "space"),
        ])

    def test_text_finished_meanwhile_goes_in_one_paste(self):
        self.make()
        self.output.commit("가")
        self.output.commit("나")
        self.output.commit("다")
        self.clock.run()
        clipboards = [entry[2] for entry in self.log if entry[1] == "clipboard"]
        self.assertEqual(clipboards, ["가", "나다"])

    def test_a_v_that_never_comes_back_does_not_stall(self):
        self.make()
        self.paster.game = None
        self.output.commit("가")
        self.output.send_key(None, 0, "after")
        self.clock.run()
        self.assertEqual(self.log[-1], ("back", "after"))

    def test_without_forwarding_no_wait(self):
        self.make(forwarding=False)
        self.output.commit("가")
        self.output.send_key(None, 0, "after")
        self.clock.run()
        self.assertEqual(self.log[-1], ("back", "after"))
        self.assertEqual(round(self.clock.now, 3), round(0.175 + GAP, 3))  # a gap after the key

    def test_waits_while_the_key_map_is_not_ready(self):
        self.make()
        self.paster.ready = False
        self.clock.call_later(0.1, lambda: setattr(self.paster, "ready", True))
        self.output.commit("가")
        self.clock.run()
        presses = [entry for entry in self.log if entry[1] == "press"]
        self.assertEqual(presses[0], (0.105, "press", CONTROL))
        self.assertEqual(presses[1], (0.14, "press", V))

    def test_ctrl_pressed_again_after_the_key_map_changed_under_it(self):
        self.make()
        self.output.commit("가")
        self.step()  # Ctrl down
        self.paster.ready = False
        self.clock.call_later(0.08, lambda: setattr(self.paster, "ready", True))
        self.clock.run()
        presses = [entry[1:] for entry in self.log if entry[1] in ("press", "release")]
        self.assertEqual(presses, [
            ("press", CONTROL), ("release", CONTROL),
            ("press", CONTROL), ("press", V), ("release", V), ("release", CONTROL),
        ])

    def test_own_keys_are_counted(self):
        self.make()
        self.paster.game = None  # nothing comes back on its own here
        self.assertFalse(self.output.take_own_key(V, True))
        self.output.commit("가")
        self.step()  # clipboard taken, wait for Ctrl
        self.assertTrue(self.output.ctrl_held)
        self.assertTrue(self.output.take_own_key(CONTROL, True))
        self.assertFalse(self.output.take_own_key(CONTROL, True))  # only once
        self.assertFalse(self.output.take_own_key(V, True))  # V not pressed yet
        self.assertFalse(self.output.take_own_key(PLAYER_V, True))  # the player's V is never the paste's
        self.step()  # V down
        self.assertTrue(self.output.take_own_key(V, True))
        self.assertFalse(self.output.take_own_key(V, True))
        self.clock.run()
        self.assertFalse(self.output.ctrl_held)
        self.assertFalse(self.output.take_own_key(V, False))  # the paste is over

    def step(self):
        timer = min((t for t in self.clock.timers if t.function), key=lambda t: t.when)
        self.clock.timers.remove(timer)
        self.clock.now = timer.when
        function, timer.function = timer.function, None
        function()


class DaemonPasteTest(unittest.TestCase):
    """The daemon with a fake XIM server: switching to pasting and back, and the
    player's V (ㅍ) while a paste is on its way."""

    def setUp(self):
        self.clock, self.log = Clock(), []
        log, clock = self.log, self.clock

        class Server:
            def __init__(self, *_args):
                self.on_reset = self.on_focus = self.on_key = self.on_toggle = None

            def send_key_back(self, context, serial, event):
                log.append(("back", event[1]))

            focused = True

            def commit(self, text, backspaces=0):
                if not self.focused:
                    return False
                log.append(("ime commit", text))
                return True

            def preedit(self, text):
                if not self.focused:
                    return False
                log.append(("ime preedit", text))
                return True

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        test = self

        class Paster(FakePaster):
            def __init__(self, _display):
                super().__init__(clock, log, lambda keycode, pressed: test.game_hands_back(keycode, pressed))

            def fileno(self):
                return -1

        def no_keymap(*_args):
            raise OSError("not in this test")

        self.originals = (xim_daemon.XimServer, xim_daemon.Paster, xim_daemon.STEAM_DISPLAY, xim_daemon.KeymapWatch)
        xim_daemon.XimServer, xim_daemon.Paster, xim_daemon.STEAM_DISPLAY = Server, Paster, ""
        xim_daemon.KeymapWatch = no_keymap
        self.daemon = xim_daemon.Daemon(":1")
        self.daemon.timers = clock
        for part in (self.daemon.session, self.daemon.steam, self.daemon.paste_output, self.daemon.game_output):
            part.timers = clock
        self.daemon.forwarding = True
        self.daemon.hangul_mode = True
        self.daemon.ime_wake = True

    def tearDown(self):
        (xim_daemon.XimServer, xim_daemon.Paster, xim_daemon.STEAM_DISPLAY,
         xim_daemon.KeymapWatch) = self.originals

    def game_hands_back(self, keycode, pressed):
        """Wine forwards the keys the paste pressed, as it does any key."""
        self.press(keycode, pressed)

    def press(self, code_or_keycode, pressed=True, evdev=False, state=0):
        keycode = code_or_keycode + xim_daemon.EVDEV_TO_X11_KEYCODE if evdev else code_or_keycode
        event = bytearray(32)
        event[28:30] = state.to_bytes(2, "little")
        event[0] = xim_daemon.KEY_PRESS if pressed else xim_daemon.KEY_PRESS + 1
        event[1] = keycode
        self.daemon._on_key(("context",), 0, bytes(event))

    def type_jamo(self, codes, state=0):
        for code in codes:
            self.press(code, True, evdev=True, state=state)
            self.press(code, False, evdev=True, state=state)

    def start_batching(self):
        shown = self.shown = []
        overlay = type("Overlay", (), {"show": lambda _self, text: shown.append(text)})()
        self.daemon.overlay = self.daemon.batch_display.overlay = overlay
        self.row_file = os.path.join(tempfile.mkdtemp(), "batch-text")
        self.daemon.batch_display.path = self.row_file
        self.daemon.paste_batch = True
        self.daemon._set_paste(True)
        self.daemon._update_batch()
        self.daemon.server.lookup_keysym = lambda _keycode, _state: self.keysym

    keysym = 0

    def pastes(self):
        return [entry[2] for entry in self.log if entry[1] == "clipboard"]

    def test_batch_holds_the_text_until_enter(self):
        self.start_batching()
        self.type_jamo([30, 37, 44, 18])  # ㅁㅏ, then ㅋㄷ: not one syllable
        self.clock.run()
        self.assertEqual(self.pastes(), [])
        self.assertTrue(self.shown[-1])
        held = self.daemon.batch.text + self.daemon.batch.composing
        self.press(28, True, evdev=True)
        self.press(28, False, evdev=True)
        self.clock.run()
        self.assertEqual(self.pastes(), [held])
        self.assertIn(PLAYER_V, [e[2] for e in self.log if e[1] == "press"])
        self.assertNotIn(V, [e[2] for e in self.log if e[1] == "press"])
        self.assertEqual(self.shown[-1], "")
        self.assertEqual([e[1] for e in self.log if e[0] == "back" and e[1] == 28 + xim_daemon.EVDEV_TO_X11_KEYCODE],
                         [28 + xim_daemon.EVDEV_TO_X11_KEYCODE] * 2)

    def test_before_the_first_jamo_keys_go_to_the_game(self):
        self.start_batching()
        self.keysym = 0x20
        self.press(57, True, evdev=True)
        self.clock.run()
        self.assertIn(("back", 57 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)
        self.assertEqual(self.daemon.batch.text, "")

    def test_batch_keeps_spaces_and_erases_held_text(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.keysym = 0x20
        self.press(57, True, evdev=True)
        self.assertEqual(self.daemon.batch.text, "마 ")
        self.press(14, True, evdev=True)
        self.assertEqual(self.daemon.batch.text, "마")
        self.press(14, True, evdev=True)
        self.assertEqual(self.shown[-1], "")
        self.clock.run()
        self.assertEqual(self.pastes(), [])
        self.assertNotIn(("back", 14 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)

    def test_escape_drops_the_held_text(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.press(1, True, evdev=True)
        self.press(1, False, evdev=True)
        self.clock.run()
        self.assertEqual(self.pastes(), [])
        self.assertEqual(self.shown[-1], "")
        self.assertNotIn(("back", 1 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)

    def test_batch_is_sent_when_leaving_hangul_mode(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon._set_hangul_mode(False)
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])

    def test_toggle_key_sends_the_batch_only_once_it_is_up(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon._toggle_mode()
        self.assertFalse(self.daemon.hangul_mode)
        self.assertEqual(self.shown[-1], "")
        self.assertEqual(self.pastes(), [])
        self.clock.now += 0.5  # the key is still held
        self.assertEqual(self.pastes(), [])
        self.daemon._toggle_released()
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])

    def test_a_toggle_key_never_seen_up_does_not_keep_the_text(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon._toggle_mode()
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])

    def test_toggling_without_text_holds_nothing_back(self):
        self.start_batching()
        self.daemon._toggle_mode()
        self.assertFalse(self.daemon.paste_output.paused)

    def test_the_key_after_the_toggle_follows_the_paste(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon._toggle_mode()
        self.press(57, True, evdev=True)
        self.assertNotIn(("back", 57 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)
        self.daemon._toggle_released()
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])
        self.assertIn(("back", 57 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)

    def grabbed(self, code, pressed=True, state=0):
        self.daemon._grabbed_key(code + xim_daemon.EVDEV_TO_X11_KEYCODE, state, pressed)

    def test_jamo_typed_while_the_toggle_key_is_down_are_kept(self):
        self.start_batching()
        self.press(57, True, evdev=True)  # any key, which gives the daemon a window
        self.press(57, False, evdev=True)
        self.daemon.hangul_mode = False
        self.daemon._toggle_mode()
        self.assertTrue(self.daemon.hangul_mode)
        self.grabbed(30)
        self.grabbed(30, False)
        self.grabbed(37)
        self.assertEqual(self.shown[-1], "마")
        self.assertNotIn(("back", 30 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)

    def test_alt_of_the_toggle_key_does_not_end_a_jamo(self):
        self.start_batching()
        self.grabbed(30, state=xim_daemon.MOD1_MASK)
        self.grabbed(37, state=xim_daemon.MOD1_MASK)
        self.assertEqual(self.shown[-1], "마")

    def test_keys_stay_forwarded_until_the_paste_is_over(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon._toggle_mode()
        self.assertTrue(self.daemon.forwarding)
        self.daemon._toggle_released()
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])
        self.assertFalse(self.daemon.forwarding)

    def test_english_typed_right_after_the_toggle_follows_the_paste(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon._toggle_mode()
        self.grabbed(30)  # still down: X sends it here
        self.grabbed(30, False)
        self.press(31, True, evdev=True)  # let go by now: Wine forwards it
        self.press(31, False, evdev=True)
        self.daemon._toggle_released()
        self.clock.run()
        clipboard = next(i for i, e in enumerate(self.log) if e[1] == "clipboard")
        keycodes = [e[1] for e in self.log[clipboard:] if e[0] == "back"]
        for code in (30, 31):
            self.assertIn(code + xim_daemon.EVDEV_TO_X11_KEYCODE, keycodes)

    def test_other_keys_send_the_batch_first(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.press(103, True, evdev=True)  # an arrow key, with no text of its own
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])
        self.assertIn(("back", 103 + xim_daemon.EVDEV_TO_X11_KEYCODE), self.log)

    def row_text(self):
        try:
            with open(self.row_file, encoding="utf-8") as held:
                return held.read()
        except OSError:
            return ""

    def open_steam_keyboard(self):
        self.daemon.virtual_active = True
        self.daemon._steam_typing_changed()

    def test_the_steam_keyboard_shows_held_text_in_its_row(self):
        self.start_batching()
        self.open_steam_keyboard()
        self.type_jamo([30, 37])
        self.clock.run()
        self.assertEqual(self.pastes(), [])
        self.assertEqual(self.row_text(), "마")
        self.assertEqual(oct(os.stat(self.row_file).st_mode & 0o777), "0o600")
        self.assertEqual(self.shown[-1], "")  # not on the overlay as well

    def test_closing_the_steam_keyboard_sends_what_is_held(self):
        self.start_batching()
        self.open_steam_keyboard()
        self.type_jamo([30, 37])
        self.daemon.virtual_active = False
        self.daemon._steam_typing_changed()
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])
        self.assertEqual(self.row_text(), "")

    def test_opening_the_steam_keyboard_moves_what_is_held(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.assertEqual(self.shown[-1], "마")
        self.open_steam_keyboard()
        self.clock.run()
        self.assertEqual(self.pastes(), [])
        self.assertEqual(self.row_text(), "마")
        self.assertEqual(self.shown[-1], "")

    def test_enter_on_the_steam_keyboard_sends_and_clears_the_row(self):
        self.start_batching()
        self.open_steam_keyboard()
        self.type_jamo([30, 37])
        self.press(28, True, evdev=True)
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])
        self.assertEqual(self.row_text(), "")

    def test_without_the_input_row_the_steam_keyboard_pastes_at_once(self):
        self.start_batching()
        self.daemon.batch_row_missing = True
        self.open_steam_keyboard()
        self.assertFalse(self.daemon.batching)
        self.type_jamo([30, 37, 44, 37])  # the last vowel ends 마
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])

    def test_turning_batching_off_sends_what_is_held(self):
        self.start_batching()
        self.type_jamo([30, 37])
        self.daemon.paste_batch = False
        self.daemon._update_batch()
        self.clock.run()
        self.assertEqual(self.pastes(), ["마"])
        self.assertFalse(self.daemon.batching)

    def test_switching_to_paste_and_back(self):
        daemon = self.daemon
        daemon._set_paste(True)
        self.assertIs(daemon.output, daemon.paste_output)
        self.assertIs(daemon.session.output, daemon.paste_output)
        self.assertTrue(daemon.session.direct)
        daemon._set_paste(False)
        self.assertIs(daemon.output, daemon.game_output)
        self.assertFalse(daemon.session.direct)

    def test_direct_setting_kept_while_pasting(self):
        daemon = self.daemon
        daemon._set_paste(True)
        daemon._set_direct(True)
        self.assertIs(daemon.output, daemon.paste_output)
        daemon._set_paste(False)
        self.assertTrue(daemon.session.direct)
        self.assertTrue(daemon.game_output.direct)

    def test_syllables_are_pasted_and_space_follows(self):
        self.daemon._set_paste(True)
        # ㄱ ㅏ ㄴ ㅏ, then Space: 가 is finished by ㄴ+ㅏ, 나 by Space.
        self.type_jamo([19, 37, 31, 37])
        self.press(57, True, evdev=True)
        self.clock.run()
        clipboards = [entry[2] for entry in self.log if entry[1] == "clipboard"]
        self.assertEqual("".join(clipboards), "가나")
        space = 57 + xim_daemon.EVDEV_TO_X11_KEYCODE
        self.assertEqual([e for e in self.log if e[0] == "back"][-1], ("back", space))
        self.assertFalse(any(e[0] == "ime commit" for e in self.log))

    def test_player_v_while_pasting_is_a_jamo(self):
        self.daemon._set_paste(True)
        # 메이플: ㅁ ㅔ ㅇ ㅣ ㅍ ㅡ ㄹ, with ㅍ (V) typed while 메이 is being pasted.
        self.type_jamo([30, 25, 32, 38])
        self.clock.timers.sort(key=lambda t: t.when)
        for _ in range(2):  # into the paste: clipboard taken, Ctrl pressed
            timer = min((t for t in self.clock.timers if t.function), key=lambda t: t.when)
            self.clock.timers.remove(timer)
            self.clock.now = timer.when
            timer.function()
        self.assertTrue(self.daemon.paste_output.ctrl_held)
        # The server reports the paste's Ctrl as held on the player's keys too.
        self.type_jamo([47, 50, 33], state=xim_daemon.CONTROL_MASK)
        self.press(57, True, evdev=True)
        self.clock.run()
        text = "".join(entry[2] for entry in self.log if entry[1] == "clipboard")
        self.assertEqual(text, "메이플")
        self.assertNotIn(("back", PLAYER_V), self.log)

    def wake_presses(self):
        return [e for e in self.log if e[1:] == ("press", WAKE)]

    def test_no_wake_unless_turned_on_for_the_game(self):
        self.daemon.ime_wake = False
        self.enter()
        self.type_jamo([19])
        self.clock.run()
        self.assertFalse(self.wake_presses())

    def enter(self, long_ago=True):
        self.press(28, True, evdev=True)
        self.press(28, False, evdev=True)
        self.clock.run()
        if long_ago:
            self.daemon.last_enter = -10  # no wait for the field to open

    def test_only_the_first_jamo_after_enter_wakes_the_game(self):
        daemon = self.daemon
        self.type_jamo([19, 37])  # 가, with no Enter before: no wake key
        self.clock.run()
        self.assertFalse(self.wake_presses())
        self.enter()
        held = []
        self.game_hands_back = lambda keycode, pressed: held.append((keycode, pressed))
        before = len(self.log)
        self.type_jamo([19])  # the first jamo after Enter
        self.assertEqual(len(self.wake_presses()), 1)
        # The wake key comes first; the jamo's composition waits for it.
        self.assertFalse([e for e in self.log[before:] if e[0] == "ime preedit"])
        self.clock.run()
        self.assertIn(("ime preedit", "ㄱ"), self.log[before:])
        self.type_jamo([37, 19, 37])  # typing on: no more
        self.clock.run()
        self.assertEqual(len(self.wake_presses()), 1)

    def test_enter_in_english_mode_does_not_count(self):
        daemon = self.daemon
        daemon.hangul_mode = False
        self.enter()
        daemon.hangul_mode = True
        self.type_jamo([19])
        self.clock.run()
        self.assertFalse(self.wake_presses())

    def test_the_wake_key_back_lets_the_text_go_after_a_settle(self):
        daemon = self.daemon
        self.enter()
        self.game_hands_back = lambda keycode, pressed: None  # handed back below
        self.type_jamo([19])
        self.assertTrue(daemon.game_output.holding)
        jamo = len(daemon.session.composer._keys)
        self.press(WAKE, True)
        self.press(WAKE, False)
        self.assertEqual(len(daemon.session.composer._keys), jamo)  # left alone
        self.assertTrue(daemon.game_output.holding)  # the game still needs a moment
        self.clock.run()
        self.assertFalse(daemon.game_output.holding)
        self.assertIn(("ime preedit", "ㄱ"), self.log)

    def test_the_wake_key_waits_after_a_quick_enter(self):
        daemon = self.daemon
        self.enter(long_ago=False)  # the jamo comes right after Enter
        start = self.clock.now
        self.type_jamo([19])
        self.assertFalse(self.wake_presses())  # not in the frame that opens the field
        self.clock.run()
        (at, _, _), = self.wake_presses()
        self.assertGreaterEqual(at - start, 0.4)
        self.assertIn(("ime preedit", "ㄱ"), self.log)  # held, then let go

    def test_direct_mode_wakes_after_enter_too(self):
        daemon = self.daemon
        daemon._set_direct(True)
        self.enter()
        self.type_jamo([19])
        self.assertEqual(len(self.wake_presses()), 1)

    def test_paste_mode_does_not_wake(self):
        daemon = self.daemon
        daemon._set_paste(True)
        self.enter()
        self.type_jamo([19, 37])
        self.press(57, True, evdev=True)
        self.clock.run()
        self.assertFalse(self.wake_presses())

    def held_output(self):
        output = self.daemon.game_output
        output.hold()
        output.commit("가")
        output.preedit("ㄴ")
        output.commit("나")
        output.preedit("ㄷ")
        return output

    def test_held_text_goes_as_one_then_the_composition(self):
        output = self.held_output()
        output.release()
        self.assertEqual([e for e in self.log if e[0].startswith("ime")], [("ime commit", "가나")])
        self.clock.run()
        self.assertEqual([e for e in self.log if e[0].startswith("ime")],
                         [("ime commit", "가나"), ("ime preedit", "ㄷ")])

    def test_typing_on_after_the_held_text_is_merged_with_the_composition(self):
        output = self.held_output()
        output.release()
        output.preedit("다")  # typed in the moment before the composition goes
        self.clock.run()
        self.assertEqual([e for e in self.log if e[0] == "ime preedit"], [("ime preedit", "다")])

    def test_direct_mode_sends_held_text_and_composition_together(self):
        self.daemon._set_direct(True)
        output = self.held_output()
        output.release()
        self.clock.run()
        self.assertEqual([e for e in self.log if e[0].startswith("ime")], [("ime commit", "가나ㄷ")])
        self.assertEqual(output.typed, "ㄷ")

    def test_a_held_key_stays_between_the_texts(self):
        output = self.daemon.game_output
        output.hold()
        output.commit("가")
        output.send_key(("context",), 0, bytes([2, 36]) + bytes(30))
        output.commit("나")
        output.release()
        self.assertEqual([e for e in self.log if e[0] != "press"],
                         [("ime commit", "가"), ("back", 36), ("ime commit", "나")])

    def test_erasing_while_waiting_takes_from_what_waits(self):
        daemon = self.daemon
        daemon._set_direct(True)
        daemon.server.focused = False
        self.type_jamo([19, 37])  # ㄱ, then ㅏ: 가 replaces ㄱ
        self.clock.run()
        self.assertEqual(daemon.game_output.waiting, "가")
        self.assertNotIn(("back", 14 + 8), self.log)  # no Backspace sent to erase the field

    def test_ime_used_when_not_pasting(self):
        self.type_jamo([19, 37])
        self.press(57, True, evdev=True)
        self.clock.run()
        self.assertIn(("ime commit", "가"), self.log)
        self.assertFalse(any(len(e) > 1 and e[1] == "clipboard" for e in self.log))


if __name__ == "__main__":
    unittest.main()
