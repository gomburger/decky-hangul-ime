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


ACCOUNT = "100"


def run(call):
    return asyncio.run(call)


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.runtime = os.path.join(self.base, "run")
        os.mkdir(self.runtime)
        self.backend = load_backend(os.path.join(self.base, "settings"))
        self.backend.runtime_dir = lambda: self.runtime
        self.backend.list_game_processes = lambda: []
        self.backend.bridge.use_account(ACCOUNT)

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
        bridge.set_input_state(":1", self.backend.BATCH_ROW_MISSING, False)
        bridge.set_input_state(":1", self.backend.TOGGLE_KEYS, "hangul")
        bridge.set_input_state(":1", self.backend.WAKE_WAIT_MS, 550)
        written = {os.path.join(self.runtime, name) for name in os.listdir(self.runtime)}
        read = {
            daemon.physical_file, daemon.virtual_file, daemon.direct_file, daemon.erase_text_file,
            daemon.backspace_hold_file, daemon.paste_file, daemon.paste_gap_file, daemon.toggle_keys_file,
            daemon.wake_wait_file, daemon.ime_wake_file, daemon.paste_batch_file, daemon.batch_row_missing_file,
            daemon.enter_commit_file,
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
                         {"active": True, "hangul": False, "english_on_close": True, "batch": False})
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
        fresh.use_account(ACCOUNT)
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
        fresh.use_account(ACCOUNT)
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
        fresh.use_account(ACCOUNT)
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
        fresh.use_account(ACCOUNT)
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

    def test_batching_is_on_by_default_and_stays_as_set(self):
        plugin, run = self.backend.Plugin, asyncio.run
        self.assertTrue(run(plugin.set_game_enabled(plugin, "3", True))["games"]["3"]["paste_batch"])
        run(plugin.set_game_input(plugin, "3", "paste_batch", False))
        fresh = self.backend.Bridge()
        fresh.load()
        fresh.use_account(ACCOUNT)
        self.assertFalse(fresh.games["3"]["paste_batch"])
        self.assertTrue(self.backend.clean_game({"virtual": True})["paste_batch"])

    def test_a_game_running_without_the_option_is_left_alone(self):
        plugin, bridge, run = self.backend.Plugin, self.backend.bridge, asyncio.run
        run(plugin.set_game_enabled(plugin, "3", True))
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": False}]
        self.backend.list_game_processes = lambda: programs
        bridge.in_game, bridge.active_game = True, "3"
        bridge.unlinked = bridge.game_unlinked("3")
        self.assertTrue(bridge.unlinked)
        self.assertFalse(run(plugin.get_keyboard_mode(plugin))["active"])
        state = run(plugin.get_state(plugin))
        self.assertEqual(state["unlinked_game"], "3")
        self.assertFalse(state["active"])

    def test_a_game_started_before_the_option_was_added_needs_a_restart(self):
        plugin, backend, run = self.backend.Plugin, self.backend, asyncio.run
        backend.seconds_since_boot = lambda: 100.0
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": False, "started": 50.0}]
        backend.list_game_processes = lambda: programs
        state = run(plugin.set_game_enabled(plugin, "3", True))
        self.assertEqual(state["restart_games"], {"3": "added"})
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {"3": "added"})  # still, on reopening
        # Started again after it, still without the option: the option was lost.
        programs[0]["started"] = 150.0
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})
        # Started again with it.
        programs[0].update(started=50.0, has_xim_option=True)
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})

    def test_restoring_the_option_asks_for_a_restart_too(self):
        plugin, backend, run = self.backend.Plugin, self.backend, asyncio.run
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": False, "started": 50.0}]
        backend.list_game_processes = lambda: programs
        backend.seconds_since_boot = lambda: 10.0  # registered before the game started
        self.assertEqual(run(plugin.set_game_enabled(plugin, "3", True))["restart_games"], {})
        backend.seconds_since_boot = lambda: 100.0
        self.assertEqual(run(plugin.note_launch_option_added(plugin, 3))["restart_games"], {"3": "added"})

    def test_a_game_running_with_the_option_needs_a_restart_once_it_is_off(self):
        plugin, backend, run = self.backend.Plugin, self.backend, asyncio.run
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": True, "started": 50.0}]
        backend.list_game_processes = lambda: programs
        backend.seconds_since_boot = lambda: 10.0
        run(plugin.set_game_enabled(plugin, "3", True))
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})
        backend.seconds_since_boot = lambda: 100.0
        self.assertEqual(run(plugin.set_game_enabled(plugin, "3", False))["restart_games"], {"3": "removed"})
        programs[0].update(started=150.0, has_xim_option=False)  # started again without it
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})

    def test_a_game_older_than_the_input_server_needs_a_restart(self):
        plugin, backend, bridge, run = self.backend.Plugin, self.backend, self.backend.bridge, asyncio.run
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": True, "started": 50.0}]
        backend.list_game_processes = lambda: programs
        backend.seconds_since_boot = lambda: 10.0
        run(plugin.set_game_enabled(plugin, "3", True))
        bridge.xim_servers, bridge.server_started_at = {":1": None}, {":1": 20.0}
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})
        bridge.server_started_at[":1"] = 100.0  # started again after a pause
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {"3": "server"})
        # Left alone until then: no Hangul from the Steam keyboard's letters.
        bridge.in_game, bridge.active_game, bridge.link_checked = True, "3", (None, 0.0, False)
        bridge.unlinked = bridge.game_unlinked("3")
        self.assertTrue(bridge.unlinked)
        self.assertFalse(run(plugin.get_keyboard_mode(plugin))["active"])
        bridge.games["3"]["paste"] = True  # pasting still works
        bridge.link_checked = (None, 0.0, False)
        self.assertFalse(bridge.game_unlinked("3"))
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})
        bridge.games["3"]["paste"] = False
        bridge.xim_servers = {}  # paused: nothing to say yet
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})
        bridge.xim_servers = {":1": None}
        programs[0]["started"] = 150.0  # the game started again
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {})

    def test_no_pausing_while_a_connected_game_runs(self):
        plugin, backend, bridge, run = self.backend.Plugin, self.backend, self.backend.bridge, asyncio.run
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": True, "started": 50.0}]
        backend.list_game_processes = lambda: programs
        with self.assertRaises(ValueError):
            run(plugin.set_paused(plugin, True))
        self.assertFalse(bridge.paused)
        programs[0]["has_xim_option"] = False  # a game the input server never reached
        self.assertTrue(run(plugin.set_paused(plugin, True))["paused"])
        programs[0]["has_xim_option"] = True  # running through the pause
        backend.seconds_since_boot = lambda: 100.0
        run(plugin.set_game_enabled(plugin, "3", True))
        bridge.option_changed_at["3"] = 10.0
        self.assertFalse(run(plugin.set_paused(plugin, False))["paused"])
        bridge.xim_servers, bridge.server_started_at = {":1": None}, {":1": 101.0}
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {"3": "resumed"})
        bridge.link_checked = (None, 0.0, False)
        self.assertTrue(bridge.game_unlinked("3"))  # Steam keyboard left alone till then
        bridge.server_started_at[":1"] = 200.0  # started again much later, not for the pause
        self.assertEqual(run(plugin.get_state(plugin))["restart_games"], {"3": "server"})

    def test_remote_play_uses_the_host_ime_whatever_the_game(self):
        plugin, backend, bridge, run = self.backend.Plugin, self.backend, self.backend.bridge, asyncio.run
        backend.remote_play = lambda: ("1260320", ":0")
        bridge.start_key_watch = lambda display: None
        bridge.follow_remote_play()
        mode = run(plugin.get_keyboard_mode(plugin))
        self.assertEqual((mode["active"], mode["hangul"], mode["remote"], mode["english_on_close"]), (True, False, True, False))
        # The globe key sent Right Alt: noted, and its echo is not a second switch.
        run(plugin.note_host_toggle(plugin))
        self.assertTrue(run(plugin.set_keyboard_hangul(plugin, True))["hangul"])
        bridge.host_key_pressed(108)
        self.assertTrue(bridge.remote_hangul)
        # Caps Lock sends Right Alt with no change here: noted first, then ignored.
        bridge.own_toggle_at = -100.0
        run(plugin.note_host_toggle(plugin))
        bridge.host_key_pressed(108)
        self.assertTrue(bridge.remote_hangul)
        # A physical keyboard's Hangul key switches the host: the layout follows.
        bridge.own_toggle_at = -100.0
        bridge.host_key_pressed(130)
        self.assertFalse(bridge.remote_hangul)
        bridge.host_key_pressed(30)  # any other key
        self.assertFalse(bridge.remote_hangul)
        self.assertEqual(run(plugin.get_state(plugin))["remote_play"], "1260320")
        # The stream ends: back to the game's own settings.
        backend.remote_play = lambda: None
        bridge.follow_remote_play()
        self.assertNotIn("remote", run(plugin.get_keyboard_mode(plugin)))

    def test_no_focus_watch_while_paused_or_with_no_game(self):
        bridge = self.backend.bridge
        stopped = []
        bridge.follow_remote_play = lambda found=None: None
        self.backend.scan_steam = lambda: (None, None)
        bridge.focus_watch = (":0", None, type("Stop", (), {"set": lambda _self: stopped.append(True)})())
        bridge.games = {}
        bridge.sync()
        self.assertEqual((stopped, bridge.focus_watch), ([True], None))
        self.assertIsNone(bridge.next_sync_delay(0.0))  # no stream: Steam tells of one
        bridge.remote_app = "1260320"
        self.assertEqual(bridge.next_sync_delay(0.0), self.backend.IDLE_POLL_SECONDS)
        bridge.remote_app = None
        # Steam told of a stream starting: its client may come a moment later.
        asyncio.run(self.backend.Plugin.notify_change(self.backend.Plugin))
        self.assertEqual(bridge.next_sync_delay(self.backend.time.monotonic()), self.backend.POLL_SECONDS)
        self.assertIsNone(bridge.next_sync_delay(self.backend.time.monotonic() + 20))
        # Paused: nothing looked at, and no look until a wake.
        self.backend.scan_steam = lambda: self.fail("looked at /proc while paused")
        bridge.paused, bridge.games = True, {"3": {}}
        bridge.sync()
        self.assertIsNone(bridge.next_sync_delay(0.0))

    def test_idle_looks_stop_a_while_after_the_last_game(self):
        backend, bridge = self.backend, self.backend.bridge
        bridge.games = {"3": {}}
        bridge.focus_watch = (":0", type("Thread", (), {"is_alive": lambda _self: True})(), None)
        backend.display_lost = lambda thread: False
        bridge.in_game, bridge.busy_at = False, 100.0
        self.assertEqual(bridge.next_sync_delay(130.0), backend.IDLE_POLL_SECONDS)
        self.assertIsNone(bridge.next_sync_delay(170.0))  # a minute with no game
        bridge.remote_app = "1260320"  # a stream still looks now and then
        self.assertEqual(bridge.next_sync_delay(170.0), backend.IDLE_POLL_SECONDS)
        bridge.remote_app = None
        bridge.in_game = True
        self.assertEqual(bridge.next_sync_delay(170.0), backend.POLL_SECONDS)
        bridge.focus_watch = None

    def test_link_is_looked_at_again_after_a_while(self):
        bridge = self.backend.bridge
        programs = [{"pid": 1, "name": "game.exe", "app_id": "3", "has_xim_option": False}]
        self.backend.list_game_processes = lambda: programs
        self.assertTrue(bridge.game_unlinked("3"))
        programs[0]["has_xim_option"] = True  # restarted with the option
        self.assertTrue(bridge.game_unlinked("3"))  # not looked at again yet
        bridge.link_checked = ("3", -100.0, True)
        self.assertFalse(bridge.game_unlinked("3"))

    def test_a_game_with_no_program_found_counts_as_linked(self):
        self.backend.list_game_processes = lambda: []
        self.assertFalse(self.backend.bridge.game_unlinked("3"))

    def test_an_input_server_that_keeps_stopping_is_started_less_often(self):
        bridge = self.backend.bridge

        class Stopped:
            def poll(self):
                return 1

        bridge.daemon_started[":1"] = 100.0
        bridge.daemon_stopped(":1", Stopped(), 101.0)
        self.assertEqual(bridge.daemon_retry_at[":1"], 103.0)  # 2 s
        bridge.daemon_started[":1"] = 103.0
        bridge.daemon_stopped(":1", Stopped(), 104.0)
        self.assertEqual(bridge.daemon_retry_at[":1"], 108.0)  # 4 s
        for _ in range(10):
            bridge.daemon_started[":1"] = 200.0
            bridge.daemon_stopped(":1", Stopped(), 201.0)
        self.assertEqual(bridge.daemon_retry_at[":1"], 201.0 + self.backend.DAEMON_RETRY_MAX_SECONDS)
        bridge.daemon_started[":1"] = 300.0
        bridge.daemon_stopped(":1", Stopped(), 400.0)  # ran a long while first
        self.assertEqual(bridge.daemon_retry_at[":1"], 400.0)

    def test_only_the_error_name_is_kept(self):
        path = self.backend.daemon_error_path(":1")
        with open(path, "w", encoding="utf-8") as errors:
            errors.write("Traceback (most recent call last):\n  File x\nValueError: some typed text\n")
        self.assertEqual(self.backend.daemon_exit_reason(":1"), "ValueError")
        with open(path, "w", encoding="utf-8") as errors:
            errors.write("")
        self.assertEqual(self.backend.daemon_exit_reason(":1"), "no error output")

    def test_a_display_left_behind_is_not_served(self):
        import socket
        directory = tempfile.mkdtemp()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(os.path.join(directory, "X1"))
        stale.close()  # the file stays, with nothing listening
        self.assertFalse(self.backend.display_served(":1", directory))
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        live.bind(os.path.join(directory, "X2"))
        live.listen(1)
        try:
            self.assertTrue(self.backend.display_served(":2", directory))
        finally:
            live.close()
        self.assertFalse(self.backend.display_served(":3", directory))

    def test_opacity_off_on_desktop_is_kept(self):
        plugin, run = self.backend.Plugin, asyncio.run
        state = run(plugin.get_state(plugin))
        self.assertFalse(state["opacity_off_on_desktop"])
        self.assertIn("desktop_mode", state)
        self.assertTrue(run(plugin.set_opacity_off_on_desktop(plugin, True))["opacity_off_on_desktop"])
        fresh = self.backend.Bridge()
        fresh.load()
        self.assertTrue(fresh.opacity_off_on_desktop)

    def test_older_settings_load_without_paste(self):
        import json
        os.makedirs(os.path.dirname(self.backend.SETTINGS_FILE), exist_ok=True)
        with open(self.backend.SETTINGS_FILE, "w", encoding="utf-8") as settings:
            json.dump({"games": {"9": {"virtual": True, "physical": True, "direct": True, "erase_text": False}}}, settings)
        bridge = self.backend.Bridge()
        bridge.load()
        bridge.use_account(ACCOUNT)
        bridge.claim_games(["9"])
        self.assertEqual(bridge.games["9"]["paste"], False)
        self.assertEqual(bridge.paste_gap_ms, 35)

    def test_each_account_has_its_own_games(self):
        plugin, bridge = self.backend.Plugin, self.backend.bridge
        run(plugin.set_game_enabled(plugin, "3", True))
        run(plugin.set_game_input(plugin, "3", "paste", True))
        self.assertEqual(run(plugin.set_account(plugin, "200")), [])
        self.assertEqual(run(plugin.get_state(plugin))["games"], {})
        run(plugin.set_game_enabled(plugin, "4", True))
        fresh = self.backend.Bridge()
        fresh.load()
        self.assertEqual(fresh.games, {})  # no account yet, so no game is active
        fresh.use_account(ACCOUNT)
        self.assertEqual(list(fresh.games), ["3"])
        self.assertTrue(fresh.games["3"]["paste"])
        fresh.use_account("200")
        self.assertEqual(list(fresh.games), ["4"])
        self.assertEqual(bridge.account, "200")

    def test_switching_account_leaves_the_game(self):
        bridge = self.backend.bridge
        bridge.game_window, bridge.active_game, bridge.in_game = 5, "3", True
        bridge.use_account("200")
        self.assertEqual((bridge.game_window, bridge.active_game, bridge.in_game), (None, None, False))

    def test_older_games_go_only_to_the_account_that_claims_them(self):
        import json
        os.makedirs(os.path.dirname(self.backend.SETTINGS_FILE), exist_ok=True)
        with open(self.backend.SETTINGS_FILE, "w", encoding="utf-8") as settings:
            json.dump({"games": {"8": {"virtual": True, "direct": True}, "9": {"virtual": True}}}, settings)
        plugin, bridge = self.backend.Plugin, self.backend.bridge
        bridge.load()
        bridge.account = None
        self.assertEqual(run(plugin.set_account(plugin, "300")), ["8", "9"])
        run(plugin.claim_games(plugin, ["8", "unknown"]))
        self.assertEqual(list(bridge.games), ["8"])
        self.assertTrue(bridge.games["8"]["direct"])
        self.assertEqual(bridge.unclaimed_games(), ["9"])
        self.assertEqual(run(plugin.set_account(plugin, "400")), ["8", "9"])  # still there for others
        self.assertEqual(bridge.games, {})

    def test_changes_wait_for_an_account(self):
        plugin, bridge = self.backend.Plugin, self.backend.bridge
        bridge.account = None
        with self.assertRaises(ValueError):
            run(plugin.set_game_enabled(plugin, "3", True))
        with self.assertRaises(ValueError):
            run(plugin.set_account(plugin, "not a number"))


if __name__ == "__main__":
    unittest.main()
