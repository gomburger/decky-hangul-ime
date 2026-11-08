"""The backend and the daemon agree on every file, and the send methods exclude
each other. No X server is needed: the daemon's server is a fake."""

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.dont_write_bytecode = True


def load_backend(settings_dir):
    os.environ["DECKY_PLUGIN_SETTINGS_DIR"] = settings_dir
    os.environ["DECKY_PLUGIN_DIR"] = ROOT
    spec = importlib.util.spec_from_file_location("hangul_backend", os.path.join(ROOT, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.runtime = os.path.join(self.base, "run")
        os.mkdir(self.runtime)
        self.backend = load_backend(os.path.join(self.base, "settings"))
        self.backend.runtime_dir = lambda: self.runtime
        self.backend.list_game_processes = lambda: []

    def test_daemon_reads_every_file_the_backend_writes(self):
        os.environ["XDG_RUNTIME_DIR"] = self.runtime
        import xim_daemon

        class Server:
            def __init__(self, *_args):
                pass

            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        class NoPaster:
            def __init__(self, *_args):
                raise OSError("not in this test")

        saved = (xim_daemon.XimServer, xim_daemon.Paster, xim_daemon.RUNTIME_DIR, xim_daemon.STEAM_DISPLAY)
        saved_keymap = xim_daemon.KeymapWatch
        xim_daemon.XimServer, xim_daemon.Paster, xim_daemon.KeymapWatch = Server, NoPaster, NoPaster
        xim_daemon.RUNTIME_DIR, xim_daemon.STEAM_DISPLAY = self.runtime, ""
        try:
            daemon = xim_daemon.Daemon(":1")
        finally:
            xim_daemon.XimServer, xim_daemon.Paster, xim_daemon.RUNTIME_DIR, xim_daemon.STEAM_DISPLAY = saved
            xim_daemon.KeymapWatch = saved_keymap
        bridge = self.backend.Bridge()
        for option in self.backend.GAME_OPTIONS:
            bridge.set_input_state(":1", option, True)
        bridge.set_input_state(":1", self.backend.BACKSPACE_HOLD_MS, 65)
        bridge.set_input_state(":1", self.backend.PASTE_GAP_MS, 40)
        bridge.set_input_state(":1", self.backend.TOGGLE_KEYS, "hangul")
        bridge.set_input_state(":1", self.backend.WAKE_WAIT_MS, 550)
        written = {os.path.join(self.runtime, name) for name in os.listdir(self.runtime)}
        read = {
            daemon.physical_file, daemon.virtual_file, daemon.direct_file, daemon.erase_text_file,
            daemon.backspace_hold_file, daemon.paste_file, daemon.paste_gap_file, daemon.toggle_keys_file,
            daemon.wake_wait_file, daemon.ime_wake_file,
        }
        self.assertEqual(read, written)
        self.assertEqual(self.backend.input_state_path(":1", self.backend.MODE), daemon.mode_file)
        self.assertEqual(self.backend.input_state_path(":1", self.backend.MODE_REQUEST), daemon.mode_request_file)
        self.assertEqual(xim_daemon.read_milliseconds(daemon.paste_gap_file, 0), 0.04)

    def test_send_methods_exclude_each_other(self):
        plugin, bridge = self.backend.Plugin, self.backend.bridge
        run = asyncio.run
        run(plugin.set_game_input(plugin, "7", "physical", True))
        state = run(plugin.set_send_method(plugin, "7", "paste"))
        self.assertTrue(state["games"]["7"]["paste"])
        self.assertFalse(state["games"]["7"]["direct"])
        state = run(plugin.set_send_method(plugin, "7", "direct"))
        self.assertTrue(state["games"]["7"]["direct"])
        self.assertFalse(state["games"]["7"]["paste"])
        state = run(plugin.set_send_method(plugin, "7", "ime"))
        self.assertFalse(state["games"]["7"]["direct"] or state["games"]["7"]["paste"])
        with self.assertRaises(ValueError):
            run(plugin.set_send_method(plugin, "7", "other"))
        with self.assertRaises(ValueError):
            run(plugin.set_send_method(plugin, "8", "paste"))
        self.assertEqual(bridge.games["7"]["paste"], False)

    def test_keyboard_mode_follows_the_focused_game(self):
        plugin, bridge, run = self.backend.Plugin, self.backend.bridge, asyncio.run
        run(plugin.set_game_enabled(plugin, "7", True))
        bridge.in_game, bridge.active_game = True, "7"
        self.assertEqual(run(plugin.get_keyboard_mode(plugin)),
                         {"active": True, "hangul": False, "english_on_close": True})
        run(plugin.set_send_method(plugin, "7", "direct"))
        self.assertTrue(run(plugin.get_keyboard_mode(plugin))["active"])  # in every mode
        bridge.in_game = False
        self.assertFalse(run(plugin.get_keyboard_mode(plugin))["active"])
        bridge.in_game, bridge.paused = True, True
        self.assertFalse(run(plugin.get_keyboard_mode(plugin))["active"])

    def test_english_on_close_is_per_game(self):
        plugin, bridge, run = self.backend.Plugin, self.backend.bridge, asyncio.run
        state = run(plugin.set_game_enabled(plugin, "3", True))
        self.assertTrue(state["games"]["3"]["english_on_close"])  # on at first
        run(plugin.set_game_enabled(plugin, "4", True))
        state = run(plugin.set_game_setting(plugin, "3", "english_on_close", False))
        self.assertFalse(state["games"]["3"]["english_on_close"])
        self.assertTrue(state["games"]["4"]["english_on_close"])
        bridge.in_game, bridge.active_game = True, "3"
        self.assertFalse(run(plugin.get_keyboard_mode(plugin))["english_on_close"])
        fresh = self.backend.Bridge()
        fresh.load()
        self.assertFalse(fresh.games["3"]["english_on_close"])
        with self.assertRaises(ValueError):
            run(plugin.set_game_setting(plugin, "9", "english_on_close", True))
        with self.assertRaises(ValueError):
            run(plugin.set_game_setting(plugin, "3", "other", True))

    def test_keyboard_hangul_asks_for_the_mode_and_waits_for_it(self):
        plugin, bridge, run, backend = self.backend.Plugin, self.backend.bridge, asyncio.run, self.backend
        bridge.xim_servers = {":1": None}
        mode_file = backend.input_state_path(":1", backend.MODE)
        requests = []
        def fake_request(enabled):
            requests.append(enabled)
            with open(mode_file, "w", encoding="utf-8") as mode:
                mode.write("1" if enabled else "0")  # as the daemon would, a moment later
        backend.request_hangul_mode = fake_request
        self.assertTrue(run(plugin.set_keyboard_hangul(plugin, True))["hangul"])
        self.assertTrue(run(plugin.set_keyboard_hangul(plugin, True))["hangul"])  # asked twice: still on
        self.assertFalse(run(plugin.set_keyboard_hangul(plugin, False))["hangul"])
        self.assertEqual(requests, [True, True, False])

    def test_wake_is_per_game_and_off_at_first(self):
        plugin, run = self.backend.Plugin, asyncio.run
        state = run(plugin.set_game_enabled(plugin, "3", True))
        self.assertFalse(state["games"]["3"]["ime_wake"])
        self.assertEqual(state["games"]["3"]["wake_wait_ms"], 500)
        run(plugin.set_game_enabled(plugin, "4", True))
        run(plugin.set_game_input(plugin, "3", "ime_wake", True))
        state = run(plugin.set_wake_wait_ms(plugin, "3", 400))
        self.assertTrue(state["games"]["3"]["ime_wake"])
        self.assertEqual(state["games"]["3"]["wake_wait_ms"], 400)
        self.assertEqual(state["games"]["4"]["wake_wait_ms"], 500)
        self.assertEqual(run(plugin.set_wake_wait_ms(plugin, "3", -5))["games"]["3"]["wake_wait_ms"], 0)
        self.assertEqual(run(plugin.set_wake_wait_ms(plugin, "3", 5000))["games"]["3"]["wake_wait_ms"], 1000)
        run(plugin.set_wake_wait_ms(plugin, "3", 600))
        fresh = self.backend.Bridge()
        fresh.load()
        self.assertEqual(fresh.games["3"]["wake_wait_ms"], 600)
        self.assertTrue(fresh.games["3"]["ime_wake"])
        with self.assertRaises(ValueError):
            run(plugin.set_wake_wait_ms(plugin, "9", 100))

    def test_paste_gap_is_kept_in_range_and_saved(self):
        plugin, run = self.backend.Plugin, asyncio.run
        self.assertEqual(run(plugin.set_paste_gap_ms(plugin, 5))["paste_gap_ms"], 20)
        self.assertEqual(run(plugin.set_paste_gap_ms(plugin, 500))["paste_gap_ms"], 70)
        self.assertEqual(run(plugin.set_paste_gap_ms(plugin, 45))["paste_gap_ms"], 45)
        fresh = self.backend.Bridge()
        fresh.load()
        self.assertEqual(fresh.paste_gap_ms, 45)

    def test_one_toggle_uses_both_keyboards(self):
        plugin, bridge, run = self.backend.Plugin, self.backend.bridge, asyncio.run
        state = run(plugin.set_game_enabled(plugin, "5", True))
        self.assertTrue(state["games"]["5"]["virtual"] and state["games"]["5"]["physical"])
        run(plugin.set_send_method(plugin, "5", "paste"))
        state = run(plugin.set_game_enabled(plugin, "5", True))
        self.assertTrue(state["games"]["5"]["paste"])  # turning it on again keeps the options
        state = run(plugin.set_game_enabled(plugin, "5", False))
        self.assertNotIn("5", state["games"])

    def test_toggle_keys_are_chosen_saved_and_read(self):
        plugin, run = self.backend.Plugin, asyncio.run
        self.assertEqual(run(plugin.get_state(plugin))["toggle_keys"], ["hangul", "right_alt", "shift_space"])
        state = run(plugin.set_toggle_key(plugin, "right_alt", False))
        self.assertEqual(state["toggle_keys"], ["hangul", "shift_space"])
        state = run(plugin.set_toggle_key(plugin, "right_alt", True))
        self.assertEqual(state["toggle_keys"], ["hangul", "right_alt", "shift_space"])
        for name in ("hangul", "right_alt", "shift_space"):
            run(plugin.set_toggle_key(plugin, name, False))
        fresh = self.backend.Bridge()
        fresh.load()
        self.assertEqual(fresh.toggle_keys, [])
        with self.assertRaises(ValueError):
            run(plugin.set_toggle_key(plugin, "caps", True))
        import xim_daemon
        path = os.path.join(self.runtime, "keys")
        self.assertEqual(xim_daemon.read_toggle_keys(path), ("hangul", "right_alt", "shift_space"))
        bridge = self.backend.Bridge()
        bridge.toggle_keys = ["shift_space"]
        bridge.set_input_state(":1", self.backend.TOGGLE_KEYS, ",".join(bridge.toggle_keys))
        written = self.backend.input_state_path(":1", self.backend.TOGGLE_KEYS)
        self.assertEqual(xim_daemon.read_toggle_keys(written), ("shift_space",))
        bridge.set_input_state(":1", self.backend.TOGGLE_KEYS, "")
        self.assertEqual(xim_daemon.read_toggle_keys(written), ())

    def test_older_settings_load_without_paste(self):
        import json
        os.makedirs(os.path.dirname(self.backend.SETTINGS_FILE), exist_ok=True)
        with open(self.backend.SETTINGS_FILE, "w", encoding="utf-8") as settings:
            json.dump({"games": {"9": {"virtual": True, "physical": True, "direct": True, "erase_text": False}}}, settings)
        bridge = self.backend.Bridge()
        bridge.load()
        self.assertEqual(bridge.games["9"]["paste"], False)
        self.assertEqual(bridge.paste_gap_ms, 35)


if __name__ == "__main__":
    unittest.main()
