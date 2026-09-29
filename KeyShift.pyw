"""
KeyShift — fix text typed in the wrong keyboard layout.

Select text (or select nothing) and press the shortcut (default Ctrl+Shift+Space).
The text is re-mapped key-for-key between the keyboard layouts already installed
in Windows (e.g. "sghl" -> "سلام"), pasted back, and the keyboard is switched
to the right layout so you can keep typing.

- No mapping tables: every layout is read live from Windows (ToUnicodeEx).
- Nothing you type is stored. Only settings go to %APPDATA%\\KeyShift\\config.json.
- Pure Python standard library. Run:  pythonw KeyShift.pyw
"""

import ctypes
import json
import os
import queue
import sys
import threading
import time

APP = "KeyShift"
VERSION = "1.0"
WIN = sys.platform == "win32"

# ─────────────────────────────────────────────────────────────────────────────
#  Pure conversion logic (no Windows calls — testable anywhere)
# ─────────────────────────────────────────────────────────────────────────────


class Layout:
    """One installed keyboard layout: physical key (scancode, modifier) <-> text."""

    def __init__(self, hkl, key, code, name, sub, fwd):
        self.hkl, self.key, self.code, self.name, self.sub = hkl, key, code, name, sub
        self.fwd = fwd  # (scancode, mod) -> str      mod: 0 none, 1 shift, 2 altgr, 3 shift+altgr
        self.rev = {}  # str -> (scancode, mod)      prefers fewer modifiers
        for k, s in sorted(fwd.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            self.rev.setdefault(s, k)
        self.maxlen = max((len(s) for s in self.rev), default=1)


def exclusive_chars(layouts):
    """Characters only one of the given layouts can produce -> tells us who typed what."""
    sets = {L.key: {s for s in L.rev if len(s) == 1} for L in layouts}
    excl = {}
    for L in layouts:
        others = set().union(*(sets[o.key] for o in layouts if o is not L))
        excl[L.key] = {c for c in sets[L.key] if c not in others and not c.isspace()}
    return excl


def next_layout(layouts, src):
    return layouts[(layouts.index(src) + 1) % len(layouts)]


def detect(text, layouts, current_key=None):
    """(src, dst) for a selection: src = layout that best explains the text, dst = next one."""
    if len(layouts) < 2:
        return None
    excl = exclusive_chars(layouts)
    scores = [(sum(c in excl[L.key] for c in text), L) for L in layouts]
    top = max(s for s, _ in scores)
    if top == 0:
        return None
    cands = [L for s, L in scores if s == top]
    src = next((L for L in cands if L.key == current_key), cands[0])
    return src, next_layout(layouts, src)


def convert(text, src, dst):
    """Re-type `text` (typed on layout src) as if the same keys were pressed on dst."""
    out, i, n = [], 0, len(text)
    while i < n:
        for ln in range(min(src.maxlen, n - i), 0, -1):
            s = text[i:i + ln]
            k = src.rev.get(s)
            if k is not None:
                out.append(dst.fwd.get(k) or s)
                i += ln
                break
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def wrong_tail(line, layouts):
    """
    Nothing selected: find the end part of the line typed in the wrong layout.
    Walks back from the last layout-specific character until text from another
    layout starts. Returns (start_index, src, dst) or None.
    """
    if len(layouts) < 2:
        return None
    excl = exclusive_chars(layouts)
    owner = {c: L for L in layouts for c in excl[L.key]}
    i = len(line) - 1
    while i >= 0 and line[i] not in owner:
        i -= 1
    if i < 0:
        return None
    src = owner[line[i]]
    k = i
    while k > 0 and owner.get(line[k - 1], src) is src:
        k -= 1
    while line[k] not in excl[src.key]:  # skip leading neutral chars (spaces, shared punctuation)
        k += 1
    return k, src, next_layout(layouts, src)


def caret_len(s):
    return len(s.replace("\r\n", "\n"))


# ─────────────────────────────────────────────────────────────────────────────
#  Windows plumbing
# ─────────────────────────────────────────────────────────────────────────────

if WIN:
    import winreg
    from ctypes import wintypes as W

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    HANDLE = ctypes.c_void_p

    def _fn(dll, name, res, *args):
        f = getattr(dll, name)
        f.restype, f.argtypes = res, list(args)
        return f

    # keyboard / layouts
    GetKeyboardLayoutList = _fn(user32, "GetKeyboardLayoutList", ctypes.c_int, ctypes.c_int, ctypes.POINTER(W.HKL))
    GetKeyboardLayout = _fn(user32, "GetKeyboardLayout", W.HKL, W.DWORD)
    MapVirtualKeyExW = _fn(user32, "MapVirtualKeyExW", W.UINT, W.UINT, W.UINT, W.HKL)
    MapVirtualKeyW = _fn(user32, "MapVirtualKeyW", W.UINT, W.UINT, W.UINT)
    ToUnicodeEx = _fn(user32, "ToUnicodeEx", ctypes.c_int, W.UINT, W.UINT, ctypes.POINTER(ctypes.c_ubyte),
                      W.LPWSTR, ctypes.c_int, W.UINT, W.HKL)
    GetAsyncKeyState = _fn(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
    GetKeyNameTextW = _fn(user32, "GetKeyNameTextW", ctypes.c_int, W.LONG, W.LPWSTR, ctypes.c_int)
    RegisterHotKey = _fn(user32, "RegisterHotKey", W.BOOL, W.HWND, ctypes.c_int, W.UINT, W.UINT)
    UnregisterHotKey = _fn(user32, "UnregisterHotKey", W.BOOL, W.HWND, ctypes.c_int)
    LCIDToLocaleName = _fn(kernel32, "LCIDToLocaleName", ctypes.c_int, W.DWORD, W.LPWSTR, ctypes.c_int, W.DWORD)
    GetLocaleInfoEx = _fn(kernel32, "GetLocaleInfoEx", ctypes.c_int, W.LPCWSTR, W.DWORD, W.LPWSTR, ctypes.c_int)

    # windows / messages
    GetForegroundWindow = _fn(user32, "GetForegroundWindow", W.HWND)
    GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", W.DWORD, W.HWND, ctypes.POINTER(W.DWORD))
    GetClassNameW = _fn(user32, "GetClassNameW", ctypes.c_int, W.HWND, W.LPWSTR, ctypes.c_int)
    PostMessageW = _fn(user32, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    SendMessageW = _fn(user32, "SendMessageW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    DefWindowProcW = _fn(user32, "DefWindowProcW", LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    GetMessageW = _fn(user32, "GetMessageW", W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
    TranslateMessage = _fn(user32, "TranslateMessage", W.BOOL, ctypes.POINTER(W.MSG))
    DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(W.MSG))
    PostQuitMessage = _fn(user32, "PostQuitMessage", None, ctypes.c_int)
    DestroyWindow = _fn(user32, "DestroyWindow", W.BOOL, W.HWND)
    RegisterWindowMessageW = _fn(user32, "RegisterWindowMessageW", W.UINT, W.LPCWSTR)
    FindWindowW = _fn(user32, "FindWindowW", W.HWND, W.LPCWSTR, W.LPCWSTR)
    GetParent = _fn(user32, "GetParent", W.HWND, W.HWND)
    SetForegroundWindow = _fn(user32, "SetForegroundWindow", W.BOOL, W.HWND)
    GetCursorPos = _fn(user32, "GetCursorPos", W.BOOL, ctypes.POINTER(W.POINT))
    GetSystemMetrics = _fn(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
    CreatePopupMenu = _fn(user32, "CreatePopupMenu", W.HMENU)
    AppendMenuW = _fn(user32, "AppendMenuW", W.BOOL, W.HMENU, W.UINT, ctypes.c_size_t, W.LPCWSTR)
    SetMenuDefaultItem = _fn(user32, "SetMenuDefaultItem", W.BOOL, W.HMENU, W.UINT, W.UINT)
    TrackPopupMenu = _fn(user32, "TrackPopupMenu", ctypes.c_int, W.HMENU, W.UINT, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, W.HWND, ctypes.c_void_p)
    DestroyMenu = _fn(user32, "DestroyMenu", W.BOOL, W.HMENU)
    GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", W.HMODULE, W.LPCWSTR)
    CreateMutexW = _fn(kernel32, "CreateMutexW", HANDLE, ctypes.c_void_p, W.BOOL, W.LPCWSTR)

    # clipboard
    OpenClipboard = _fn(user32, "OpenClipboard", W.BOOL, W.HWND)
    CloseClipboard = _fn(user32, "CloseClipboard", W.BOOL)
    EmptyClipboard = _fn(user32, "EmptyClipboard", W.BOOL)
    GetClipboardData = _fn(user32, "GetClipboardData", HANDLE, W.UINT)
    SetClipboardData = _fn(user32, "SetClipboardData", HANDLE, W.UINT, HANDLE)
    EnumClipboardFormats = _fn(user32, "EnumClipboardFormats", W.UINT, W.UINT)
    GetClipboardFormatNameW = _fn(user32, "GetClipboardFormatNameW", ctypes.c_int, W.UINT, W.LPWSTR, ctypes.c_int)
    RegisterClipboardFormatW = _fn(user32, "RegisterClipboardFormatW", W.UINT, W.LPCWSTR)
    GetClipboardSequenceNumber = _fn(user32, "GetClipboardSequenceNumber", W.DWORD)
    GlobalAlloc = _fn(kernel32, "GlobalAlloc", HANDLE, W.UINT, ctypes.c_size_t)
    GlobalLock = _fn(kernel32, "GlobalLock", ctypes.c_void_p, HANDLE)
    GlobalUnlock = _fn(kernel32, "GlobalUnlock", W.BOOL, HANDLE)
    GlobalSize = _fn(kernel32, "GlobalSize", ctypes.c_size_t, HANDLE)
    GlobalFree = _fn(kernel32, "GlobalFree", HANDLE, HANDLE)

    # icons
    GetDC = _fn(user32, "GetDC", W.HDC, W.HWND)
    ReleaseDC = _fn(user32, "ReleaseDC", ctypes.c_int, W.HWND, W.HDC)
    CreateBitmap = _fn(gdi32, "CreateBitmap", W.HBITMAP, ctypes.c_int, ctypes.c_int, W.UINT, W.UINT, ctypes.c_void_p)
    DeleteObject = _fn(gdi32, "DeleteObject", W.BOOL, HANDLE)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                    ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                    ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                    ("biClrImportant", W.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD * 1)]

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", W.BOOL), ("xHotspot", W.DWORD), ("yHotspot", W.DWORD),
                    ("hbmMask", W.HBITMAP), ("hbmColor", W.HBITMAP)]

    CreateDIBSection = _fn(gdi32, "CreateDIBSection", W.HBITMAP, W.HDC, ctypes.POINTER(BITMAPINFO), W.UINT,
                           ctypes.POINTER(ctypes.c_void_p), HANDLE, W.DWORD)
    CreateIconIndirect = _fn(user32, "CreateIconIndirect", W.HICON, ctypes.POINTER(ICONINFO))

    # input
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD), ("time", W.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD),
                    ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _INPUTU(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", W.DWORD), ("u", _INPUTU)]

    SendInput = _fn(user32, "SendInput", W.UINT, W.UINT, ctypes.POINTER(INPUT), ctypes.c_int)

    # tray
    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", W.DWORD), ("hWnd", W.HWND), ("uID", W.UINT), ("uFlags", W.UINT),
                    ("uCallbackMessage", W.UINT), ("hIcon", W.HICON), ("szTip", W.WCHAR * 128),
                    ("dwState", W.DWORD), ("dwStateMask", W.DWORD), ("szInfo", W.WCHAR * 256),
                    ("uVersion", W.UINT), ("szInfoTitle", W.WCHAR * 64), ("dwInfoFlags", W.DWORD),
                    ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", W.HICON)]

    Shell_NotifyIconW = _fn(shell32, "Shell_NotifyIconW", W.BOOL, W.DWORD, ctypes.POINTER(NOTIFYICONDATAW))

    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, W.HWND, W.UINT, W.WPARAM, W.LPARAM)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", W.UINT), ("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                    ("hCursor", HANDLE), ("hbrBackground", HANDLE), ("lpszMenuName", W.LPCWSTR),
                    ("lpszClassName", W.LPCWSTR), ("hIconSm", W.HICON)]

    RegisterClassExW = _fn(user32, "RegisterClassExW", W.ATOM, ctypes.POINTER(WNDCLASSEXW))
    CreateWindowExW = _fn(user32, "CreateWindowExW", W.HWND, W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU,
                          W.HINSTANCE, ctypes.c_void_p)

