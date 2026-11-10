"""Desktop helpers for driving Bambu Studio: screenshot, find its window, click, keys.
Windows only, ctypes + Pillow."""
import ctypes
import ctypes.wintypes as wt
import time

from PIL import ImageGrab

user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixel coordinates on scaled displays
except (AttributeError, OSError):
    pass


def shot(path, bbox=None):
    ImageGrab.grab(bbox=bbox, all_screens=True).save(path)
    return path


def windows(title_part):
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            n = user32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, buf, n + 1)
            if title_part.lower() in buf.value.lower():
                r = wt.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                out.append((h, buf.value, (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(cb, 0)
    return out


def focus(hwnd):
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.3)
