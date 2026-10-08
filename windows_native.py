"""The Windows half of JARVIS's hands and eyes, through `ctypes` alone.

On macOS these jobs are done by `osascript`, `screencapture`, `sips` and
`ps`. Windows has no equivalent command line tools worth spawning, but it has
stable Win32 APIs for every one of them, and `ctypes` (standard library)
reaches those in-process — so this module costs no dependency, and most of
it costs no subprocess either.

* `pid_alive` — OpenProcess + WaitForSingleObject. Never `os.kill`, which on
  Windows terminates the process (see platform_support.py).
* `list_windows` — EnumWindows over the visible, titled, top-level windows,
  in Z order, with the owning process's executable name as the "app".
* `capture_display` — GDI StretchBlt of one monitor straight down to the
  size the brain is shown, read back with GetDIBits and encoded as PNG with
  `zlib`. No temp file ever exists.
* `console_id` / `press_key` — the `answer_dialog` path. A session's pid is
  attached to a console, and AttachConsole(pid) + WriteConsoleInput puts a
  key event into THAT console's input buffer: exact pid -> console identity,
  no window focus, no foreground stealing, and no "type into whatever is in
  front" failure mode at all. AttachConsole detaches the caller from its own
  console first, so those two run in a short-lived helper process (this file,
  run as a script) and never inside the server.

Importable on any platform: nothing touches `ctypes.WinDLL` until a function
that needs it is called, so the pure parts (`encode_png`, `sample_pixels`)
are tested everywhere.
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib

# ── pure helpers (any platform) ───────────────────────────────────────────


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """An 8-bit RGB PNG from tightly packed RGB rows. Standard library only."""
    stride = width * 3
    if len(rgb) < stride * height:
        raise ValueError("pixel buffer is shorter than width*height*3")
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride]
                   for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def bgra_to_rgb(bgra: bytes) -> bytes:
    """GDI's 32-bit BGRA rows to packed RGB, by slice assignment (fast)."""
    n = len(bgra) // 4
    rgb = bytearray(n * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return bytes(rgb)


def sample_pixels(width: int, height: int, rgb: bytes,
                  edge: int = 32) -> list[tuple[int, int, int]]:
    """An evenly spaced edge x edge grid of pixels — what `screen._is_blank`
    judges, the same job `sips` does for it on macOS."""
    if width <= 0 or height <= 0:
        return []
    out = []
    for j in range(edge):
        y = min(height - 1, (j * height) // edge)
        for i in range(edge):
            x = min(width - 1, (i * width) // edge)
            k = (y * width + x) * 3
            out.append((rgb[k], rgb[k + 1], rgb[k + 2]))
    return out


# ── Win32 plumbing (Windows only, resolved lazily) ────────────────────────

_dlls: dict = {}


def _dll(name: str):
    if name not in _dlls:
        _dlls[name] = ctypes.WinDLL(name, use_last_error=True)  # type: ignore[attr-defined]
    return _dlls[name]


def _wt():
    from ctypes import wintypes
    return wintypes


# ── is a pid alive ────────────────────────────────────────────────────────

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_TIMEOUT = 0x102


def pid_alive(pid: int) -> bool:
    """True if `pid` is a running process. Opens a query handle; never kills."""
    w = _wt()
    k32 = _dll("kernel32")
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.OpenProcess.restype = w.HANDLE
    k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k32.WaitForSingleObject.restype = w.DWORD
    k32.CloseHandle.argtypes = [w.HANDLE]
    h = k32.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                        False, int(pid))
    if not h:
        return False
    try:
        # Signalled means exited; a handle can outlive its process.
        return k32.WaitForSingleObject(h, 0) == _WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


def _process_name(pid: int) -> str:
    """The executable's stem ("chrome", "Code", "WindowsTerminal"), or ""."""
    w = _wt()
    k32 = _dll("kernel32")
    k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k32.OpenProcess.restype = w.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [
        w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k32.QueryFullProcessImageNameW.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        name = buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
        return name[:-4] if name.lower().endswith(".exe") else name
    finally:
        k32.CloseHandle(h)


# ── the window list ───────────────────────────────────────────────────────

_GW_OWNER = 4
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_CLOAKED = 14
_GA_ROOTOWNER = 3
# The desktop itself and the taskbar are windows too; they are not "open".
_NOT_WINDOWS = {"Program Manager"}


def list_windows() -> list[tuple[str, str, bool]]:
    """(app, title, frontmost) for every visible, titled top-level window,
    front to back. Raises OSError if the enumeration itself fails."""
    w = _wt()
    user32 = _dll("user32")
    try:
        dwm = _dll("dwmapi")
    except OSError:
        dwm = None
    if dwm is not None:
        dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p,
                                              w.DWORD]

    get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    get_long.argtypes = [w.HWND, ctypes.c_int]
    get_long.restype = ctypes.c_ssize_t
    user32.IsWindowVisible.argtypes = [w.HWND]
    user32.GetWindow.argtypes = [w.HWND, w.UINT]
    user32.GetWindow.restype = w.HWND
    user32.GetWindowTextLengthW.argtypes = [w.HWND]
    user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user32.GetForegroundWindow.restype = w.HWND
    user32.GetAncestor.argtypes = [w.HWND, w.UINT]
    user32.GetAncestor.restype = w.HWND

    fg = user32.GetForegroundWindow()
    front = user32.GetAncestor(fg, _GA_ROOTOWNER) if fg else None
    found: list[tuple[str, str, bool]] = []
    names: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def _each(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindow(hwnd, _GW_OWNER):
                return True                       # a dialog's owner lists it
            if get_long(hwnd, _GWL_EXSTYLE) & _WS_EX_TOOLWINDOW:
                return True
            if dwm is not None:
                cloaked = w.DWORD(0)
                if dwm.DwmGetWindowAttribute(
                        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked),
                        ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                    return True                   # on another virtual desktop
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title or title in _NOT_WINDOWS:
                return True
            pid = w.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in names:
                names[pid.value] = _process_name(pid.value) or "unknown"
            found.append((names[pid.value], title,
                          bool(front) and hwnd == front))
        except Exception:
            pass                                  # one odd window costs itself
        return True

    if not user32.EnumWindows(_each, 0):
        raise OSError(ctypes.get_last_error(), "EnumWindows failed")
    return found


# ── the picture ───────────────────────────────────────────────────────────

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000
_HALFTONE = 4
_DIB_RGB_COLORS = 0
_MONITORINFOF_PRIMARY = 1
_dpi_set = False


def _make_dpi_aware() -> None:
    """Without this a scaled display (125%, 150%) reports its logical size
    and the capture is a cropped corner of the screen. Once per process."""
    global _dpi_set
    if _dpi_set:
        return
    _dpi_set = True
    try:
        _dll("shcore").SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            _dll("user32").SetProcessDPIAware()
        except Exception:
            pass


def monitors() -> list[tuple[int, int, int, int]]:
    """(left, top, width, height) per display, the primary one first — so
    display 1 is the main display, as `screencapture -D 1` numbers it."""
    w = _wt()
    user32 = _dll("user32")
    _make_dpi_aware()

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT),
                    ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

    user32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MONITORINFO)]
    out: list[tuple[bool, tuple[int, int, int, int]]] = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    def _each(hmon, _hdc, _rect, _lparam):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            out.append((bool(info.dwFlags & _MONITORINFOF_PRIMARY),
                        (r.left, r.top, r.right - r.left, r.bottom - r.top)))
        return True

    user32.EnumDisplayMonitors(None, None, _each, 0)
    out.sort(key=lambda m: not m[0])     # stable: primary first, rest in order
    return [rect for _primary, rect in out]