# constants
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 1, 2, 4, 8, 0x4000
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C
VK_HOME, VK_LEFT, VK_RIGHT = 0x24, 0x25, 0x27
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
WM_APP = 0x8000
WM_TRAY, WM_REHOTKEY, WM_SUSPEND, WM_SHOWAPP, WM_BALLOON, WM_QUITAPP = (WM_APP + i for i in range(1, 7))
CLASS = "KeyShiftShellWindow"
TERMINALS = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "PuTTY", "VirtualConsoleClass"}
SCANCODES = (list(range(0x02, 0x0E)) + list(range(0x10, 0x1C)) + list(range(0x1E, 0x2A))
             + list(range(0x2B, 0x36)) + [0x56, 0x39])
MODSTATES = [(), (0x10, 0xA0), (0x11, 0x12, 0xA2, 0xA5), (0x10, 0xA0, 0x11, 0x12, 0xA2, 0xA5)]


def hkl_key(h):
    return f"{(h or 0) & 0xFFFFFFFF:08X}"


# ── layouts ──────────────────────────────────────────────────────────────────

_variant_ids = None


def _klid(lang, dev):
    """Registry id of the physical layout behind an HKL."""
    global _variant_ids
    if dev in (0, lang):
        return f"{lang:08X}"
    if dev & 0xF000 != 0xF000:
        return f"{dev:08X}"
    if _variant_ids is None:
        _variant_ids = {}
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Keyboard Layouts") as r:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(r, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(r, name) as k:
                            _variant_ids[str(winreg.QueryValueEx(k, "Layout Id")[0]).upper()] = name
                    except OSError:
                        pass
        except OSError:
            pass
    return _variant_ids.get(f"{dev & 0x0FFF:04X}", "")


