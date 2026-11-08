"""Decky backend: runs the input bridge, and tells it which game has focus.

xim_daemon.py gives each game display an XIM server, and does the Hangul typing. It
runs while any game is registered, because a game has to find it already there when
the game starts. This backend tells it, through small files, which keyboards the
focused game uses.
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys

PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR", os.path.dirname(os.path.abspath(__file__)))
# Decky puts py_modules on the path; this also covers loading main.py on its own.
if os.path.join(PLUGIN_DIR, "py_modules") not in sys.path:
    sys.path.insert(0, os.path.join(PLUGIN_DIR, "py_modules"))

from state_files import (  # noqa: E402
    BACKSPACE_HOLD_MS, DIRECT, GAME_OPTIONS, MODE, MODE_REQUEST, PASTE, PASTE_GAP_MS, PHYSICAL, TOGGLE_KEYS,
    WAKE_WAIT_MS,
    VIRTUAL, state_path,
)

SETTINGS_DIR = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR", "/tmp/hangul-ime-bridge")
LOG_DIR = os.environ.get("DECKY_PLUGIN_LOG_DIR", SETTINGS_DIR)
USER_HOME = os.environ.get("DECKY_USER_HOME", "/home/deck")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")
DAEMON_SCRIPT = os.path.join(PLUGIN_DIR, "py_modules", "xim_daemon.py")
POLL_SECONDS = 1.0
MODE_REQUEST_WAIT_SECONDS = 0.3  # the daemon checks for a request every 0.25 s
# Waiting for the daemon to take a mode asked for: how often to look, and how long.
MODE_POLL_SECONDS = 0.05
MODE_SETTLE_SECONDS = 0.8
XIM_MODIFIER = b"@im=hangulbridge"
# How long direct mode holds Backspace before typing what replaces the text.
DEFAULT_BACKSPACE_HOLD_MS = 50
MIN_BACKSPACE_HOLD_MS = 10
MAX_BACKSPACE_HOLD_MS = 100
# Each wait of a paste. A game reads keys once a frame, so each has to be longer than
# a frame: 35 ms holds down to 30 fps.
DEFAULT_PASTE_GAP_MS = 35
MIN_PASTE_GAP_MS = 20
MAX_PASTE_GAP_MS = 70
# Per game, for the plugin alone, with what a game starts with: whether closing the
# Steam keyboard after typing into the game switches to English, so its keys work
# as controls again.
ENGLISH_ON_CLOSE = "english_on_close"
GAME_SETTINGS = {ENGLISH_ON_CLOSE: True}
# After Enter, how long before the key that wakes a game's IME, per game.
DEFAULT_WAKE_WAIT_MS = 500
MIN_WAKE_WAIT_MS = 0
MAX_WAKE_WAIT_MS = 1000
# The keys that switch between Hangul and English, all on unless turned off.
TOGGLE_KEY_NAMES = ("hangul", "right_alt", "shift_space")
# How the composition reaches a game: through the IME, typed as text, or pasted.
SEND_METHODS = {"ime": (), "direct": (DIRECT,), "paste": (PASTE,)}
DEFAULT_KEYBOARD_OPACITY = 100
MIN_KEYBOARD_OPACITY = 30
MAX_KEYBOARD_OPACITY = 100
OWN_PROCESS = re.compile(r"^(\\\\\?\\)?(c:\\windows\\|.*\\compatibilitytools\.d\\)", re.IGNORECASE)
EXE_NAME = re.compile(r"([^\\/]+?\.exe)(?:\s|$)", re.IGNORECASE)

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "hangul-ime-bridge.log"),
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("hangul-ime-bridge")


def runtime_dir():
    return f"/run/user/{os.getuid()}"


def input_state_path(display, input_type):
    """The file telling a daemon whether the focused game uses one kind of input."""
    return state_path(runtime_dir(), display, input_type)


def clamp_backspace_hold(value):
    try:
        return max(MIN_BACKSPACE_HOLD_MS, min(MAX_BACKSPACE_HOLD_MS, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_BACKSPACE_HOLD_MS


def clamp_wake_wait(value):
    try:
        return max(MIN_WAKE_WAIT_MS, min(MAX_WAKE_WAIT_MS, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_WAKE_WAIT_MS


def new_game():
    return {key: False for key in GAME_OPTIONS} | GAME_SETTINGS | {WAKE_WAIT_MS: DEFAULT_WAKE_WAIT_MS}


def clamp_paste_gap(value):
    try:
        return max(MIN_PASTE_GAP_MS, min(MAX_PASTE_GAP_MS, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_PASTE_GAP_MS


def clamp_keyboard_opacity(value):
    try:
        return max(MIN_KEYBOARD_OPACITY, min(MAX_KEYBOARD_OPACITY, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_KEYBOARD_OPACITY


def host_env(display=None, steam_display=None):
    """Build a clean environment, leaving out the loader's bundled libraries."""
    env = {
        "HOME": USER_HOME,
        "PATH": "/usr/bin:/bin",
        "LANG": "ko_KR.UTF-8",
        "LC_ALL": "ko_KR.UTF-8",
        "XDG_RUNTIME_DIR": runtime_dir(),
    }
    if display:
        env["DISPLAY"] = display
    if steam_display:
        env["HANGUL_BRIDGE_STEAM_DISPLAY"] = steam_display
    return env


