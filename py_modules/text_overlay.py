"""A line of text drawn over the game, for text the game does not show yet.

gamescope shows one window with GAMESCOPE_EXTERNAL_OVERLAY above the game, without
giving it focus, so keys keep going to the game. That is the slot the performance
overlay uses as well, which is hidden for as long as this window exists: it is made
when there is text to show and destroyed as soon as there is none.
"""

import ctypes

FONT = b"Noto Sans CJK KR"
FONT_SIZE = 28
BAR_HEIGHT = 56
MARGIN = 20
TEXT_BASELINE = 18
REDRAW_SECONDS = 0.1  # the first drawing can come before the window is on screen
CW_BACK_PIXEL, CW_BORDER_PIXEL, CW_OVERRIDE_REDIRECT, CW_COLORMAP = 1 << 1, 1 << 3, 1 << 9, 1 << 13
XA_CARDINAL, PROP_MODE_REPLACE, INPUT_OUTPUT, TRUE_COLOR = 6, 0, 1, 4
OPERATOR_CLEAR, OPERATOR_OVER = 0, 2

Pointer, Ulong, Double = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_double


class VisualInfo(ctypes.Structure):
    _fields_ = [
        ("visual", Pointer), ("visualid", Ulong), ("screen", ctypes.c_int), ("depth", ctypes.c_int),
        ("c_class", ctypes.c_int), ("red_mask", Ulong), ("green_mask", Ulong), ("blue_mask", Ulong),
        ("colormap_size", ctypes.c_int), ("bits_per_rgb", ctypes.c_int),
    ]


class WindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", Ulong), ("background_pixel", Ulong), ("border_pixmap", Ulong),
        ("border_pixel", Ulong), ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int), ("backing_planes", Ulong), ("backing_pixel", Ulong),
        ("save_under", ctypes.c_int), ("event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long), ("override_redirect", ctypes.c_int),
        ("colormap", Ulong), ("cursor", Ulong),
    ]


class TextExtents(ctypes.Structure):
    _fields_ = [(name, Double) for name in ("x_bearing", "y_bearing", "width", "height", "x_advance", "y_advance")]


def _declare(library, signatures):
    for name, (result, arguments) in signatures.items():
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments


