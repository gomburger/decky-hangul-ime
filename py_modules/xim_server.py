"""A small XIM server that games reach through XMODIFIERS=@im=hangulbridge.

Keys go straight to the game unless the daemon asks for them, since each key
passed here makes a round trip. It asks only while Hangul mode is on or the Steam
keyboard is typing, and sends back whatever it does not use. The server pushes
composition and finished text to the client whenever it is told to. Only the parts
of the XIM protocol that Xlib clients need are implemented.
"""

import ctypes
import ctypes.util
import struct
import time

# Xlib constants.
KEY_PRESS = 2
CLIENT_MESSAGE = 33
SELECTION_REQUEST = 30
XA_ATOM = 4
XA_STRING = 31
PROP_MODE_REPLACE = 0
COMPOUND_TEXT_STYLE = 1
LC_CTYPE = 0
TEXT_LOCALE = b"ko_KR.UTF-8"
EVENT_SIZE = 192

# Offsets into XEvent on 64-bit.
OFFSET_WINDOW = 32
OFFSET_FORMAT = 48
OFFSET_DATA = 56
CM_DATA_SIZE = 20

# XIM protocol opcodes.
XIM_CONNECT = 1
XIM_CONNECT_REPLY = 2
XIM_DISCONNECT = 3
XIM_DISCONNECT_REPLY = 4
XIM_OPEN = 30
XIM_OPEN_REPLY = 31
XIM_CLOSE = 32
XIM_CLOSE_REPLY = 33
XIM_SET_EVENT_MASK = 37
XIM_ENCODING_NEGOTIATION = 38
XIM_ENCODING_NEGOTIATION_REPLY = 39
XIM_QUERY_EXTENSION = 40
XIM_QUERY_EXTENSION_REPLY = 41
XIM_GET_IM_VALUES = 44
XIM_GET_IM_VALUES_REPLY = 45
XIM_CREATE_IC = 50
XIM_CREATE_IC_REPLY = 51
XIM_DESTROY_IC = 52
XIM_DESTROY_IC_REPLY = 53
XIM_SET_IC_VALUES = 54
XIM_SET_IC_VALUES_REPLY = 55
XIM_GET_IC_VALUES = 56
XIM_GET_IC_VALUES_REPLY = 57
XIM_SET_IC_FOCUS = 58
XIM_UNSET_IC_FOCUS = 59
XIM_FORWARD_EVENT = 60
XIM_SYNC = 61
XIM_SYNC_REPLY = 62
XIM_COMMIT = 63
XIM_RESET_IC = 64
XIM_RESET_IC_REPLY = 65
XIM_PREEDIT_START = 73
XIM_PREEDIT_DRAW = 75
XIM_PREEDIT_DONE = 78

FORWARD_SYNCHRONOUS = 0x0001
KEY_EVENT_MASKS = 0x0003  # KeyPressMask | KeyReleaseMask
KEY_EVENT_STATE_OFFSET = 80  # in an XKeyEvent, on 64-bit, followed by the keycode
COMMIT_LOOKUP_CHARS = 0x0002
PREEDIT_STATUS_NO_FEEDBACK = 0x0002

# XIM value types.
TYPE_CARD32 = 3
TYPE_WINDOW = 5
TYPE_STYLES = 10
TYPE_RECTANGLE = 11
TYPE_POINT = 12
TYPE_FONTSET = 13
TYPE_NESTED = 0x7FFF
TYPE_SEPARATOR = 0