def _layout_text(klid):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            rf"SYSTEM\CurrentControlSet\Control\Keyboard Layouts\{klid}") as k:
            return str(winreg.QueryValueEx(k, "Layout Text")[0])
    except OSError:
        return ""


def installed_hkls():
    n = GetKeyboardLayoutList(0, None)
    arr = (W.HKL * max(n, 1))()
    n = GetKeyboardLayoutList(n, arr)
    return [arr[i] or 0 for i in range(n)]


def read_layout(hkl):
    h = hkl & 0xFFFFFFFF
    lang, dev = h & 0xFFFF, h >> 16
    buf = ctypes.create_unicode_buffer(128)
    loc = buf.value if LCIDToLocaleName(lang, buf, 85, 0) else ""
    name = buf.value if loc and GetLocaleInfoEx(loc, 0x1001, buf, 128) else (loc or f"0x{lang:04X}")
    code = (loc.split("-")[0] if loc else "??").upper()[:3]
    sub = _layout_text(_klid(lang, dev))

    fwd = {}
    state = (ctypes.c_ubyte * 256)()
    out = ctypes.create_unicode_buffer(8)
    blank = (ctypes.c_ubyte * 256)()
    for m, keys in enumerate(MODSTATES):
        for i in range(256):
            state[i] = 0
        for k in keys:
            state[k] = 0x80
        for sc in SCANCODES:
            vk = MapVirtualKeyExW(sc, 1, hkl)
            if not vk:
                continue
            r = ToUnicodeEx(vk, sc, state, out, 8, 4, hkl)  # flag 4: don't touch keyboard state
            if r < 0:
                s = out.value[:1]  # dead key
                ToUnicodeEx(0x20, 0x39, blank, out, 8, 4, hkl)
            elif r > 0:
                s = out[:r]
            else:
                continue
            if s and all(ord(c) >= 32 and ord(c) != 127 for c in s):
                fwd[(sc, m)] = s
    return Layout(hkl, hkl_key(hkl), code, name, sub, fwd)


# ── input ────────────────────────────────────────────────────────────────────

def _key(vk, up):
    i = INPUT(type=1)
    i.ki = KEYBDINPUT(vk, MapVirtualKeyW(vk, 0), (2 if up else 0) | (1 if vk in EXTENDED else 0), 0, 0)
    return i


def press(*vks, times=1):
    """press(ctrl, 'C'): hold all but the last key, tap the last `times` times."""
    vks = [ord(v) if isinstance(v, str) else v for v in vks]
    mods, key = vks[:-1], vks[-1]
    seq = [_key(m, False) for m in mods]
    for _ in range(max(0, min(times, 3000))):
        seq += [_key(key, False), _key(key, True)]
    seq += [_key(m, True) for m in reversed(mods)]
    arr = (INPUT * len(seq))(*seq)
    SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def wait_modifiers_released(limit=1.5):
    end = time.time() + limit
    while time.time() < end:
        if not any(GetAsyncKeyState(v) & 0x8000 for v in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN)):
            return
        time.sleep(0.01)


# ── clipboard ────────────────────────────────────────────────────────────────

CF_UNICODETEXT = 13
_GDI_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}


def _open_clip():
    for _ in range(25):
        if OpenClipboard(None):
            return True
        time.sleep(0.015)
    return False


def _fmt_name(f):
    b = ctypes.create_unicode_buffer(128)
    return b.value if GetClipboardFormatNameW(f, b, 128) else ""


def clip_save(cap=32 * 1024 * 1024):
    saved, total = [], 0
    if not _open_clip():
        return None
    try:
        f = EnumClipboardFormats(0)
        while f:
            if f not in _GDI_FORMATS and not (0x200 <= f <= 0x3FF):
                h = GetClipboardData(f)
                if h:
                    size = GlobalSize(h)
                    p = GlobalLock(h)
                    if p and size and total + size <= cap:
                        saved.append((f, ctypes.string_at(p, size)))
                        total += size
                    if p:
                        GlobalUnlock(h)
            f = EnumClipboardFormats(f)
    finally:
        CloseClipboard()
    return saved


def _put(fmt, data):
    h = GlobalAlloc(0x0002, max(len(data), 1))  # GMEM_MOVEABLE
    p = GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    GlobalUnlock(h)
    if not SetClipboardData(fmt, h):
        GlobalFree(h)


