"""The small files the plugin and each display's daemon talk through.

Both sides build the names here, so they cannot drift apart.
"""

import os

# Written by the plugin: whether the focused game uses each option. These are also
# the option names in the settings and the UI.
VIRTUAL = "virtual"  # the Steam keyboard
PHYSICAL = "physical"  # the physical keyboard
DIRECT = "direct"  # typing the composition as text
ERASE_TEXT = "erase_text"  # erasing it with Backspace characters rather than keys
PASTE = "paste"  # pasting finished text with Ctrl+V, for games the IME cannot reach
IME_WAKE = "ime_wake"  # pressing a key after Enter, so the game turns its IME back on
PASTE_BATCH = "paste_batch"  # holding pasted text on an input line until Enter
GAME_OPTIONS = (VIRTUAL, PHYSICAL, DIRECT, ERASE_TEXT, PASTE, IME_WAKE, PASTE_BATCH)
# Written by the plugin: how long direct mode holds Backspace, in milliseconds.
BACKSPACE_HOLD_MS = "backspace_hold_ms"
# Written by the plugin: each wait of a paste, in milliseconds.
PASTE_GAP_MS = "paste_gap_ms"
# Written by the plugin: after Enter, how long before the key that wakes the focused
# game's IME, in milliseconds. Also the name of that setting for each game.
WAKE_WAIT_MS = "wake_wait_ms"
# Written by the plugin: the keys that switch between Hangul and English, by name,
# comma separated.
TOGGLE_KEYS = "toggle_keys"
# Written by the plugin: whether the Steam keyboard's input row could not be found.
BATCH_ROW_MISSING = "batch_row_missing"
# Written by the daemon, readable only by this user: the text held for sending, for
# the Steam keyboard's input row. Only there while there is such text.
BATCH_TEXT = "batch-text"
# Written by the daemon: how the Steam keyboard's Hangul last came in, as one of
# the words below. No text, only the way.
STEAM_PATH = "steam-path"
STEAM_PATH_LETTERS = "letters"  # letters the plugin turned the jamo keys into, composed here
STEAM_PATH_READBACK = "readback"  # Hangul Steam composed, read back: the fallback
# Written by the daemon: whether physical keys are typing Hangul.
MODE = "mode"
# Written by the plugin: the mode its button asks for, removed by the daemon once read.
MODE_REQUEST = "mode-request"


def state_path(runtime_dir, display, name):
    return os.path.join(runtime_dir, f"hangul-bridge-{display.lstrip(':')}.{name}")
