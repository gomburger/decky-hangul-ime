"""Keys the Steam keyboard typed, read after it put the usual key map back."""

import collections
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.dont_write_bytecode = True

import keymap_watch  # noqa: E402

KIEUK, THIEUTH, ONE = 0x100314B, 0x100314C, 0x31


def watch_with_notes(*notes):
    watch = keymap_watch.KeymapWatch.__new__(keymap_watch.KeymapWatch)
    watch._notes = collections.deque((time.monotonic(), keycode, keysym) for keycode, keysym in notes)
    watch.process_events = lambda: None
    return watch


class KeymapWatchTest(unittest.TestCase):
    def test_a_key_read_too_late_gets_its_note(self):
        watch = watch_with_notes((10, KIEUK), (10, KIEUK))
        self.assertEqual(watch.take(10, ONE), KIEUK)
        self.assertEqual(watch.take(10, ONE), KIEUK)
        self.assertEqual(watch.take(10, ONE), ONE)  # no note left: the player's own 1

    def test_the_oldest_note_wins_over_a_newer_map(self):
        watch = watch_with_notes((10, KIEUK), (10, THIEUTH))
        self.assertEqual(watch.take(10, THIEUTH), KIEUK)
        self.assertEqual(watch.take(10, THIEUTH), THIEUTH)

    def test_notes_are_per_key(self):
        watch = watch_with_notes((10, KIEUK), (11, THIEUTH))
        self.assertEqual(watch.take(11, ONE), THIEUTH)
        self.assertEqual(watch.take(10, ONE), KIEUK)

    def test_old_notes_expire(self):
        watch = watch_with_notes((10, KIEUK))
        watch._notes[0] = (time.monotonic() - 5, 10, KIEUK)
        self.assertEqual(watch.take(10, ONE), ONE)


if __name__ == "__main__":
    unittest.main()