def _no_history():
    # keep KeyShift's temporary clipboard writes out of Win+V history and cloud clipboard
    _put(RegisterClipboardFormatW("ExcludeClipboardContentFromMonitorProcessing"), b"\0\0\0\0")
    _put(RegisterClipboardFormatW("CanIncludeInClipboardHistory"), b"\0\0\0\0")
    _put(RegisterClipboardFormatW("CanUploadToCloudClipboard"), b"\0\0\0\0")


def clip_restore(saved):
    if saved is None or not _open_clip():
        return
    try:
        EmptyClipboard()
        for f, data in saved:
            _put(f, data)
        _no_history()
    finally:
        CloseClipboard()


def clip_set_text(text):
    if not _open_clip():
        return False
    try:
        EmptyClipboard()
        _put(CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))
        _no_history()
        return True
    finally:
        CloseClipboard()


def clip_get_text():
    """Clipboard text, or None if empty / it's an editor's 'copy whole line' with no selection."""
    if not _open_clip():
        return None
    try:
        f = EnumClipboardFormats(0)
        while f:
            n = _fmt_name(f) if f >= 0xC000 else ""
            if n == "VisualStudioEditorOperationsLineCutCopyClipboardTag":
                return None
            if n == "vscode-editor-data":
                h = GetClipboardData(f)
                p = GlobalLock(h) if h else None
                if p:
                    raw = ctypes.string_at(p, GlobalSize(h))
                    GlobalUnlock(h)
                    if b'"isFromEmptySelection":true' in raw.replace(b" ", b""):
                        return None
            f = EnumClipboardFormats(f)
        h = GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = GlobalLock(h)
        if not p:
            return None
        s = ctypes.wstring_at(p)
        GlobalUnlock(h)
        return s or None
    finally:
        CloseClipboard()


def copy_selection(timeout=0.3):
    seq = GetClipboardSequenceNumber()
    press(VK_CONTROL, "C")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.01)
        if GetClipboardSequenceNumber() != seq:
            time.sleep(0.02)
            return clip_get_text()
    return None


def paste(text):
    clip_set_text(text)
    press(VK_CONTROL, "V")
    time.sleep(0.06)


# ── misc ─────────────────────────────────────────────────────────────────────

def class_name(hwnd):
    b = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, b, 256)
    return b.value


def window_layout(hwnd):
    return GetKeyboardLayout(GetWindowThreadProcessId(hwnd, None)) or 0


def switch_layout(hwnd, hkl):
    PostMessageW(hwnd, 0x0050, 0, ctypes.c_ssize_t(hkl).value)  # WM_INPUTLANGCHANGEREQUEST


KEYNAMES = {0x13: "Pause", 0x20: "Space", 0x2D: "Insert", 0x2E: "Delete", 0x24: "Home", 0x23: "End",
            0x21: "Page Up", 0x22: "Page Down", 0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
            0x91: "Scroll Lock", 0x14: "Caps Lock", 0x09: "Tab", 0x0D: "Enter", 0x08: "Backspace"}


def hotkey_text(mods, vk):
    parts = [n for m, n in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))
             if mods & m]
    if vk in KEYNAMES:
        k = KEYNAMES[vk]
    elif 0x70 <= vk <= 0x87:
        k = f"F{vk - 0x6F}"
    elif 0x30 <= vk <= 0x5A:
        k = chr(vk)
    else:
        b = ctypes.create_unicode_buffer(32)
        k = b.value if GetKeyNameTextW(MapVirtualKeyW(vk, 0) << 16, b, 32) else f"Key {vk}"
    return " + ".join(parts + [k])


# ── config / startup ─────────────────────────────────────────────────────────

CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
DEFAULTS = {"hotkey": [MOD_CONTROL | MOD_SHIFT, 0x20], "disabled": [], "switch": True, "paused": False}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def load_cfg():
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return {**DEFAULTS, **json.load(f)}, False
    except (OSError, ValueError):
        return dict(DEFAULTS), True


def save_cfg(cfg):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    except OSError:
        pass


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{w if os.path.exists(w) else exe}" "{os.path.abspath(__file__)}" --tray'


def get_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return bool(winreg.QueryValueEx(k, APP)[0])
    except OSError:
        return False