IM_ATTRIBUTES = [("queryInputStyle", TYPE_STYLES)]
IC_ATTRIBUTES = [
    ("inputStyle", TYPE_CARD32),
    ("clientWindow", TYPE_WINDOW),
    ("focusWindow", TYPE_WINDOW),
    ("filterEvents", TYPE_CARD32),
    ("preeditAttributes", TYPE_NESTED),
    ("statusAttributes", TYPE_NESTED),
    ("fontSet", TYPE_FONTSET),
    ("area", TYPE_RECTANGLE),
    ("areaNeeded", TYPE_RECTANGLE),
    ("colorMap", TYPE_CARD32),
    ("stdColorMap", TYPE_CARD32),
    ("foreground", TYPE_CARD32),
    ("background", TYPE_CARD32),
    ("backgroundPixmap", TYPE_CARD32),
    ("spotLocation", TYPE_POINT),
    ("lineSpace", TYPE_CARD32),
    ("separatorofNestedList", TYPE_SEPARATOR),
]
# Preedit by callbacks or by position, each with status by callbacks or nothing.
INPUT_STYLES = [0x404, 0x402, 0x408, 0x204, 0x202, 0x208]
ATTRIBUTE_FILTER_EVENTS = next(i for i, a in enumerate(IC_ATTRIBUTES) if a[0] == "filterEvents")
# Keysyms that switch between Hangul and English on a physical keyboard, and the
# modifiers each needs. Each is grabbed with Caps Lock and Num Lock on and off.
SHIFT_MASK = 0x01
LOCK_MODIFIERS = (0, 0x02, 0x10, 0x12)
ANY_MODIFIER = 0x8000
GRAB_MODE_ASYNC = 1
TOGGLE_KEYS = {
    "hangul": (0xFF31, 0),  # Hangul
    "right_alt": (0xFFEA, 0),  # Alt_R
    "shift_space": (0x0020, SHIFT_MASK),  # Shift+Space
}
# Between two toggles. Repeats are told apart where X can say which presses are
# repeats, and then this only covers a key that sends its press twice; otherwise
# it is what spaces out the toggles of a key held down.
TOGGLE_DEBOUNCE_SECONDS = 0.05
TOGGLE_DEBOUNCE_SECONDS_WITHOUT_REPEATS = 0.3
LOCALES = "@locale=C,POSIX,en_US,ko,ko_KR,ko_KR.UTF-8,ko_KR.utf8,en_US.UTF-8,en_US.utf8"
TRANSPORTS = "@transport=X/"


def _padding(length):
    return b"\0" * ((4 - length % 4) % 4)


def _attribute_list(attributes):
    packed = b""
    for index, (name, kind) in enumerate(attributes):
        encoded = name.encode()
        entry = struct.pack("<HHH", index, kind, len(encoded)) + encoded
        packed += entry + _padding(len(entry))
    return packed


X_ERROR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


class TextProperty(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.POINTER(ctypes.c_ubyte)),
        ("encoding", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("nitems", ctypes.c_ulong),
    ]


class Connection:
    def __init__(self, client_window, server_window):
        self.client_window = client_window
        self.server_window = server_window


