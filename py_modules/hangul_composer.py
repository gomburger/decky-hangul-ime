"""Two-set (dubeolsik) Hangul composer driven by compatibility jamo (U+31xx)."""

CHOSEONG = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
JUNGSEONG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
JONGSEONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"

COMPOUND_VOWEL = {
    "ㅗㅏ": "ㅘ", "ㅗㅐ": "ㅙ", "ㅗㅣ": "ㅚ",
    "ㅜㅓ": "ㅝ", "ㅜㅔ": "ㅞ", "ㅜㅣ": "ㅟ",
    "ㅡㅣ": "ㅢ",
}
COMPOUND_FINAL = {
    "ㄱㅅ": "ㄳ", "ㄴㅈ": "ㄵ", "ㄴㅎ": "ㄶ",
    "ㄹㄱ": "ㄺ", "ㄹㅁ": "ㄻ", "ㄹㅂ": "ㄼ", "ㄹㅅ": "ㄽ",
    "ㄹㅌ": "ㄾ", "ㄹㅍ": "ㄿ", "ㄹㅎ": "ㅀ",
    "ㅂㅅ": "ㅄ",
}


def _is_vowel(ch):
    return ch in JUNGSEONG


def _syllable(lead, vowel, final=""):
    if lead and vowel:
        return chr(
            0xAC00
            + CHOSEONG.index(lead) * 588
            + JUNGSEONG.index(vowel) * 28
            + (JONGSEONG.index(final) if final else 0)
        )
    return lead or vowel or ""


class HangulComposer:
    """Holds the syllable being composed.

    Backspace replays the keys of the current syllable, so it removes one
    jamo at a time, as a Windows Korean IME does.
    """

    def __init__(self):
        self._keys = []

    @property
    def preedit(self):
        return _syllable(*self._parts(self._keys))

    @property
    def empty(self):
        return not self._keys

    def feed(self, jamo):
        """Add one jamo. Returns text to commit (possibly empty)."""
        committed, kept = self._step(self._keys, jamo)
        self._keys = kept
        return committed

    def backspace(self):
        """Remove one jamo. Returns False if nothing was being composed."""
        if not self._keys:
            return False
        self._keys.pop()
        return True

    def flush(self):
        """Finish the current syllable and return it."""
        text = self.preedit
        self._keys = []
        return text

    @staticmethod
    def _parts(keys):
        lead = vowel = final = ""
        for key in keys:
            if _is_vowel(key):
                vowel = COMPOUND_VOWEL.get(vowel + key, key)
            elif vowel:
                final = COMPOUND_FINAL.get(final + key, key)
            else:
                # Two consonants with no vowel yet show as a cluster: ㄱ+ㅅ -> ㄳ
                lead = COMPOUND_FINAL.get(lead + key, key)
        return lead, vowel, final

    @classmethod
    def _step(cls, keys, jamo):
        lead, vowel, final = cls._parts(keys)
        current = _syllable(lead, vowel, final)

        if not keys:
            return "", [jamo]

        if _is_vowel(jamo):
            if final:
                # A vowel takes the last final consonant as its lead: 간+ㅏ -> 가나
                carried = keys[-1]
                head = _syllable(*cls._parts(keys[:-1]))
                return head, [carried, jamo]
            if vowel and vowel + jamo in COMPOUND_VOWEL:
                return "", keys + [jamo]
            if lead and not vowel:
                if len(keys) == 2:
                    # A vowel after a cluster takes its second consonant: ㄳ+ㅏ -> ㄱ사
                    return keys[0], [keys[1], jamo]
                return "", keys + [jamo]
            return current, [jamo]

        if lead and not vowel and len(keys) == 1 and lead + jamo in COMPOUND_FINAL:
            return "", keys + [jamo]
        if lead and vowel:
            if not final and jamo in JONGSEONG[1:]:
                return "", keys + [jamo]
            if final and final + jamo in COMPOUND_FINAL:
                return "", keys + [jamo]
        return current, [jamo]