def set_startup(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except OSError:
                    pass
    except OSError:
        pass


# ── icon (drawn in code, anti-aliased) ───────────────────────────────────────

ACCENT_TOP, ACCENT_BOT = (0x9A, 0xAB, 0xFF), (0x62, 0x70, 0xF0)


def _tri(x, y, a, b, c):
    d1 = (x - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (y - b[1])
    d2 = (x - c[0]) * (b[1] - c[1]) - (b[0] - c[0]) * (y - c[1])
    d3 = (x - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (y - a[1])
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def _in_rrect(x, y, inset=0.03, r=0.27):
    cx = max(abs(x - 0.5) - (0.5 - inset - r), 0)
    cy = max(abs(y - 0.5) - (0.5 - inset - r), 0)
    return cx * cx + cy * cy <= r * r


def _in_arrows(x, y, t=0.052, head=0.15):
    if 0.22 <= x <= 0.60 and abs(y - 0.37) <= t:
        return True
    if _tri(x, y, (0.56, 0.37 - head), (0.56, 0.37 + head), (0.80, 0.37)):
        return True
    if 0.40 <= x <= 0.78 and abs(y - 0.63) <= t:
        return True
    return _tri(x, y, (0.44, 0.63 - head), (0.44, 0.63 + head), (0.20, 0.63))


def icon_pixels(n, ss=4):
    """n*n list of (r, g, b, alpha 0..1) — straight alpha."""
    px = []
    for j in range(n):
        for i in range(n):
            bg = fg = 0
            for sy in range(ss):
                y = (j + (sy + 0.5) / ss) / n
                for sx in range(ss):
                    x = (i + (sx + 0.5) / ss) / n
                    if _in_rrect(x, y):
                        bg += 1
                        fg += _in_arrows(x, y)
            a = bg / (ss * ss)
            if bg:
                t = (j + 0.5) / n
                base = [ACCENT_TOP[k] + (ACCENT_BOT[k] - ACCENT_TOP[k]) * t for k in range(3)]
                w = fg / bg
                col = tuple(int(base[k] + (255 - base[k]) * w) for k in range(3))
            else:
                col = (0, 0, 0)
            px.append((*col, a))
    return px


def make_hicon(n):
    bgra = bytearray()
    for r, g, b, a in icon_pixels(n):
        bgra += bytes((b, g, r, int(a * 255)))
    bmi = BITMAPINFO()
    bmi.bmiHeader = BITMAPINFOHEADER(40, n, -n, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = ctypes.c_void_p()
    dc = GetDC(None)
    color = CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ReleaseDC(None, dc)
    ctypes.memmove(bits, bytes(bgra), len(bgra))
    mask_bytes = (ctypes.c_ubyte * (((n + 15) // 16) * 2 * n))()
    mask = CreateBitmap(n, n, 1, 1, mask_bytes)
    hicon = CreateIconIndirect(ctypes.byref(ICONINFO(True, 0, 0, mask, color)))
    DeleteObject(color)
    DeleteObject(mask)
    return hicon


# ─────────────────────────────────────────────────────────────────────────────
#  Core: state + the fix action
# ─────────────────────────────────────────────────────────────────────────────


class Core:
    def __init__(self):
        self.cfg, self.first_run = load_cfg()
        self.lock = threading.RLock()
        self.busy = threading.Lock()
        self.layouts, self._sig = [], None
        self.last = None  # (hwnd, text, layout_key, time) — in memory only, for press-again-to-cycle
        self.refresh()

    def refresh(self):
        with self.lock:
            hkls = installed_hkls()
            sig = tuple(hkls)
            if sig == self._sig:
                return False
            self._sig = sig
            self.layouts = [read_layout(h) for h in hkls]
            return True

    def enabled(self):
        with self.lock:
            off = set(self.cfg["disabled"])
            return [L for L in self.layouts if L.key not in off]

    def save(self):
        save_cfg(self.cfg)

    # hotkey pressed ─────────────────────────────────────────────────────────
    def fix(self):
        if not self.busy.acquire(False):
            return
        try:
            self._fix()
        except Exception:
            pass
        finally:
            self.busy.release()

    def _fix(self):
        fg = GetForegroundWindow()
        if not fg or class_name(fg) in TERMINALS:  # Ctrl+C in a terminal = interrupt; never do that
            return
        wait_modifiers_released()
        self.refresh()
        layouts = self.enabled()
        if len(layouts) < 2:
            return
        cur = hkl_key(window_layout(fg))
        saved = clip_save()
        out = dst = None
        try:
            text = copy_selection()
            if text:  # 1) there is a selection -> convert all of it
                d = detect(text, layouts, cur)
                if not d:
                    return
                src, dst = d
                out = convert(text, src, dst)
                if out == text:
                    return
                paste(out)
            else:
                # 2) pressed again right after a fix -> cycle that same text to the next layout
                last = self.last
                if last and last[0] == fg and time.time() - last[3] < 12:
                    press(VK_SHIFT, VK_LEFT, times=caret_len(last[1]))
                    t = copy_selection()
                    src = next((L for L in layouts if L.key == last[2]), None)
                    if t == last[1] and src:
                        dst = next_layout(layouts, src)
                        out = convert(t, src, dst)
                        paste(out)
                    else:
                        press(VK_RIGHT)
                # 3) nothing selected -> fix the wrong-layout tail of the current line
                if out is None:
                    press(VK_SHIFT, VK_HOME)
                    line = copy_selection()
                    if not line:
                        return
                    press(VK_RIGHT)  # collapse selection back to the caret
                    line = line.replace("\r\n", "\n").split("\n")[-1]
                    r = wrong_tail(line, layouts)
                    if not r:
                        return
                    k, src, dst = r
                    tail = line[k:]
                    press(VK_SHIFT, VK_LEFT, times=caret_len(tail))
                    out = convert(tail, src, dst)
                    paste(out)
            self.last = (fg, out, dst.key, time.time())
            if self.cfg["switch"]:
                switch_layout(fg, dst.hkl)
        finally:
            time.sleep(0.25)  # let the target app finish reading the clipboard
            clip_restore(saved)


# ─────────────────────────────────────────────────────────────────────────────
#  Shell thread: hidden window for hotkey + tray icon
# ─────────────────────────────────────────────────────────────────────────────


class Shell(threading.Thread):
    def __init__(self, core, events, hicon):
        super().__init__(daemon=True)
        self.core, self.events, self.hicon = core, events, hicon
        self.hwnd = None
        self.suspended = False
        self.ready = threading.Event()

    def run(self):
        hinst = GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = CLASS
        RegisterClassExW(ctypes.byref(wc))
        self.hwnd = CreateWindowExW(0x80, CLASS, APP, 0, 0, 0, 0, 0, None, None, hinst, None)
        self.taskbar_created = RegisterWindowMessageW("TaskbarCreated")
        self._icon(0)
        self.register()
        self.ready.set()
        msg = W.MSG()
        while GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(msg))
            DispatchMessageW(ctypes.byref(msg))

    def post(self, msg):
        if self.hwnd:
            PostMessageW(self.hwnd, msg, 0, 0)

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        return nid

    def _tip(self):
        if self.core.cfg["paused"]:
            return f"{APP} — paused"
        return f"{APP} — {hotkey_text(*self.core.cfg['hotkey'])}"

    def _icon(self, action):  # 0 add, 1 modify, 2 delete
        nid = self._nid()
        nid.uFlags = 0x1 | 0x2 | 0x4
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self._tip()
        Shell_NotifyIconW(action, ctypes.byref(nid))

    def _balloon(self, title, text):
        nid = self._nid()
        nid.uFlags = 0x10
        nid.szInfoTitle, nid.szInfo, nid.dwInfoFlags = title, text, 0x10
        Shell_NotifyIconW(1, ctypes.byref(nid))

    def register(self):
        UnregisterHotKey(self.hwnd, 1)
        if not (self.suspended or self.core.cfg["paused"]):
            mods, vk = self.core.cfg["hotkey"]
            ok = bool(RegisterHotKey(self.hwnd, 1, mods | MOD_NOREPEAT, vk))
            self.events.put(("hotkey", ok))
        self._icon(1)

    def _menu(self):
        m = CreatePopupMenu()
        AppendMenuW(m, 0, 1, f"Open {APP}")
        AppendMenuW(m, 0x8 if self.core.cfg["paused"] else 0, 2, "Pause")
        AppendMenuW(m, 0x800, 0, None)
        AppendMenuW(m, 0, 3, "Quit")
        SetMenuDefaultItem(m, 1, 0)
        pt = W.POINT()
        GetCursorPos(ctypes.byref(pt))
        SetForegroundWindow(self.hwnd)
        cmd = TrackPopupMenu(m, 0x100 | 0x80 | 0x2, pt.x, pt.y, 0, self.hwnd, None)
        PostMessageW(self.hwnd, 0, 0, 0)
        DestroyMenu(m)
        if cmd == 1:
            self.events.put(("show",))
        elif cmd == 2:
            self.core.cfg["paused"] = not self.core.cfg["paused"]
            self.core.save()
            self.register()
            self.events.put(("state",))
        elif cmd == 3:
            self.events.put(("quit",))

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == 0x0312:  # WM_HOTKEY
                threading.Thread(target=self.core.fix, daemon=True).start()
            elif msg == WM_TRAY:
                ev = lp & 0xFFFF
                if ev == 0x0202:  # left click
                    self.events.put(("show",))
                elif ev in (0x0205, 0x007B):  # right click / context menu
                    self._menu()
            elif msg == WM_REHOTKEY:
                self.suspended = False
                self.register()
            elif msg == WM_SUSPEND:
                self.suspended = True
                UnregisterHotKey(self.hwnd, 1)
            elif msg == WM_SHOWAPP:
                self.events.put(("show",))
            elif msg == WM_BALLOON:
                self._balloon(APP, f"Still running in the tray. {hotkey_text(*self.core.cfg['hotkey'])} fixes text.")
            elif msg == WM_QUITAPP:
                UnregisterHotKey(self.hwnd, 1)
                self._icon(2)
                DestroyWindow(self.hwnd)
            elif msg == 0x0002:  # WM_DESTROY
                PostQuitMessage(0)
            elif msg == self.taskbar_created:
                self._icon(0)
        except Exception:
            pass
        return DefWindowProcW(hwnd, msg, wp, lp)


# ─────────────────────────────────────────────────────────────────────────────
#  Settings window (tkinter)
# ─────────────────────────────────────────────────────────────────────────────

BG, SURF, BORDER, CHIP = "#0f1013", "#17181c", "#24262c", "#1f2230"
TEXT, MUTED, ACCENT, DANGER = "#ececf1", "#80848f", "#8b9bff", "#ff7a7a"
OFF, KNOB = "#34363d", "#ffffff"
FONT = "Segoe UI"


def _hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c)


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def photo(tk, w, h, pixel):
    """Build a PhotoImage from pixel(i, j) -> (r, g, b)."""
    img = tk.PhotoImage(width=w, height=h)
    rows = ["{" + " ".join(_hex(pixel(i, j)) for i in range(w)) + "}" for j in range(h)]
    img.put(" ".join(rows))
    return img


def logo_image(tk, n, bg):
    px = icon_pixels(n)
    b = _rgb(bg)
    return photo(tk, n, n, lambda i, j: _mix(b, px[j * n + i][:3], px[j * n + i][3]))


def toggle_image(tk, w, h, pos, bg, ss=3):
    b, off, on, knob = _rgb(bg), _rgb(OFF), _rgb(ACCENT), _rgb(KNOB)
    track = _mix(off, on, pos)
    r = h / 2
    m = max(2.0, h * 0.14)
    kr = r - m
    kx = r + pos * (w - 2 * r)

    def pixel(i, j):
        tc = kc = 0
        for sy in range(ss):
            y = j + (sy + 0.5) / ss
            for sx in range(ss):
                x = i + (sx + 0.5) / ss
                cx = min(max(x, r), w - r)
                if (x - cx) ** 2 + (y - r) ** 2 <= r * r:
                    tc += 1
                    if (x - kx) ** 2 + (y - r) ** 2 <= kr * kr:
                        kc += 1
        n = ss * ss
        col = _mix(b, track, tc / n)
        return _mix(col, knob, kc / n)

    return photo(tk, w, h, pixel)


class Toggle:
    FRAMES = 7
    _cache = {}

    def __init__(self, tk, master, value, command, S):
        self.tk, self.value, self.command = tk, value, command
        self.w, self.h = round(34 * S), round(20 * S)
        bg = master["bg"]
        key = (self.w, self.h, bg)
        if key not in Toggle._cache:
            Toggle._cache[key] = [toggle_image(tk, self.w, self.h, f / (self.FRAMES - 1), bg)
                                  for f in range(self.FRAMES)]
        self.frames = Toggle._cache[key]
        self.idx = self.FRAMES - 1 if value else 0
        self.label = tk.Label(master, image=self.frames[self.idx], bg=bg, bd=0, cursor="hand2")
        self.label.bind("<Button-1>", lambda e: self.flip())

    def flip(self):
        self.set(not self.value)
        if self.command:
            self.command(self.value)

    def set(self, v):
        self.value = v
        self._animate()

    def _animate(self):
        target = self.FRAMES - 1 if self.value else 0
        if self.idx == target:
            return
        self.idx += 1 if target > self.idx else -1
        self.label.configure(image=self.frames[self.idx])
        self.label.after(14, self._animate)


class App:
    def __init__(self, core, show):
        import tkinter as tk
        self.tk, self.core = tk, core
        self.events = queue.Queue()
        self.hicon_small = make_hicon(GetSystemMetrics(49) or 16)
        self.hicon_big = make_hicon(GetSystemMetrics(11) or 32)
        self.shell = Shell(core, self.events, self.hicon_small)
        self.shell.start()
        self.shell.ready.wait(3)

        self.root = r = tk.Tk()
        r.withdraw()
        self.S = r.winfo_fpixels("1i") / 96
        r.title(APP)
        r.configure(bg=BG)
        r.resizable(False, False)
        r.minsize(self.px(400), 0)
        r.protocol("WM_DELETE_WINDOW", self.hide)
        self.visible = False
        self.styled = False
        self.capturing = False
        self.told_tray = False
        self.hotkey_ok = True
        self.build()
        r.after(60, self.poll)
        r.after(2000, self.watch_layouts)
        if show:
            self.show()

    def px(self, v):
        return round(v * self.S)

    # ── layout ───────────────────────────────────────────────────────────────
    def build(self):
        tk, px = self.tk, self.px
        outer = tk.Frame(self.root, bg=BG, padx=px(22), pady=px(20))
        outer.pack(fill="both", expand=True)
        self.outer = outer

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", pady=(0, px(16)))
        self.logo = logo_image(tk, px(34), BG)
        tk.Label(head, image=self.logo, bg=BG).pack(side="left", padx=(0, px(12)))
        t = tk.Frame(head, bg=BG)
        t.pack(side="left", fill="x", expand=True)
        tk.Label(t, text=APP, bg=BG, fg=TEXT, font=(FONT + " Semibold", 15), anchor="w").pack(fill="x")
        tk.Label(t, text="Fix text typed in the wrong layout", bg=BG, fg=MUTED, font=(FONT, 9),
                 anchor="w").pack(fill="x")

        # layouts
        self.section(outer, "LAYOUTS")
        self.layout_box = tk.Frame(outer, bg=BG)
        self.layout_box.pack(fill="x")
        self.layout_hint = tk.Label(outer, text="", bg=BG, fg=DANGER, font=(FONT, 9), anchor="w")
        self.render_layouts()

        # shortcut
        self.section(outer, "SHORTCUT")
        hk = tk.Frame(outer, bg=SURF, highlightthickness=1, highlightbackground=BORDER, cursor="hand2")
        hk.pack(fill="x")
        self.hk_frame = hk
        self.hk_label = tk.Label(hk, text="", bg=SURF, fg=TEXT, font=(FONT + " Semibold", 11),
                                 pady=px(10), cursor="hand2")
        self.hk_label.pack(fill="x")
        for w in (hk, self.hk_label):
            w.bind("<Button-1>", lambda e: self.start_capture())
            w.bind("<Enter>", lambda e: hk.configure(highlightbackground=ACCENT))
            w.bind("<Leave>", lambda e: None if self.capturing else hk.configure(highlightbackground=BORDER))
        self.hk_hint = tk.Label(outer, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                                justify="left", wraplength=px(356))
        self.hk_hint.pack(fill="x", pady=(px(6), 0))
        self.render_hotkey()

        # try it
        self.section(outer, "TRY IT")
        box = tk.Frame(outer, bg=SURF, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill="x")
        self.test = tk.Entry(box, bg=SURF, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0,
                             font=(FONT, 11), highlightthickness=0)
        self.test.pack(fill="x", padx=px(12), pady=px(9))
        self.test.bind("<KeyRelease>", lambda e: self.render_test())
        self.test.bind("<FocusIn>", lambda e: box.configure(highlightbackground=ACCENT))
        self.test.bind("<FocusOut>", lambda e: box.configure(highlightbackground=BORDER))
        res = tk.Frame(outer, bg=BG)
        res.pack(fill="x", pady=(px(8), 0))
        self.test_dir = tk.Label(res, text="", bg=CHIP, fg=ACCENT, font=(FONT + " Semibold", 8), padx=px(6))
        self.test_out = tk.Label(res, text="", bg=BG, fg=TEXT, font=(FONT, 11), anchor="w")
        self.render_test()

        # options
        self.section(outer, "OPTIONS")
        self.row(outer, "Switch keyboard after fixing", "Keep typing in the right language",
                 self.core.cfg["switch"], self.set_switch)
        self.row(outer, "Start with Windows", "Waits quietly in the tray",
                 get_startup(), set_startup)

        foot = tk.Frame(outer, bg=BG)
        foot.pack(fill="x", pady=(px(12), 0))
        tk.Label(foot, text=f"v{VERSION}  ·  nothing you type is stored", bg=BG, fg=MUTED,
                 font=(FONT, 8)).pack(side="left")
        q = tk.Label(foot, text="Quit", bg=BG, fg=MUTED, font=(FONT, 9), cursor="hand2")
        q.pack(side="right")
        q.bind("<Button-1>", lambda e: self.quit())
        q.bind("<Enter>", lambda e: q.configure(fg=DANGER))
        q.bind("<Leave>", lambda e: q.configure(fg=MUTED))

    def section(self, parent, text):
        self.tk.Label(parent, text=text, bg=BG, fg=MUTED, font=(FONT + " Semibold", 8),
                      anchor="w").pack(fill="x", pady=(self.px(14), self.px(6)))

    def row(self, parent, title, sub, value, command, chip=None, dim=False):
        tk, px = self.tk, self.px
        f = tk.Frame(parent, bg=SURF, highlightthickness=1, highlightbackground=BORDER, cursor="hand2")
        f.pack(fill="x", pady=(0, px(6)))
        inner = tk.Frame(f, bg=SURF)
        inner.pack(fill="x", padx=px(12), pady=px(9))
        widgets = [f, inner]
        if chip:
            c = tk.Label(inner, text=chip, bg=CHIP, fg=ACCENT if not dim else MUTED,
                         font=(FONT + " Semibold", 8), width=4, pady=px(3))
            c.pack(side="left", padx=(0, px(12)))
            widgets.append(c)
        txt = tk.Frame(inner, bg=SURF)
        txt.pack(side="left", fill="x", expand=True)
        a = tk.Label(txt, text=title, bg=SURF, fg=MUTED if dim else TEXT, font=(FONT + " Semibold", 10), anchor="w")
        a.pack(fill="x")
        widgets += [txt, a]
        if sub:
            b = tk.Label(txt, text=sub, bg=SURF, fg=MUTED, font=(FONT, 9), anchor="w")
            b.pack(fill="x")
            widgets.append(b)
        tg = Toggle(tk, inner, value, command, self.S)
        tg.label.pack(side="right")
        for w in widgets:
            w.bind("<Button-1>", lambda e: tg.flip())
        return f, tg

    def render_layouts(self):
        for w in self.layout_box.winfo_children():
            w.destroy()
        off = set(self.core.cfg["disabled"])
        for L in self.core.layouts:
            on = L.key not in off
            sub = L.sub if L.sub and L.sub.lower() != L.name.lower() else f"HKL {L.key}"
            self.row(self.layout_box, L.name, sub, on, lambda v, k=L.key: self.set_layout(k, v),
                     chip=L.code, dim=not on)
        n = len(self.core.enabled())
        if len(self.core.layouts) < 2:
            msg = "Only one keyboard layout is installed. Add another in Windows Settings › Language."
        elif n < 2:
            msg = "Turn on at least two layouts."
        else:
            msg = ""
        self.layout_hint.configure(text=msg)
        if msg:
            self.layout_hint.pack(fill="x", after=self.layout_box)
        else:
            self.layout_hint.pack_forget()

    def render_hotkey(self):
        if self.capturing:
            self.hk_label.configure(text="Press a shortcut…   Esc to cancel", fg=ACCENT)
            return
        self.hk_label.configure(text=hotkey_text(*self.core.cfg["hotkey"]), fg=TEXT)
        if self.core.cfg["paused"]:
            self.hk_hint.configure(text="Paused — resume from the tray icon.", fg=DANGER)
        elif not self.hotkey_ok:
            self.hk_hint.configure(text="Another app already uses this shortcut. Click to pick a different one.",
                                   fg=DANGER)
        else:
            self.hk_hint.configure(
                text="Select text and press it. Nothing selected? It fixes the end of the line. "
                     "Press again to undo / cycle.", fg=MUTED)

    def render_test(self):
        text = self.test.get()
        layouts = self.core.enabled()
        d = detect(text, layouts) if text else None
        self.test_dir.pack_forget()
        self.test_out.pack_forget()
        if d:
            src, dst = d
            self.test_dir.configure(text=f"{src.code} → {dst.code}")
            self.test_dir.pack(side="left", padx=(0, self.px(10)))
            self.test_out.configure(text=convert(text, src, dst), fg=TEXT, font=(FONT, 11))
        else:
            hint = "Type something in the wrong layout to preview" if not text else "Nothing to convert"
            self.test_out.configure(text=hint, fg=MUTED, font=(FONT, 9))
        self.test_out.pack(side="left", fill="x")

    # ── actions ──────────────────────────────────────────────────────────────
    def set_layout(self, key, on):
        off = set(self.core.cfg["disabled"])
        off.discard(key) if on else off.add(key)
        self.core.cfg["disabled"] = sorted(off)
        self.core.save()
        self.root.after(120, lambda: (self.render_layouts(), self.render_test()))

    def set_switch(self, v):
        self.core.cfg["switch"] = v
        self.core.save()

    def start_capture(self):
        if self.capturing:
            return
        self.capturing = True
        self.shell.post(WM_SUSPEND)
        self.hk_frame.configure(highlightbackground=ACCENT)
        self.render_hotkey()
        self.root.bind("<KeyPress>", self.on_capture)
        self.root.focus_force()

    def end_capture(self):
        self.capturing = False
        self.root.unbind("<KeyPress>")
        self.hk_frame.configure(highlightbackground=BORDER)
        self.shell.post(WM_REHOTKEY)
        self.render_hotkey()

    def on_capture(self, e):
        vk = e.keycode
        if vk in (16, 17, 18, 91, 92, 160, 161, 162, 163, 164, 165, 20):
            return "break"
        mods = 0
        for v, m in ((VK_CONTROL, MOD_CONTROL), (VK_MENU, MOD_ALT), (VK_SHIFT, MOD_SHIFT),
                     (VK_LWIN, MOD_WIN), (VK_RWIN, MOD_WIN)):
            if GetAsyncKeyState(v) & 0x8000:
                mods |= m
        if vk == 27 and not mods:
            self.end_capture()
            return "break"
        standalone = vk in (0x13, 0x91) or 0x70 <= vk <= 0x87
        if not mods and not standalone:
            return "break"
        self.core.cfg["hotkey"] = [mods, vk]
        self.core.save()
        self.end_capture()
        return "break"

    # ── window ───────────────────────────────────────────────────────────────
    def show(self):
        r = self.root
        first = not self.styled
        r.deiconify()
        if first:
            r.update_idletasks()
            w, h = r.winfo_reqwidth(), r.winfo_reqheight()
            r.geometry(f"+{(r.winfo_screenwidth() - w) // 2}+{(r.winfo_screenheight() - h) // 3}")
            self.style_window()
        r.lift()
        r.attributes("-topmost", True)
        r.after(50, lambda: r.attributes("-topmost", False))
        r.focus_force()
        self.visible = True
        if self.core.refresh():
            self.render_layouts()

    def style_window(self):
        self.styled = True
        try:
            hwnd = GetParent(self.root.winfo_id()) or self.root.winfo_id()
            dark = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(W.HWND(hwnd), 20, ctypes.byref(dark), 4)
            SendMessageW(hwnd, 0x0080, 0, self.hicon_small or 0)
            SendMessageW(hwnd, 0x0080, 1, self.hicon_big or 0)
        except Exception:
            pass

    def hide(self):
        if self.capturing:
            self.end_capture()
        self.root.withdraw()
        self.visible = False
        if not self.told_tray:
            self.told_tray = True
            self.shell.post(WM_BALLOON)

    def quit(self):
        self.shell.post(WM_QUITAPP)
        self.root.after(150, self.root.destroy)

    def poll(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "show":
                    self.show()
                elif ev[0] == "quit":
                    self.quit()
                    return
                elif ev[0] == "hotkey":
                    self.hotkey_ok = ev[1]
                    self.render_hotkey()
                    if not ev[1] and not self.visible:
                        self.show()
                elif ev[0] == "state":
                    self.render_hotkey()
        except queue.Empty:
            pass
        self.root.after(60, self.poll)

    def watch_layouts(self):
        if self.visible and self.core.refresh():
            self.render_layouts()
            self.render_test()
        self.root.after(2000, self.watch_layouts)

    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    CreateMutexW(None, False, "Local\\KeyShift.SingleInstance")
    if ctypes.get_last_error() == 183:  # already running -> bring it up
        h = FindWindowW(CLASS, None)
        if h:
            PostMessageW(h, WM_SHOWAPP, 0, 0)
        return
    core = Core()
    if core.first_run:
        core.save()
        set_startup(True)
    App(core, show="--tray" not in sys.argv).run()


if __name__ == "__main__" and WIN:
    main()