class XimServer:
    def __init__(self, display_name, server_name="hangulbridge"):
        # Xlib converts text using the C library's locale, which starts out as "C".
        ctypes.CDLL(None).setlocale(LC_CTYPE, TEXT_LOCALE)
        self._x = ctypes.CDLL(ctypes.util.find_library("X11"))
        self._declare_functions()
        # By default Xlib ends the process on any X error, such as a toggle key that
        # another program already grabbed, or a client window that has just closed.
        self._error_handler = X_ERROR_HANDLER(self._on_x_error)
        self._x.XSetErrorHandler(self._error_handler)
        self._display = self._x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open X display {display_name}")
        self._root = self._x.XDefaultRootWindow(self._display)
        # A key held down repeats as a release and a press, which would read as the
        # toggle key let go and pressed again. Asked this way, a repeat comes as a
        # press alone. It applies to this connection only, not to the game's keys.
        supported = ctypes.c_int()
        detectable = self._x.XkbSetDetectableAutoRepeat(self._display, 1, ctypes.byref(supported))
        self._toggle_debounce = (
            TOGGLE_DEBOUNCE_SECONDS if detectable else TOGGLE_DEBOUNCE_SECONDS_WITHOUT_REPEATS
        )
        self._window = self._new_window()
        self._connections = {}
        self._next_property = 0
        self._next_im = 1
        self._next_ic = 1
        self._focus = None  # (connection, im id, ic id)
        self._preedit_on = False
        self._preedit_length = 0
        self.on_focus = lambda _focused: None
        self.on_reset = lambda: None
        self.on_key = lambda _context, _serial, _event: None
        self.on_toggle = lambda: None
        self.on_toggle_released = lambda: None
        self.on_grabbed_key = lambda keycode, state, pressed: None
        self._contexts = set()
        self._forward_keys = False
        self._grabbed = {}  # keycode -> the modifiers its toggle key needs
        self._toggle_keycode = None  # the key that last toggled
        self._toggle_down = set()  # toggle keys held down now
        self._last_toggle = 0.0

        self._atom_protocol = self._atom("_XIM_PROTOCOL")
        self._atom_connect = self._atom("_XIM_XCONNECT")
        self._atom_locales = self._atom("LOCALES")
        self._atom_transport = self._atom("TRANSPORT")
        self._register(server_name)

    def _declare_functions(self):
        x = self._x
        ulong = ctypes.c_ulong
        void = ctypes.c_void_p
        x.XOpenDisplay.restype = void
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XDefaultRootWindow.restype = ulong
        x.XDefaultRootWindow.argtypes = [void]
        x.XInternAtom.restype = ulong
        x.XInternAtom.argtypes = [void, ctypes.c_char_p, ctypes.c_int]
        x.XCreateSimpleWindow.restype = ulong
        x.XCreateSimpleWindow.argtypes = [void, ulong] + [ctypes.c_int] * 2 + [ctypes.c_uint] * 3 + [ulong] * 2
        x.XDestroyWindow.argtypes = [void, ulong]
        x.XSetSelectionOwner.argtypes = [void, ulong, ulong, ulong]
        x.XChangeProperty.argtypes = [void, ulong, ulong, ulong, ctypes.c_int, ctypes.c_int, void, ctypes.c_int]
        x.XGetWindowProperty.argtypes = [
            void, ulong, ulong, ctypes.c_long, ctypes.c_long, ctypes.c_int, ulong,
            ctypes.POINTER(ulong), ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ulong),
            ctypes.POINTER(ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        x.XSendEvent.argtypes = [void, ulong, ctypes.c_int, ctypes.c_long, void]
        x.XNextEvent.argtypes = [void, void]
        x.XPending.argtypes = [void]
        x.XFlush.argtypes = [void]
        x.XConnectionNumber.argtypes = [void]
        x.XFree.argtypes = [void]
        x.Xutf8TextListToTextProperty.argtypes = [
            void, ctypes.POINTER(ctypes.c_char_p), ctypes.c_int, ctypes.c_int, ctypes.POINTER(TextProperty),
        ]
        x.XCloseDisplay.argtypes = [void]
        x.XSetErrorHandler.argtypes = [X_ERROR_HANDLER]
        x.XSetErrorHandler.restype = void
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [void, ulong]
        x.XkbSetDetectableAutoRepeat.argtypes = [void, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        x.XGrabKey.argtypes = [
            void, ctypes.c_int, ctypes.c_uint, ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        x.XUngrabKey.argtypes = [void, ctypes.c_int, ctypes.c_uint, ulong]
        x.XGetSelectionOwner.restype = ulong
        x.XGetSelectionOwner.argtypes = [void, ulong]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ulong)
        x.XGetKeyboardMapping.argtypes = [void, ctypes.c_ubyte, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]

    def _on_x_error(self, _display, _error):
        return 0

    def _atom(self, name):
        return self._x.XInternAtom(self._display, name.encode(), 0)

    def _new_window(self):
        return self._x.XCreateSimpleWindow(self._display, self._root, 0, 0, 1, 1, 0, 0, 0)

    def _registered_servers(self):
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        items = ctypes.c_ulong()
        remaining = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        self._x.XGetWindowProperty(
            self._display, self._root, self._servers, 0, 1024, 0, XA_ATOM,
            ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(items),
            ctypes.byref(remaining), ctypes.byref(data),
        )
        if not data:
            return []
        atoms = list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[:items.value])
        self._x.XFree(data)
        return atoms

    def _write_servers(self, atoms):
        data = (ctypes.c_ulong * len(atoms))(*atoms)
        self._x.XChangeProperty(
            self._display, self._root, self._servers, XA_ATOM, 32, PROP_MODE_REPLACE, data, len(atoms)
        )

    def _register(self, server_name):
        self._selection = self._atom(f"@server={server_name}")
        self._servers = self._atom("XIM_SERVERS")
        self._x.XSetSelectionOwner(self._display, self._selection, self._window, 0)
        others = [atom for atom in self._registered_servers() if atom != self._selection]
        self._write_servers([self._selection] + others)
        self._x.XFlush(self._display)

    def fileno(self):
        return self._x.XConnectionNumber(self._display)

    def close(self):
        if self._display:
            # Another server under the same name may have taken over since; its
            # registration is then left alone.
            if self._x.XGetSelectionOwner(self._display, self._selection) == self._window:
                others = [atom for atom in self._registered_servers() if atom != self._selection]
                self._write_servers(others)
            self._x.XCloseDisplay(self._display)
            self._display = None

    def process_events(self):
        """Handle everything waiting on the X connection."""
        event = ctypes.create_string_buffer(EVENT_SIZE)
        while self._x.XPending(self._display):
            self._x.XNextEvent(self._display, event)
            kind = struct.unpack_from("<i", event.raw, 0)[0]
            if kind == CLIENT_MESSAGE:
                self._on_client_message(event.raw)
            elif kind == SELECTION_REQUEST:
                self._on_selection_request(event.raw)
            elif kind in (KEY_PRESS, KEY_PRESS + 1) and self._grabbed:
                self._on_grabbed_event(event.raw, kind == KEY_PRESS)
        self._x.XFlush(self._display)

    def _on_grabbed_event(self, raw, pressed):
        """Handle a key that came here by the toggle key's grab. While a toggle key
        is held, every other key comes this way too, and belongs to the player."""
        state, keycode = struct.unpack_from("<II", raw, KEY_EVENT_STATE_OFFSET)
        if not self._is_toggle_key(keycode, state):
            self.on_grabbed_key(keycode, state, pressed)
        elif not pressed:
            self._toggle_down.discard(keycode)
            self.on_toggle_released()
        elif keycode in self._toggle_down:
            return  # held down, repeating: one toggle per press
        else:
            self._toggle_down.add(keycode)
            self._toggle_keycode = keycode
            now = time.monotonic()
            if now - self._last_toggle > self._toggle_debounce:
                self._last_toggle = now
                self.on_toggle()

    def _is_toggle_key(self, keycode, state):
        """Space is a toggle key only with Shift: typed plain while another toggle
        key is down, it is the player's. The key that toggled stays one whatever is
        held, so that it does not start typing once Shift is let go before it."""
        if keycode not in self._grabbed:
            return False
        needed = self._grabbed[keycode]
        return keycode == self._toggle_keycode or state & needed == needed

    def _on_selection_request(self, raw):
        requestor, selection, target, prop, when = struct.unpack_from("<QQQQQ", raw, 40)
        if target == self._atom_locales:
            text = LOCALES
        elif target == self._atom_transport:
            text = TRANSPORTS
        else:
            text = None
        if text is None:
            prop = 0
        else:
            encoded = text.encode()
            buffer = ctypes.create_string_buffer(encoded)
            self._x.XChangeProperty(
                self._display, requestor, prop, target, 8, PROP_MODE_REPLACE, buffer, len(encoded)
            )
        reply = ctypes.create_string_buffer(EVENT_SIZE)
        struct.pack_into("<i", reply, 0, 31)  # SelectionNotify
        struct.pack_into("<QQQQQ", reply, 32, requestor, selection, target, prop, when)
        self._x.XSendEvent(self._display, requestor, 0, 0, reply)

    def _on_client_message(self, raw):
        window, message_type = struct.unpack_from("<QQ", raw, OFFSET_WINDOW)
        data_format = struct.unpack_from("<i", raw, OFFSET_FORMAT)[0]
        if message_type == self._atom_connect:
            client_window = struct.unpack_from("<Q", raw, OFFSET_DATA)[0]
            self._accept(client_window)
        elif message_type == self._atom_protocol:
            connection = self._connections.get(window)
            if connection is None:
                return
            if data_format == 8:
                packet = raw[OFFSET_DATA:OFFSET_DATA + CM_DATA_SIZE]
            else:
                length, prop = struct.unpack_from("<QQ", raw, OFFSET_DATA)
                packet = self._read_property(window, prop, length)
            self._on_packet(connection, packet)

    def _read_property(self, window, prop, length):
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        items = ctypes.c_ulong()
        remaining = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        self._x.XGetWindowProperty(
            self._display, window, prop, 0, (length + 3) // 4 + 1, 1, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format), ctypes.byref(items),
            ctypes.byref(remaining), ctypes.byref(data),
        )
        if not data:
            return b""
        packet = ctypes.string_at(data, min(items.value, length))
        self._x.XFree(data)
        return packet

    def _accept(self, client_window):
        server_window = self._new_window()
        self._connections[server_window] = Connection(client_window, server_window)
        reply = ctypes.create_string_buffer(EVENT_SIZE)
        struct.pack_into("<i", reply, 0, CLIENT_MESSAGE)
        struct.pack_into("<QQ", reply, OFFSET_WINDOW, client_window, self._atom_connect)
        struct.pack_into("<i", reply, OFFSET_FORMAT, 32)
        # Major 0, minor 2: short packets in a message, longer ones in a property.
        struct.pack_into("<QQQQ", reply, OFFSET_DATA, server_window, 0, 2, CM_DATA_SIZE)
        self._x.XSendEvent(self._display, client_window, 0, 0, reply)

    def _send(self, connection, major, payload=b"", minor=0):
        body = payload + _padding(len(payload))
        packet = struct.pack("<BBH", major, minor, len(body) // 4) + body
        event = ctypes.create_string_buffer(EVENT_SIZE)
        struct.pack_into("<i", event, 0, CLIENT_MESSAGE)
        struct.pack_into("<QQ", event, OFFSET_WINDOW, connection.client_window, self._atom_protocol)
        if len(packet) <= CM_DATA_SIZE:
            struct.pack_into("<i", event, OFFSET_FORMAT, 8)
            event[OFFSET_DATA:OFFSET_DATA + len(packet)] = packet
        else:
            self._next_property += 1
            prop = self._atom(f"_XIM_SERVER_{self._next_property % 64}")
            buffer = ctypes.create_string_buffer(packet)
            self._x.XChangeProperty(
                self._display, connection.client_window, prop, XA_STRING, 8,
                PROP_MODE_REPLACE, buffer, len(packet),
            )
            struct.pack_into("<i", event, OFFSET_FORMAT, 32)
            struct.pack_into("<QQ", event, OFFSET_DATA, len(packet), prop)
        self._x.XSendEvent(self._display, connection.client_window, 0, 0, event)

    def _on_packet(self, connection, packet):
        if len(packet) < 4:
            return
        major = packet[0]
        body = packet[4:]
        handler = self._HANDLERS.get(major)
        if handler:
            handler(self, connection, body)
        self._x.XFlush(self._display)

    def _on_connect(self, connection, _body):
        self._send(connection, XIM_CONNECT_REPLY, struct.pack("<HH", 1, 0))

    def _on_disconnect(self, connection, _body):
        self._send(connection, XIM_DISCONNECT_REPLY)
        if self._focus and self._focus[0] is connection:
            self._set_focus(None)
        self._contexts = {c for c in self._contexts if c[0] is not connection}
        del self._connections[connection.server_window]
        self._x.XDestroyWindow(self._display, connection.server_window)

    def _on_open(self, connection, _body):
        im_id = self._next_im
        self._next_im += 1
        im_attributes = _attribute_list(IM_ATTRIBUTES)
        ic_attributes = _attribute_list(IC_ATTRIBUTES)
        self._send(
            connection,
            XIM_OPEN_REPLY,
            struct.pack("<HH", im_id, len(im_attributes)) + im_attributes
            + struct.pack("<HH", len(ic_attributes), 0) + ic_attributes,
        )

    def _on_close(self, connection, body):
        self._send(connection, XIM_CLOSE_REPLY, body[:2] + b"\0\0")

    def _on_encoding(self, connection, body):
        im_id, list_length = struct.unpack_from("<HH", body)
        names = []
        position = 4
        while position < 4 + list_length:
            size = body[position]
            names.append(body[position + 1:position + 1 + size])
            position += 1 + size
        index = names.index(b"COMPOUND_TEXT") if b"COMPOUND_TEXT" in names else -1
        self._send(
            connection, XIM_ENCODING_NEGOTIATION_REPLY,
            struct.pack("<HHhH", im_id, 0, index, 0),
        )

    def _on_query_extension(self, connection, body):
        self._send(connection, XIM_QUERY_EXTENSION_REPLY, body[:2] + struct.pack("<H", 0))

    def _on_get_im_values(self, connection, body):
        im_id = struct.unpack_from("<H", body)[0]
        styles = struct.pack("<HH", len(INPUT_STYLES), 0) + struct.pack(f"<{len(INPUT_STYLES)}I", *INPUT_STYLES)
        attribute = struct.pack("<HH", 0, len(styles)) + styles
        self._send(
            connection, XIM_GET_IM_VALUES_REPLY,
            struct.pack("<HH", im_id, len(attribute)) + attribute,
        )

    def _on_create_ic(self, connection, body):
        im_id = struct.unpack_from("<H", body)[0]
        ic_id = self._next_ic
        self._next_ic += 1
        self._send(connection, XIM_CREATE_IC_REPLY, struct.pack("<HH", im_id, ic_id))
        context = (connection, im_id, ic_id)
        self._contexts.add(context)
        self._send_event_mask(context)

    def _on_destroy_ic(self, connection, body):
        im_id, ic_id = struct.unpack_from("<HH", body)
        if self._focus == (connection, im_id, ic_id):
            self._set_focus(None)
        self._contexts.discard((connection, im_id, ic_id))
        self._send(connection, XIM_DESTROY_IC_REPLY, struct.pack("<HH", im_id, ic_id))

    def _on_set_ic_values(self, connection, body):
        self._send(connection, XIM_SET_IC_VALUES_REPLY, body[:4])

    def _on_get_ic_values(self, connection, body):
        im_id, ic_id, list_length = struct.unpack_from("<HHH", body)
        values = b""
        for position in range(0, list_length, 2):
            attribute = struct.unpack_from("<H", body, 6 + position)[0]
            if attribute == ATTRIBUTE_FILTER_EVENTS:
                values += struct.pack("<HHI", attribute, 4, 0)
        self._send(
            connection, XIM_GET_IC_VALUES_REPLY,
            struct.pack("<HHHH", im_id, ic_id, len(values), 0) + values,
        )

    def _on_set_focus(self, connection, body):
        self._set_focus((connection, *struct.unpack_from("<HH", body)))

    def _on_unset_focus(self, connection, body):
        if self._focus == (connection, *struct.unpack_from("<HH", body)):
            self._set_focus(None)

    def _set_focus(self, focus):
        self._focus = focus
        self._preedit_on = False
        self._preedit_length = 0
        self.on_focus(focus is not None)

    def _send_event_mask(self, context):
        """Ask for keys only in Hangul mode. Otherwise they reach the client with no
        round trip here, which is what keeps English input and game controls fast."""
        connection, im_id, ic_id = context
        mask = KEY_EVENT_MASKS if self._forward_keys else 0
        self._send(connection, XIM_SET_EVENT_MASK, struct.pack("<HHII", im_id, ic_id, mask, 0))

    def set_forward_keys(self, forward):
        self._forward_keys = forward
        for context in self._contexts:
            self._send_event_mask(context)
        self._x.XFlush(self._display)

    def _on_forward_event(self, connection, body):
        im_id, ic_id, flag, serial = struct.unpack_from("<HHHH", body)
        if flag & FORWARD_SYNCHRONOUS:
            self._send(connection, XIM_SYNC_REPLY, body[:4])
        self.on_key((connection, im_id, ic_id), serial, body[8:40])

    def lookup_keysym(self, keycode, state):
        """Return the keysym of a key as the server maps it right now.

        The Steam keyboard types any character by mapping it to a key just before
        pressing it, so the mapping is asked for each time rather than cached.
        """
        count = ctypes.c_int()
        symbols = self._x.XGetKeyboardMapping(self._display, keycode, 1, ctypes.byref(count))
        if not symbols:
            return 0
        try:
            levels = [symbols[i] for i in range(count.value)]
        finally:
            self._x.XFree(symbols)
        if state & SHIFT_MASK and len(levels) > 1 and levels[1]:
            return levels[1]
        return levels[0] if levels else 0

    def send_key_back(self, context, serial, event):
        """Return a key to the client, to handle as if it had not been forwarded."""
        connection, im_id, ic_id = context
        self._send(connection, XIM_FORWARD_EVENT, struct.pack("<HHHH", im_id, ic_id, 0, serial) + event)
        self._x.XFlush(self._display)

    def grab_toggle_keys(self, names=tuple(TOGGLE_KEYS)):
        """Hold the toggle keys with the names given, so they reach this rather than
        the game."""
        if self._grabbed:
            return
        for name in names:
            if name not in TOGGLE_KEYS:
                continue
            keysym, modifiers = TOGGLE_KEYS[name]
            keycode = self._x.XKeysymToKeycode(self._display, keysym)
            if not keycode:
                continue
            for lock in LOCK_MODIFIERS:
                self._x.XGrabKey(
                    self._display, keycode, modifiers | lock, self._root, 0,
                    GRAB_MODE_ASYNC, GRAB_MODE_ASYNC,
                )
            self._grabbed[keycode] = modifiers
        self._x.XFlush(self._display)

    def ungrab_toggle_keys(self):
        for keycode in self._grabbed:
            self._x.XUngrabKey(self._display, keycode, ANY_MODIFIER, self._root)
        self._grabbed = {}
        self._toggle_keycode = None
        self._toggle_down = set()
        self._x.XFlush(self._display)

    def _on_sync(self, connection, body):
        self._send(connection, XIM_SYNC_REPLY, body[:4])

    def _on_reset_ic(self, connection, body):
        # The caller keeps composing, so the client gets nothing back to commit.
        im_id, ic_id = struct.unpack_from("<HH", body)
        self._preedit_on = False
        self._preedit_length = 0
        self._send(connection, XIM_RESET_IC_REPLY, struct.pack("<HHHH", im_id, ic_id, 0, 0))
        self.on_reset()

    def _encode(self, text):
        """Encode text as COMPOUND_TEXT, the encoding negotiated with the client."""
        pointer = ctypes.c_char_p(text.encode())
        prop = TextProperty()
        self._x.Xutf8TextListToTextProperty(
            self._display, ctypes.byref(pointer), 1, COMPOUND_TEXT_STYLE, ctypes.byref(prop)
        )
        encoded = ctypes.string_at(prop.value, prop.nitems)
        self._x.XFree(prop.value)
        return encoded

    def commit(self, text, backspaces=0):
        """Send finished text to the focused client. Returns False if there is none.

        Backspaces go first, as Backspace characters. Xlib leaves control characters
        out when it encodes text, but the client keeps them at the start, before the
        text switches to another character set, so they are put there directly.
        """
        if not self._focus:
            return False
        connection, im_id, ic_id = self._focus
        self._end_preedit()
        encoded = b"\x08" * backspaces + (self._encode(text) if text else b"")
        self._send(
            connection, XIM_COMMIT,
            struct.pack("<HHHH", im_id, ic_id, COMMIT_LOOKUP_CHARS, len(encoded)) + encoded,
        )
        self._x.XFlush(self._display)
        return True

    def preedit(self, text):
        """Show text as the composition in the focused client, or clear it if empty."""
        if not self._focus:
            return False
        connection, im_id, ic_id = self._focus
        if not text:
            self._end_preedit()
        else:
            if not self._preedit_on:
                self._send(connection, XIM_PREEDIT_START, struct.pack("<HH", im_id, ic_id))
                self._preedit_on = True
            encoded = self._encode(text)
            draw = struct.pack(
                "<HHiiiIH", im_id, ic_id, len(text), 0, self._preedit_length,
                PREEDIT_STATUS_NO_FEEDBACK, len(encoded),
            ) + encoded
            draw += _padding(len(draw) - 18 + 2) + struct.pack("<HH", 0, 0)
            self._send(connection, XIM_PREEDIT_DRAW, draw)
            self._preedit_length = len(text)
        self._x.XFlush(self._display)
        return True

    def _end_preedit(self):
        if not self._preedit_on:
            return
        connection, im_id, ic_id = self._focus
        if self._preedit_length:
            draw = struct.pack(
                "<HHiiiIH", im_id, ic_id, 0, 0, self._preedit_length, 0x0003, 0
            ) + _padding(2) + struct.pack("<HH", 0, 0)
            self._send(connection, XIM_PREEDIT_DRAW, draw)
        self._send(connection, XIM_PREEDIT_DONE, struct.pack("<HH", im_id, ic_id))
        self._preedit_on = False
        self._preedit_length = 0

    _HANDLERS = {
        XIM_CONNECT: _on_connect,
        XIM_DISCONNECT: _on_disconnect,
        XIM_OPEN: _on_open,
        XIM_CLOSE: _on_close,
        XIM_ENCODING_NEGOTIATION: _on_encoding,
        XIM_QUERY_EXTENSION: _on_query_extension,
        XIM_GET_IM_VALUES: _on_get_im_values,
        XIM_CREATE_IC: _on_create_ic,
        XIM_DESTROY_IC: _on_destroy_ic,
        XIM_SET_IC_VALUES: _on_set_ic_values,
        XIM_GET_IC_VALUES: _on_get_ic_values,
        XIM_FORWARD_EVENT: _on_forward_event,
        XIM_SET_IC_FOCUS: _on_set_focus,
        XIM_UNSET_IC_FOCUS: _on_unset_focus,
        XIM_SYNC: _on_sync,
        XIM_RESET_IC: _on_reset_ic,
    }
