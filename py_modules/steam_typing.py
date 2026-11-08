"""Reads the Steam keyboard's typing, which reaches the game as ordinary keys.

Steam composes Hangul itself. A game cannot show its composition, so Steam types the
syllable being composed into the game, and each time it changes, erases it with
Backspace and types it again, along with any syllable it finished:

    ㄱ                 ->  ᄀ
    ㅏ                 ->  Backspace, 가
    ㄴ                 ->  Backspace, 간
    ㅏ                 ->  Backspace, 가 나

A lone jamo comes as a conjoining jamo, which Wine cannot show, so it is turned
into its compatibility jamo (U+3131 block). Consonant clusters such as ㅄ can come
as archaic initial consonants, so every conjoining jamo block is covered.

These keys are kept from the game. The last character Steam typed is the syllable
being composed, and goes to the game as the IME's composition; anything typed before
it in the same burst is finished text. A Backspace with nothing typed after it is the
player's, either clearing a lone jamo being composed or going on to the game.
"""

import unicodedata

ERASE_WAIT_SECONDS = 0.03
BACKSPACE_KEYSYM = 0xFF08
UNICODE_KEYSYM = 0x01000000

# The conjoining jamo blocks: Hangul Jamo, Extended-A and Extended-B.
CONJOINING_BLOCKS = (range(0x1100, 0x1200), range(0xA960, 0xA980), range(0xD7B0, 0xD800))
MODERN_COMPATIBILITY = range(0x3131, 0x3164)


def _conjoining_to_compatibility():
    """Pair each conjoining jamo with the modern compatibility jamo of the same
    letters, going by their Unicode names: HANGUL CHOSEONG RIEUL-KIYEOK and
    HANGUL JONGSEONG RIEUL-KIYEOK are both HANGUL LETTER RIEUL-KIYEOK."""
    letters = {}
    for point in MODERN_COMPATIBILITY:
        name = unicodedata.name(chr(point), "")
        if name.startswith("HANGUL LETTER "):
            letters[name[len("HANGUL LETTER "):]] = chr(point)
    table = {}
    for block in CONJOINING_BLOCKS:
        for point in block:
            name = unicodedata.name(chr(point), "")
            for kind in ("HANGUL CHOSEONG ", "HANGUL JUNGSEONG ", "HANGUL JONGSEONG "):
                if name.startswith(kind) and name[len(kind):] in letters:
                    table[point] = letters[name[len(kind):]]
    return table


CONJOINING_TO_COMPATIBILITY = _conjoining_to_compatibility()


def hangul_text(keysym):
    """Return the Hangul character a keysym types, or None if it types none."""
    if not UNICODE_KEYSYM + 0x100 <= keysym <= UNICODE_KEYSYM + 0x10FFFF:
        return None
    point = keysym - UNICODE_KEYSYM
    if 0xAC00 <= point <= 0xD7A3 or point in MODERN_COMPATIBILITY:
        return chr(point)
    return CONJOINING_TO_COMPATIBILITY.get(point)


class SteamTyping:
    """
    session has steam_text(commit, composing) and finish()
    timers  has call_later(seconds, function), returning a timer with cancel()
    """

    def __init__(self, session, timers):
        self.session = session
        self.timers = timers
        self.composing = ""
        self.erase_timer = None

    @property
    def busy(self):
        return bool(self.composing) or self.erase_timer is not None

    def on_text(self, text):
        """Steam typed a Hangul character."""
        if self.erase_timer:
            # Steam erased the syllable it was composing, and this replaces it.
            self.erase_timer.cancel()
            self.erase_timer = None
            finished = ""
        else:
            finished = self.composing
        self.composing = text
        self.session.steam_text(finished, text)

    def on_backspace(self):
        """Take a Backspace if it belongs to Steam's composing, and tell if it did."""
        if self.erase_timer:
            # A second Backspace: the first one cleared what was being composed.
            self._erase_over()
        if not self.composing:
            return False
        self.erase_timer = self.timers.call_later(ERASE_WAIT_SECONDS, self._erase_over)
        return True

    def _erase_over(self):
        """Nothing replaced what Steam erased, so nothing is being composed now."""
        if self.erase_timer:
            self.erase_timer.cancel()
            self.erase_timer = None
        self.composing = ""
        self.session.steam_text("", "")

    def drop(self):
        if self.erase_timer:
            self.erase_timer.cancel()
            self.erase_timer = None
        self.composing = ""

    def finish(self):
        """Another key is coming, which ends the composition."""
        if not self.busy:
            return
        if self.erase_timer:
            self._erase_over()
        self.composing = ""
        self.session.finish()
