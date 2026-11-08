"""Pasting: the order of text and keys, the timing of each step, and the switch
between pasting and the IME in the daemon. No X server is needed: the server and
the paster are fakes, and time is a fake clock."""

import os
import sys
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
    control, v, wake = CONTROL, V, WAKE

    def __init__(self, clock, log, game=None):
        self.clock, self.log, self.game = clock, log, game

    ready = True

    def keys_ready(self):
        return self.ready

    def wake_ready(self):
        return self.ready

    def take_clipboard(self, text):
        self.log.append((round(self.clock.now, 3), "clipboard", text))

    def key(self, keycode, pressed):
        self.log.append((round(self.clock.now, 3), "press" if pressed else "release", keycode))
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
        self.assertEqual(round(self.clock.now, 3), 0.175)

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