def capture_display(display: int | None, max_edge: int
                    ) -> tuple[int, int, bytes]:
    """(width, height, packed RGB) of one display, scaled so its longest edge
    is at most `max_edge`. `display` is 1-based; None is the primary.

    Raises LookupError for a display that does not exist and OSError when
    GDI refuses (a locked workstation, a secure desktop).
    """
    w = _wt()
    user32 = _dll("user32")
    gdi32 = _dll("gdi32")
    screens = monitors()
    if not screens:
        raise OSError("no displays found")
    index = (display or 1) - 1
    if index < 0 or index >= len(screens):
        raise LookupError(f"there is no display {display}")
    left, top, width, height = screens[index]
    scale = min(1.0, max_edge / float(max(width, height)))
    dw, dh = max(1, round(width * scale)), max(1, round(height * scale))

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                    ("biPlanes", w.WORD), ("biBitCount", w.WORD),
                    ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                    ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG),
                    ("biClrUsed", w.DWORD), ("biClrImportant", w.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]

    user32.GetDC.argtypes = [w.HWND]
    user32.GetDC.restype = w.HDC
    user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
    gdi32.CreateCompatibleDC.argtypes = [w.HDC]
    gdi32.CreateCompatibleDC.restype = w.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = w.HBITMAP
    gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
    gdi32.SelectObject.restype = w.HGDIOBJ
    gdi32.SetStretchBltMode.argtypes = [w.HDC, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    gdi32.StretchBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, w.HDC, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, w.DWORD]
    gdi32.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFO), w.UINT]
    gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [w.HDC]

    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise OSError("GetDC failed")
    mem_dc = bitmap = None
    try:
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, dw, dh)
        if not mem_dc or not bitmap:
            raise OSError("could not allocate a bitmap")
        previous = gdi32.SelectObject(mem_dc, bitmap)
        gdi32.SetStretchBltMode(mem_dc, _HALFTONE)
        gdi32.SetBrushOrgEx(mem_dc, 0, 0, None)
        ok = gdi32.StretchBlt(mem_dc, 0, 0, dw, dh, screen_dc, left, top,
                              width, height, _SRCCOPY | _CAPTUREBLT)
        gdi32.SelectObject(mem_dc, previous)   # GetDIBits wants it deselected
        if not ok:
            raise OSError(ctypes.get_last_error(), "StretchBlt failed")
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = dw
        info.bmiHeader.biHeight = -dh          # negative: top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(dw * dh * 4)
        rows = gdi32.GetDIBits(mem_dc, bitmap, 0, dh, buf, ctypes.byref(info),
                               _DIB_RGB_COLORS)
        if rows != dh:
            raise OSError("GetDIBits failed")
        return dw, dh, bgra_to_rgb(buf.raw)
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if mem_dc:
            gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


