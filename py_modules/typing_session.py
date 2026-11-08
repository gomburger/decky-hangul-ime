"""The typing state: Hangul composition, and when its text reaches the game.

Physical keys are composed here. The Steam keyboard composes by itself, and its text
comes in already composed through steam_text. Both show in the game the same way.

In direct mode the output types the composition into the game as text and erases it
when it changes, for games that only show finished text. Nothing is composed in the
game then, so the game never answers a commit with a reset, and nothing waits for one.
"""

from hangul_composer import HangulComposer
from xim_server import SHIFT_MASK

COMMIT_WAIT_SECONDS = 0.06
COMMIT_SETTLE_SECONDS = 0.04
# How long after a commit a reset still counts as the game's answer to it.
LATE_RESET_SECONDS = 0.5

OTHER_MODIFIERS = 0x4C  # Control, Mod1 (Alt), Mod4 (Super)
BACKSPACE_CODE = 14

# Dubeolsik layout by physical key, as Linux input codes.
PHYSICAL_JAMO = {
    16: "ㅂ", 17: "ㅈ", 18: "ㄷ", 19: "ㄱ", 20: "ㅅ", 21: "ㅛ", 22: "ㅕ", 23: "ㅑ", 24: "ㅐ", 25: "ㅔ",
    30: "ㅁ", 31: "ㄴ", 32: "ㅇ", 33: "ㄹ", 34: "ㅎ", 35: "ㅗ", 36: "ㅓ", 37: "ㅏ", 38: "ㅣ",
    44: "ㅋ", 45: "ㅌ", 46: "ㅊ", 47: "ㅍ", 48: "ㅠ", 49: "ㅜ", 50: "ㅡ",
}
PHYSICAL_JAMO_SHIFTED = {16: "ㅃ", 17: "ㅉ", 18: "ㄸ", 19: "ㄲ", 20: "ㅆ", 24: "ㅒ", 25: "ㅖ"}


class TypingSession:
    """
    output   has commit(text) and preedit(text), which show text in the game
    timers   has call_later(seconds, function), returning a timer with cancel()
    """

    def __init__(self, output, timers):
        self.output = output
        self.timers = timers
        self.direct = False
        self.composer = HangulComposer()
        self.steam_composing = ""
        self.pending = ""
        self.commit_timer = None
        self.hold = False
        self.hold_timer = None
        self.recent_commit_timer = None

    def on_reset(self):
        if self.pending:
            self.deliver_commit()
        elif self.recent_commit_timer and not self.hold and self.composing:
            # A game running at a low frame rate answers a commit only after the
            # next composition is already showing, and the reset clears it. What is
            # being composed is still here, so show it again.
            self._show_preedit()

    @property
    def composing(self):
        return self.steam_composing or self.composer.preedit

    def _show_preedit(self):
        if not self.hold:
            self.output.preedit(self.composing)

    def _finish_syllable(self, text):
        """Queue finished text until the game has dealt with the composition ending.
        It answers with a reset, which throws away any commit Xlib is still holding,
        so sending the text earlier could lose it."""
        if not text:
            return
        self.pending += text
        if self.direct:
            self.deliver_commit()
            return
        self.output.preedit("")
        self.hold = True
        if self.hold_timer:
            self.hold_timer.cancel()
            self.hold_timer = None
        if not self.commit_timer:
            self.commit_timer = self.timers.call_later(COMMIT_WAIT_SECONDS, self._commit_wait_over)

    def _commit_wait_over(self):
        self.commit_timer = None
        self.deliver_commit()

    def deliver_commit(self):
        if not self.pending:
            return
        if self.commit_timer:
            self.commit_timer.cancel()
            self.commit_timer = None
        text, self.pending = self.pending, ""
        self.output.commit(text)
        if self.direct:
            return
        if self.recent_commit_timer:
            self.recent_commit_timer.cancel()
        self.recent_commit_timer = self.timers.call_later(LATE_RESET_SECONDS, self._commit_is_old)
        self.hold = True
        if self.hold_timer:
            self.hold_timer.cancel()
        self.hold_timer = self.timers.call_later(COMMIT_SETTLE_SECONDS, self._end_hold)

    def _commit_is_old(self):
        self.recent_commit_timer = None

    def _end_hold(self):
        self.hold_timer = None
        self.hold = False
        self._show_preedit()

    def finish(self):
        """Send everything at once, because a key is about to follow it."""
        finished = self.composer.flush() + self.steam_composing
        self.steam_composing = ""
        if finished:
            self.pending += finished
            if not self.direct:
                self.output.preedit("")
        self.deliver_commit()

    def drop_composing(self):
        """Forget what is being composed, which in direct mode is already typed in
        an input field the game has just left."""
        self.composer.flush()
        self.steam_composing = ""

    def steam_text(self, finished, composing):
        """Show text the Steam keyboard composed: finished text, and what it is
        composing now."""
        self._finish_syllable(self.composer.flush() + finished)
        self.steam_composing = composing
        self._show_preedit()

    def _type_jamo(self, jamo):
        self._finish_syllable(self.steam_composing + self.composer.feed(jamo))
        self.steam_composing = ""
        self._show_preedit()

    def _erase_jamo(self):
        self.composer.backspace()
        self._show_preedit()

    def physical_key(self, code, state):
        """Handle a key press in Hangul mode. Returns True if it became part of the
        text, and False if it should go on to the game."""
        if not state & OTHER_MODIFIERS:
            shifted = PHYSICAL_JAMO_SHIFTED.get(code) if state & SHIFT_MASK else None
            jamo = shifted or PHYSICAL_JAMO.get(code)
            if jamo:
                self._type_jamo(jamo)
                return True
            if code == BACKSPACE_CODE and not self.composer.empty:
                self._erase_jamo()
                return True
        self.finish()
        return False