class TextOverlay:
    """Raises OSError if this display cannot show one.

    timers  has call_later(seconds, function), returning a timer with cancel()
    """

    def __init__(self, display_name, timers):
        self.timers = timers
        self.text = ""
        self.window = None
        self.surface = self.cairo = None
        self.redraw_timer = None
        self._x = ctypes.CDLL("libX11.so.6")
        self._c = ctypes.CDLL("libcairo.so.2")
        x, c, integer, string = self._x, self._c, ctypes.c_int, ctypes.c_char_p
        _declare(x, {
            "XOpenDisplay": (Pointer, [string]),
            "XDefaultScreen": (integer, [Pointer]),
            "XRootWindow": (Ulong, [Pointer, integer]),
            "XDisplayWidth": (integer, [Pointer, integer]),
            "XDisplayHeight": (integer, [Pointer, integer]),
            "XMatchVisualInfo": (integer, [Pointer, integer, integer, integer, ctypes.POINTER(VisualInfo)]),
            "XCreateColormap": (Ulong, [Pointer, Ulong, Pointer, integer]),
            "XCreateWindow": (Ulong, [
                Pointer, Ulong, integer, integer, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, integer,
                ctypes.c_uint, Pointer, Ulong, ctypes.POINTER(WindowAttributes),
            ]),
            "XInternAtom": (Ulong, [Pointer, string, integer]),
            "XChangeProperty": (integer, [Pointer, Ulong, Ulong, Ulong, integer, integer, Pointer, integer]),
            "XMapWindow": (integer, [Pointer, Ulong]),
            "XDestroyWindow": (integer, [Pointer, Ulong]),
            "XFreeColormap": (integer, [Pointer, Ulong]),
            "XFlush": (integer, [Pointer]),
            "XSync": (integer, [Pointer, integer]),
            "XCloseDisplay": (integer, [Pointer]),
        })
        _declare(c, {
            "cairo_xlib_surface_create": (Pointer, [Pointer, Ulong, Pointer, integer, integer]),
            "cairo_create": (Pointer, [Pointer]),
            "cairo_destroy": (None, [Pointer]),
            "cairo_surface_destroy": (None, [Pointer]),
            "cairo_surface_flush": (None, [Pointer]),
            "cairo_set_operator": (None, [Pointer, integer]),
            "cairo_set_source_rgba": (None, [Pointer, Double, Double, Double, Double]),
            "cairo_paint": (None, [Pointer]),
            "cairo_rectangle": (None, [Pointer, Double, Double, Double, Double]),
            "cairo_fill": (None, [Pointer]),
            "cairo_select_font_face": (None, [Pointer, string, integer, integer]),
            "cairo_set_font_size": (None, [Pointer, Double]),
            "cairo_text_extents": (None, [Pointer, string, ctypes.POINTER(TextExtents)]),
            "cairo_move_to": (None, [Pointer, Double, Double]),
            "cairo_show_text": (None, [Pointer, string]),
        })
        self._display = x.XOpenDisplay(display_name.encode())
        if not self._display:
            raise OSError(f"cannot open display {display_name}")
        screen = x.XDefaultScreen(self._display)
        self._root = x.XRootWindow(self._display, screen)
        self.width = x.XDisplayWidth(self._display, screen)
        self.height = x.XDisplayHeight(self._display, screen)
        info = VisualInfo()
        if not x.XMatchVisualInfo(self._display, screen, 32, TRUE_COLOR, ctypes.byref(info)):
            x.XCloseDisplay(self._display)
            raise OSError("no visual with transparency")
        self._visual = info.visual
        self._colormap = x.XCreateColormap(self._display, self._root, self._visual, 0)
        self._atom = x.XInternAtom(self._display, b"GAMESCOPE_EXTERNAL_OVERLAY", 0)

    def show(self, text):
        self.text = text
        if not text:
            self._destroy_window()
            return
        if self.window is None:
            self._create_window()
            self.redraw_timer = self.timers.call_later(REDRAW_SECONDS, self._redraw)
        self._draw()

    def _redraw(self):
        self.redraw_timer = None
        if self.window is not None:
            self._draw()

    def _create_window(self):
        x = self._x
        attributes = WindowAttributes(override_redirect=1, colormap=self._colormap)
        mask = CW_BACK_PIXEL | CW_BORDER_PIXEL | CW_OVERRIDE_REDIRECT | CW_COLORMAP
        self.window = x.XCreateWindow(
            self._display, self._root, 0, 0, self.width, self.height, 0, 32, INPUT_OUTPUT,
            self._visual, mask, ctypes.byref(attributes),
        )
        one = ctypes.c_long(1)
        x.XChangeProperty(
            self._display, self.window, self._atom, XA_CARDINAL, 32, PROP_MODE_REPLACE, ctypes.byref(one), 1,
        )
        x.XMapWindow(self._display, self.window)
        x.XSync(self._display, 0)
        self.surface = self._c.cairo_xlib_surface_create(
            self._display, self.window, self._visual, self.width, self.height,
        )
        self.cairo = self._c.cairo_create(self.surface)
        self._c.cairo_select_font_face(self.cairo, FONT, 0, 0)
        self._c.cairo_set_font_size(self.cairo, FONT_SIZE)

    def _end_that_fits(self, text):
        """The end of the text, which is where the typing is."""
        room = self.width - 2 * MARGIN
        while len(text) > 1:
            extents = TextExtents()
            self._c.cairo_text_extents(self.cairo, text.encode(), ctypes.byref(extents))
            if extents.x_advance <= room:
                break
            text = text[1:]
        return text

    def _draw(self):
        c, cairo = self._c, self.cairo
        c.cairo_set_operator(cairo, OPERATOR_CLEAR)
        c.cairo_paint(cairo)
        c.cairo_set_operator(cairo, OPERATOR_OVER)
        c.cairo_set_source_rgba(cairo, 0, 0, 0, 0.8)
        c.cairo_rectangle(cairo, 0, self.height - BAR_HEIGHT, self.width, BAR_HEIGHT)
        c.cairo_fill(cairo)
        c.cairo_set_source_rgba(cairo, 1, 1, 1, 1)
        c.cairo_move_to(cairo, MARGIN, self.height - TEXT_BASELINE)
        c.cairo_show_text(cairo, self._end_that_fits(self.text).encode())
        c.cairo_surface_flush(self.surface)
        self._x.XFlush(self._display)

    def _destroy_window(self):
        if self.redraw_timer:
            self.redraw_timer.cancel()
            self.redraw_timer = None
        if self.window is None:
            return
        self._c.cairo_destroy(self.cairo)
        self._c.cairo_surface_destroy(self.surface)
        self.cairo = self.surface = None
        self._x.XDestroyWindow(self._display, self.window)
        self.window = None
        self._x.XFlush(self._display)

    def close(self):
        self._destroy_window()
        self._x.XFreeColormap(self._display, self._colormap)
        self._x.XCloseDisplay(self._display)