def run_host(args, display=None):
    """Run a short host command and return its stdout, or None on failure."""
    try:
        result = subprocess.run(
            args,
            env=host_env(display),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("%s failed: %s", args[0], error)
        return None
    return result.stdout if result.returncode == 0 else None


def list_displays():
    try:
        names = os.listdir("/tmp/.X11-unix")
    except OSError:
        return []
    return sorted(f":{name[1:]}" for name in names if name.startswith("X"))


def steam_display():
    """Return the display Steam's own UI runs on."""
    output = run_host(["pgrep", "-x", "steam"])
    pids = output.split() if output else []
    if not pids:
        return None
    try:
        with open(f"/proc/{pids[0]}/environ", "rb") as environ:
            for entry in environ.read().split(b"\0"):
                if entry.startswith(b"DISPLAY="):
                    return entry[len(b"DISPLAY="):].decode()
    except OSError:
        pass
    return None


def read_focus(display):
    """Return gamescope's focused (app id, window id), either of them may be None."""
    output = run_host(
        ["xprop", "-root", "GAMESCOPE_FOCUSED_APP", "GAMESCOPE_FOCUSED_WINDOW"],
        display=display,
    )
    values = {}
    for line in (output or "").splitlines():
        name, _, value = line.partition("=")
        value = value.strip()
        if value.isdigit() and value != "0":
            values[name.split("(")[0].strip()] = value
    return values.get("GAMESCOPE_FOCUSED_APP"), values.get("GAMESCOPE_FOCUSED_WINDOW")


def is_running(process):
    return process is not None and process.poll() is None


def stop(process):
    if not is_running(process):
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start(args, display, steam_display):
    log.info("starting %s on %s", args[-1], display)
    return subprocess.Popen(
        args,
        env=host_env(display, steam_display),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_leftover_daemons():
    """Stop input servers an earlier load of the plugin left running."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
                args = cmdline.read().split(b"\0")
        except OSError:
            continue
        if len(args) > 1 and args[1] == DAEMON_SCRIPT.encode():
            log.info("stopping leftover daemon %s", pid)
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass


def clean_game(game):
    return {
        **{key: bool(game.get(key)) for key in GAME_OPTIONS},
        **{key: bool(game.get(key, default)) for key, default in GAME_SETTINGS.items()},
        WAKE_WAIT_MS: clamp_wake_wait(game.get(WAKE_WAIT_MS, DEFAULT_WAKE_WAIT_MS)),
    }


def clean_games(games):
    return {app: clean_game(game) for app, game in games.items()}


class Bridge:
    def __init__(self):
        # Launch options belong to a Steam account, so the games do too.
        self.accounts = {}  # Steam ID -> games
        self.account = None
        self.games = {}  # the current account's: app id -> {option: value}
        self.legacy_games = {}  # saved before games were kept per account
        self.keep_keyboard_on_enter = True
        self.keyboard_opacity = DEFAULT_KEYBOARD_OPACITY
        self.backspace_hold_ms = DEFAULT_BACKSPACE_HOLD_MS
        self.paste_gap_ms = DEFAULT_PASTE_GAP_MS
        self.toggle_keys = list(TOGGLE_KEY_NAMES)
        self.paused = False
        self.game_window = None
        self.active_game = None
        self.in_game = False
        self.input_state = {}  # (display, input type) -> bool
        self.xim_servers = {}

    def load(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as settings:
                saved = json.load(settings)
        except (OSError, ValueError):
            saved = {}
        self.accounts = {
            account: clean_games(games) for account, games in saved.get("accounts", {}).items()
        }
        self.legacy_games = clean_games(saved.get("legacy_games", saved.get("games", {})))
        self.keep_keyboard_on_enter = bool(saved.get("keep_keyboard_on_enter", True))
        self.keyboard_opacity = clamp_keyboard_opacity(
            saved.get("keyboard_opacity", DEFAULT_KEYBOARD_OPACITY)
        )
        self.backspace_hold_ms = clamp_backspace_hold(
            saved.get("backspace_hold_ms", DEFAULT_BACKSPACE_HOLD_MS)
        )
        self.paste_gap_ms = clamp_paste_gap(saved.get("paste_gap_ms", DEFAULT_PASTE_GAP_MS))
        saved_keys = saved.get("toggle_keys", TOGGLE_KEY_NAMES)
        self.toggle_keys = [name for name in TOGGLE_KEY_NAMES if name in saved_keys]
        self.paused = bool(saved.get("paused", False))

    def save(self):
        """Write the settings in one step, so a crash cannot leave half a file."""
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        temporary = f"{SETTINGS_FILE}.tmp"
        with open(temporary, "w", encoding="utf-8") as settings:
            json.dump(
                {
                    "accounts": self.accounts,
                    "legacy_games": self.legacy_games,
                    "keep_keyboard_on_enter": self.keep_keyboard_on_enter,
                    "keyboard_opacity": self.keyboard_opacity,
                    "backspace_hold_ms": self.backspace_hold_ms,
                    "paste_gap_ms": self.paste_gap_ms,
                    "toggle_keys": self.toggle_keys,
                    "paused": self.paused,
                },
                settings,
            )
        os.replace(temporary, SETTINGS_FILE)

    def use_account(self, account):
        """Switch to a Steam account's games. Until one is known no game is active."""
        if account == self.account:
            return
        self.account = account
        self.games = self.accounts.setdefault(account, {})
        self.game_window = None
        self.active_game = None
        self.in_game = False

    def unclaimed_games(self):
        return sorted(app for app in self.legacy_games if app not in self.games)

    def claim_games(self, apps):
        """Give the current account its games from before they were kept per account."""
        for app in apps:
            if app in self.legacy_games and app not in self.games:
                self.games[app] = dict(self.legacy_games[app])

    def sync(self):
        """Match the running processes to the current focus and settings."""
        if self.paused or not self.games:
            self.stop_everything()
            return

        ui_display = steam_display()
        if not ui_display:
            return

        game_displays = [d for d in list_displays() if d != ui_display]
        for display, process in list(self.xim_servers.items()):
            if display not in game_displays:
                stop(process)
                del self.xim_servers[display]
        for display in game_displays:
            if not is_running(self.xim_servers.get(display)):
                self.xim_servers[display] = start(
                    ["/usr/bin/python3", DAEMON_SCRIPT, display], display, ui_display
                )

        focused, window = read_focus(ui_display)
        if focused in self.games:
            self.game_window = window
            self.active_game = focused
        # Opening the Steam keyboard changes the focused app to Steam's, while the
        # focused window stays the game's. Typing has to keep working then, since
        # that is exactly when the player is typing.
        self.in_game = focused in self.games or (
            window is not None and window == self.game_window
        )
        game = self.games.get(self.active_game, {}) if self.in_game else {}
        for display in game_displays:
            for input_type in GAME_OPTIONS:
                self.set_input_state(display, input_type, bool(game.get(input_type)))
            self.set_input_state(display, BACKSPACE_HOLD_MS, self.backspace_hold_ms)
            self.set_input_state(display, PASTE_GAP_MS, self.paste_gap_ms)
            self.set_input_state(display, WAKE_WAIT_MS, game.get(WAKE_WAIT_MS, DEFAULT_WAKE_WAIT_MS))
            self.set_input_state(display, TOGGLE_KEYS, ",".join(self.toggle_keys))

    def set_input_state(self, display, input_type, active):
        """Tell a daemon whether the focused game uses one kind of input, or a value
        it needs."""
        if self.input_state.get((display, input_type)) == active:
            return
        with open(input_state_path(display, input_type), "w", encoding="utf-8") as state:
            state.write(active if isinstance(active, str) else str(int(active)))
        self.input_state[(display, input_type)] = active

    def stop_everything(self):
        self.game_window = None
        self.active_game = None
        self.in_game = False
        self.input_state = {}
        for process in self.xim_servers.values():
            stop(process)
        self.xim_servers = {}


bridge = Bridge()
sync_task = None


async def sync_loop():
    while True:
        try:
            await asyncio.to_thread(bridge.sync)
        except Exception:
            log.exception("sync failed")
        await asyncio.sleep(POLL_SECONDS)


def is_wine_binary(path):
    """Tell whether a process's binary is Wine itself, which runs every game."""
    binary = os.path.basename(path)
    return binary.startswith("wine") and binary != "wineserver"


def game_process_name(command):
    """Return the program name from a Wine process's command line, or None.

    A game need not be an .exe: it can run as a file with another ending. Wine
    can rewrite the first argument to hold the whole command line, so it may also
    include arguments. Wine's own processes and Proton's helpers are left out.
    """
    if not command or OWN_PROCESS.search(command):
        return None
    match = EXE_NAME.search(command)
    if match:
        return match.group(1)
    return re.split(r"[\\/]", command)[-1].split(" ")[0] or None


def list_game_processes():
    """Return the running game programs, and whether each can reach the IME."""
    processes = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if not is_wine_binary(os.readlink(f"/proc/{pid}/exe")):
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as cmdline:
                command = cmdline.read().split(b"\0")[0].decode(errors="replace")
            name = game_process_name(command)
            if not name:
                continue
            with open(f"/proc/{pid}/environ", "rb") as environ:
                entries = environ.read().split(b"\0")
        except OSError:
            continue
        variables = dict(entry.split(b"=", 1) for entry in entries if b"=" in entry)
        processes.append(
            {
                "pid": int(pid),
                "name": name,
                "app_id": variables.get(b"SteamAppId", b"").decode(errors="replace"),
                "has_xim_option": variables.get(b"XMODIFIERS") == XIM_MODIFIER,
            }
        )
    return sorted(processes, key=lambda process: process["pid"])


def hangul_mode_on():
    """Tell whether any display's daemon is in Hangul mode for physical keyboards."""
    for display in bridge.xim_servers:
        name = input_state_path(display, MODE)
        try:
            with open(name, encoding="utf-8") as mode:
                if mode.read().strip() == "1":
                    return True
        except OSError:
            pass
    return False


def request_hangul_mode(enabled):
    """Ask each display's daemon to change mode, in one step as it reads this live."""
    for display in bridge.xim_servers:
        name = input_state_path(display, MODE_REQUEST)
        with open(f"{name}.tmp", "w", encoding="utf-8") as request:
            request.write("1" if enabled else "0")
        os.replace(f"{name}.tmp", name)


def keyboard_mode():
    """Whether the game in focus uses the bridge, which then composes Hangul from the
    letters the Steam keyboard types, and whether Hangul mode is on."""
    game = bridge.games.get(bridge.active_game, {}) if bridge.in_game and not bridge.paused else {}
    return {
        "active": bool(game.get(PHYSICAL)),
        "hangul": hangul_mode_on(),
        "english_on_close": bool(game.get(ENGLISH_ON_CLOSE)),
    }


def current_state():
    return {
        "games": bridge.games,
        "active": bridge.in_game,
        "xim_running": any(is_running(p) for p in bridge.xim_servers.values()),
        "processes": list_game_processes(),
        "keep_keyboard_on_enter": bridge.keep_keyboard_on_enter,
        "keyboard_opacity": bridge.keyboard_opacity,
        "backspace_hold_ms": bridge.backspace_hold_ms,
        "paste_gap_ms": bridge.paste_gap_ms,
        "toggle_keys": bridge.toggle_keys,
        "paused": bridge.paused,
        "hangul_mode": hangul_mode_on(),
    }


def require_account():
    if bridge.account is None:
        raise ValueError("Steam account is not known yet")


class Plugin:
    # Decky calls these on the class itself, so `self` is not an instance and
    # the state lives at module level.
    async def _main(self):
        global sync_task
        await asyncio.to_thread(stop_leftover_daemons)
        bridge.load()
        sync_task = asyncio.create_task(sync_loop())
        log.info("loaded")

    async def _unload(self):
        if sync_task:
            sync_task.cancel()
        await asyncio.to_thread(bridge.stop_everything)

    async def get_state(self):
        return await asyncio.to_thread(current_state)

    async def set_account(self, steam_id):
        """Follow the logged-in Steam account, and answer with the games from before
        they were kept per account that this account has not taken yet."""
        account = str(steam_id)
        if not account.isdigit():
            raise ValueError("unknown Steam account")
        bridge.use_account(account)
        bridge.save()
        return bridge.unclaimed_games()

    async def claim_games(self, app_ids):
        bridge.claim_games([str(app) for app in app_ids])
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_game_enabled(self, app_id, enabled):
        """Use the bridge in a game, for both the Steam keyboard and the physical
        one, or stop, which forgets the game's other options too."""
        require_account()
        app = str(app_id)
        if enabled:
            game = bridge.games.get(app, new_game())
            game[VIRTUAL] = game[PHYSICAL] = True
            bridge.games[app] = game
        else:
            bridge.games.pop(app, None)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_game_input(self, app_id, input_type, enabled):
        """Turn one option on or off for a game. A game with neither keyboard is
        no longer registered, and its other options go with it."""
        if input_type not in GAME_OPTIONS:
            raise ValueError(f"unknown option {input_type}")
        require_account()
        app = str(app_id)
        game = bridge.games.get(app, new_game())
        game[input_type] = bool(enabled)
        if game["virtual"] or game["physical"]:
            bridge.games[app] = game
        else:
            bridge.games.pop(app, None)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_send_method(self, app_id, method):
        """Choose how a registered game gets the composition. The ways exclude each
        other, so choosing one turns the others off."""
        if method not in SEND_METHODS:
            raise ValueError(f"unknown send method {method}")
        game = bridge.games.get(str(app_id))
        if game is None:
            raise ValueError(f"game {app_id} is not registered")
        for option in (DIRECT, PASTE):
            game[option] = option in SEND_METHODS[method]
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_toggle_key(self, name, enabled):
        if name not in TOGGLE_KEY_NAMES:
            raise ValueError(f"unknown toggle key {name}")
        chosen = set(bridge.toggle_keys) - {name} | ({name} if enabled else set())
        bridge.toggle_keys = [key for key in TOGGLE_KEY_NAMES if key in chosen]
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_wake_wait_ms(self, app_id, milliseconds):
        game = bridge.games.get(str(app_id))
        if game is None:
            raise ValueError(f"game {app_id} is not registered")
        game[WAKE_WAIT_MS] = clamp_wake_wait(milliseconds)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_paste_gap_ms(self, milliseconds):
        bridge.paste_gap_ms = clamp_paste_gap(milliseconds)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def remove_game(self, app_id):
        bridge.games.pop(str(app_id), None)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_game_setting(self, app_id, name, enabled):
        """Change one of a registered game's settings in GAME_SETTINGS."""
        if name not in GAME_SETTINGS:
            raise ValueError(f"unknown setting {name}")
        game = bridge.games.get(str(app_id))
        if game is None:
            raise ValueError(f"game {app_id} is not registered")
        game[name] = bool(enabled)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_keep_keyboard_on_enter(self, enabled):
        bridge.keep_keyboard_on_enter = bool(enabled)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_paused(self, paused):
        bridge.paused = bool(paused)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_backspace_hold_ms(self, milliseconds):
        bridge.backspace_hold_ms = clamp_backspace_hold(milliseconds)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_keyboard_opacity(self, percent):
        bridge.keyboard_opacity = clamp_keyboard_opacity(percent)
        bridge.save()
        return await asyncio.to_thread(current_state)

    async def set_hangul_mode(self, enabled):
        request_hangul_mode(bool(enabled))
        await asyncio.sleep(MODE_REQUEST_WAIT_SECONDS)
        return await asyncio.to_thread(current_state)

    async def get_keyboard_mode(self):
        """What the Steam keyboard needs, often and cheaply."""
        return await asyncio.to_thread(keyboard_mode)

    async def set_keyboard_hangul(self, enabled):
        """Ask for Hangul mode on or off, and answer once the daemon has it, or has
        had time to take it, so the Steam keyboard sends letters only once they are
        composed."""
        wanted = bool(enabled)
        request_hangul_mode(wanted)
        waited = 0.0
        while waited < MODE_SETTLE_SECONDS:
            await asyncio.sleep(MODE_POLL_SECONDS)
            waited += MODE_POLL_SECONDS
            if await asyncio.to_thread(hangul_mode_on) == wanted:
                break
        return await asyncio.to_thread(keyboard_mode)

