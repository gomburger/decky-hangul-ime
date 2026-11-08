import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "py_modules"))
sys.dont_write_bytecode = True

from hangul_composer import HangulComposer  # noqa: E402


def type_all(text):
    composer = HangulComposer()
    out = ""
    for ch in text:
        out += composer.feed(ch)
    return out + composer.flush()


class ComposeTests(unittest.TestCase):
    def test_syllables(self):
        cases = {
            "ㄱㅏㄴㅏㄷㅏ": "가나다",
            "ㅎㅏㄴㄱㅡㄹ": "한글",
            "ㄷㅏㄹㄱ": "닭",
            "ㄷㅏㄹㄱㅏ": "달가",
            "ㄱㅏㅂㅅㅣ": "갑시",
            "ㄱㅗㅏ": "과",
            "ㅇㅜㅣ": "위",
            "ㅇㅡㅣ": "의",
            "ㄲㅗㅊ": "꽃",
            "ㅏ": "ㅏ",
            "ㄱㄴ": "ㄱㄴ",
            "ㅏㅏ": "ㅏㅏ",
            "ㅏㄱ": "ㅏㄱ",
            "ㄸㅏㄸ": "따ㄸ",
        }
        for keys, expected in cases.items():
            self.assertEqual(type_all(keys), expected, keys)

    def test_preedit_steps(self):
        composer = HangulComposer()
        seen = []
        for ch in "ㄱㅏㄴㅏ":
            composer.feed(ch)
            seen.append(composer.preedit)
        self.assertEqual(seen, ["ㄱ", "가", "간", "나"])

    def test_backspace_removes_one_jamo(self):
        composer = HangulComposer()
        for ch in "ㄷㅏㄹㄱ":
            composer.feed(ch)
        steps = []
        while composer.backspace():
            steps.append(composer.preedit)
        self.assertEqual(steps, ["달", "다", "ㄷ", ""])
        self.assertFalse(composer.backspace())


    def test_consonant_cluster_without_vowel(self):
        composer = HangulComposer()
        shown = []
        committed = ""
        for ch in "ㅂㅅㅏ":
            committed += composer.feed(ch)
            shown.append(composer.preedit)
        self.assertEqual(shown, ["ㅂ", "ㅄ", "사"])
        self.assertEqual(committed + composer.flush(), "ㅂ사")

    def test_consonants_that_do_not_cluster(self):
        composer = HangulComposer()
        committed = ""
        for ch in "ㄱㄱㄴ":
            committed += composer.feed(ch)
        self.assertEqual(committed + composer.flush(), "ㄱㄱㄴ")


if __name__ == "__main__":
    unittest.main()
