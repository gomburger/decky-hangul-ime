"""Holding typed text until it is sent as one paste.

The game sees none of it until then, so what is held is shown: on the Steam
keyboard's input row while that is open, otherwise on the overlay. For the input row
it goes through a file only this user can read, which holds nothing but the current
text and is gone once the text is sent or dropped. It is never logged.
"""

import os


class BatchDisplay:
    """Shows the held text in the place that suits.

    path          the file the plugin reads the text for the input row from
    steam_typing  tells whether the Steam keyboard is open
    overlay       has show(text), or None where there is no overlay
    """

    def __init__(self, path, steam_typing, overlay):
        self.path = path
        self.steam_typing = steam_typing
        self.overlay = overlay
        self.written = ""
        self._remove()  # what a server that was killed left behind

    def _remove(self):
        for path in (self.path, f"{self.path}.tmp"):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _write(self, text):
        if text == self.written:
            return
        self.written = text
        if not text:
            self._remove()
            return
        temporary = f"{self.path}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as held:
            held.write(text)
        os.replace(temporary, self.path)

    def show(self, text):
        in_row = self.steam_typing()
        self._write(text if in_row else "")
        if self.overlay:
            self.overlay.show("" if in_row else text)


class TextBatch:
    """Takes the output's place in the typing session.

    paste    has commit(text, real_v)
    display  has show(text), where empty text takes it away
    """

    def __init__(self, paste, display):
        self.paste = paste
        self.display = display
        self.text = ""
        self.composing = ""

    @property
    def empty(self):
        return not self.text and not self.composing

    def refresh(self):
        self.display.show(self.text + self.composing)

    def commit(self, text):
        # What was being composed has just become this text.
        self.text += text
        self.composing = ""
        self.refresh()

    def preedit(self, text):
        self.composing = text
        self.refresh()

    def erase(self):
        self.text = self.text[:-1]
        self.refresh()

    def clear(self):
        self.text = self.composing = ""
        self.refresh()

    def flush(self):
        text = self.text
        self.clear()
        if text:
            self.paste.commit(text, real_v=True)