# ── one key into one console ──────────────────────────────────────────────
#
# Everything below runs in the helper process (`python windows_native.py
# ...`), never in the server: AttachConsole requires the caller to have no
# console of its own, and FreeConsole on the server would take its log
# output with it.

_KEY_EVENT = 0x0001
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_OPEN_EXISTING = 3
_INVALID_HANDLE = ctypes.c_void_p(-1).value

# The closed vocabulary, mirrored from dialog.normalize_key: Return, Escape,
# or one digit. Anything else is refused here too.
_KEYS = {"return": (0x0D, "\r"), "escape": (0x1B, "\x1b")}
_KEYS.update({d: (ord(d), d) for d in "123456789"})


def _attach(pid: int) -> bool:
    k32 = _dll("kernel32")
    k32.FreeConsole()
    return bool(k32.AttachConsole(ctypes.c_uint32(pid)))


def console_id(pid: int) -> int | None:
    """The console window handle `pid` is attached to, or None.

    Unique per console (a ConPTY pseudo-console inside Windows Terminal has
    one too), so two pids report the same id exactly when they share a
    console. HELPER PROCESS ONLY.
    """
    k32 = _dll("kernel32")
    k32.GetConsoleWindow.restype = ctypes.c_void_p
    if not _attach(pid):
        return None
    try:
        return k32.GetConsoleWindow() or None
    finally:
        k32.FreeConsole()


def press_key(pid: int, key: str, expected: int) -> str:
    """Write one key press into `pid`'s console. HELPER PROCESS ONLY.

    "ok", "moved" (the console is no longer the one that was read back to
    the user), "no_console", "bad_key" or "failed".
    """
    if key not in _KEYS:
        return "bad_key"
    w = _wt()
    k32 = _dll("kernel32")
    user32 = _dll("user32")
    k32.GetConsoleWindow.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                w.DWORD, w.DWORD, w.HANDLE]
    k32.CreateFileW.restype = w.HANDLE
    k32.CloseHandle.argtypes = [w.HANDLE]

    class KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [("bKeyDown", w.BOOL), ("wRepeatCount", w.WORD),
                    ("wVirtualKeyCode", w.WORD), ("wVirtualScanCode", w.WORD),
                    ("uChar", ctypes.c_wchar), ("dwControlKeyState", w.DWORD)]

    class _EVENT(ctypes.Union):
        _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]

    class INPUT_RECORD(ctypes.Structure):
        _fields_ = [("EventType", w.WORD), ("Event", _EVENT)]

    k32.WriteConsoleInputW.argtypes = [w.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                       w.DWORD, ctypes.POINTER(w.DWORD)]

    if not _attach(pid):
        return "no_console"
    try:
        # Re-checked at press time: the console that was read back to the
        # user is the only one this may type into.
        if (k32.GetConsoleWindow() or 0) != expected:
            return "moved"
        handle = k32.CreateFileW("CONIN$", _GENERIC_READ | _GENERIC_WRITE,
                                 _FILE_SHARE_READ | _FILE_SHARE_WRITE, None,
                                 _OPEN_EXISTING, 0, None)
        if not handle or handle == _INVALID_HANDLE:
            return "failed"
        try:
            vk, char = _KEYS[key]
            scan = user32.MapVirtualKeyW(vk, 0)
            records = (INPUT_RECORD * 2)()
            for i, down in enumerate((True, False)):
                records[i].EventType = _KEY_EVENT
                ev = records[i].Event.KeyEvent
                ev.bKeyDown = down
                ev.wRepeatCount = 1
                ev.wVirtualKeyCode = vk
                ev.wVirtualScanCode = scan
                ev.uChar = char
                ev.dwControlKeyState = 0
            written = w.DWORD(0)
            if not k32.WriteConsoleInputW(handle, records, 2, ctypes.byref(written)):
                return "failed"
            return "ok" if written.value == 2 else "failed"
        finally:
            k32.CloseHandle(handle)
    finally:
        k32.FreeConsole()


def _main(argv: list[str]) -> int:
    """`console <pid>` prints the id or nothing; `press <pid> <key> <id>`
    prints one outcome word. Exit 0 whenever an answer was printed."""
    try:
        if len(argv) == 2 and argv[0] == "console":
            found = console_id(int(argv[1]))
            print(found if found else "", flush=True)
            return 0
        if len(argv) == 4 and argv[0] == "press":
            print(press_key(int(argv[1]), argv[2], int(argv[3])), flush=True)
            return 0
    except Exception as e:                       # pragma: no cover - Windows only
        print(f"error: {e}", file=sys.stderr, flush=True)
        return 1
    print("usage: windows_native.py console PID | press PID KEY CONSOLE_ID",
          file=sys.stderr)
    return 2


if __name__ == "__main__":                       # pragma: no cover - Windows only
    sys.exit(_main(sys.argv[1:]))
