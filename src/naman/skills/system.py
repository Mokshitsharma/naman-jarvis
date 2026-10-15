"""Laptop control: volume, media, brightness, windows, keyboard, screen, power.

Windows is the main target (ctypes + built-in APIs); functions return an error string
on other systems instead of raising.
"""

from __future__ import annotations

import ctypes
import os
import platform
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..apps import ALIASES, _norm

WINDOWS = platform.system() == "Windows"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Virtual-key codes
VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21,
    "pagedown": 0x22, "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "win": 0x5B,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF, "next": 0xB0, "previous": 0xB1,
    "stop": 0xB2, "play_pause": 0xB3,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"},
}
MODIFIER_NAMES = {"control": "ctrl", "cmd": "win", "windows": "win", "option": "alt", "return": "enter"}
# Hotkeys Naman won't press for you: they can wipe work or bypass the lock screen flow.
BLOCKED_COMBOS = {("alt", "f4"), ("ctrl", "alt", "delete"), ("shift", "delete")}
# Never close these: the Windows shell, or Naman's own terminal.
PROTECTED_TITLES = {"program manager", "naman"}


def _need_windows() -> str | None:
    return None if WINDOWS else "error: this skill only works on Windows for now"


def _tap(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


# --- sound & media ---------------------------------------------------------------------


def _endpoint():
    from pycaw.pycaw import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def volume(action: str, level: int | None = None) -> str:
    """action: set | up | down | mute | unmute | get"""
    if err := _need_windows():
        return err
    ev = _endpoint()
    current = round(ev.GetMasterVolumeLevelScalar() * 100)
    if action == "get":
        return f"volume {current}%{' (muted)' if ev.GetMute() else ''}"
    if action in ("mute", "unmute"):
        ev.SetMute(1 if action == "mute" else 0, None)
        return f"{action}d"
    if action == "set":
        if level is None:
            return "error: give a level from 0 to 100"
        target = level
    else:
        step = level if level is not None else 10
        target = current + step if action == "up" else current - step
    target = max(0, min(100, int(target)))
    ev.SetMute(0, None)
    ev.SetMasterVolumeLevelScalar(target / 100, None)
    return f"volume {target}%"


def media(action: str) -> str:
    """action: play_pause | next | previous | stop"""
    if err := _need_windows():
        return err
    if action not in ("play_pause", "next", "previous", "stop"):
        return "error: unknown media action"
    _tap(VK[action])
    return f"sent {action.replace('_', '/')}"


def brightness(level: int) -> str:
    if err := _need_windows():
        return err
    level = max(0, min(100, int(level)))
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
         f" | Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}}"],
        capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW,
    )
    if result.returncode != 0 or "error" in result.stderr.lower():
        return "error: this screen doesn't allow software brightness control (external monitors usually don't)"
    return f"brightness {level}%"


# --- screen, status, power -------------------------------------------------------------


def screenshot() -> str:
    from PIL import ImageGrab

    folder = Path.home() / "Pictures" / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"naman-{datetime.now():%Y%m%d-%H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return f"saved screenshot to {path}"


def system_status() -> dict:
    import psutil

    battery = psutil.sensors_battery()
    disk = psutil.disk_usage(os.path.abspath(os.sep))
    return {
        "battery_percent": battery.percent if battery else None,
        "charging": battery.power_plugged if battery else None,
        "battery_minutes_left": (battery.secsleft // 60) if battery and battery.secsleft > 0 else None,
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_free_gb": round(disk.free / 1e9, 1),
    }


def lock_screen() -> str:
    if err := _need_windows():
        return err
    ctypes.windll.user32.LockWorkStation()
    return "locked"


def power(action: str) -> str:
    """action: sleep | restart | shutdown (restart/shutdown wait 30 s; cancel with cancel_shutdown)"""
    if err := _need_windows():
        return err
    if action == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        return "going to sleep"
    if action in ("restart", "shutdown"):
        flag = "/r" if action == "restart" else "/s"
        subprocess.run(["shutdown", flag, "/t", "30"], creationflags=NO_WINDOW)
        return f"{action} in 30 seconds. Say 'cancel shutdown' to stop it"
    return "error: unknown power action"


def cancel_shutdown() -> str:
    if err := _need_windows():
        return err
    result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, creationflags=NO_WINDOW)
    return "cancelled" if result.returncode == 0 else "nothing to cancel"


# --- windows ---------------------------------------------------------------------------


def _windows() -> list[tuple[int, str]]:
    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def _match_windows(name: str) -> list[tuple[int, str]]:
    q = _norm(name)
    q = ALIASES.get(q, q)
    words = q.split()
    hits = []
    for hwnd, title in _windows():
        t = _norm(title)
        if t in PROTECTED_TITLES or t.startswith("naman"):
            continue
        if all(re.search(rf"\b{re.escape(w)}", t) for w in words):
            hits.append((hwnd, title))
    return hits


BROWSERS = ("google chrome", "microsoft edge", "mozilla firefox", "brave", "opera")


def process_name(hwnd: int) -> str:
    """'spotify.exe', 'chrome.exe'... for a window, or '' if it can't be read."""
    import psutil

    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def app_window(app_name: str) -> int | None:
    """The window of an app that is already open, found by the app's name in the title.

    A browser tab that merely mentions the app ("spotify - Google Search - Google Chrome")
    doesn't count, unless the app is that browser. Web apps installed from Chrome
    ("Spotify - Web Player") have no browser suffix, so they are found."""
    if not WINDOWS:
        return None
    q = ALIASES.get(_norm(app_name), _norm(app_name))
    words = q.split()
    exe = (words[0] + ".exe") if words else ""
    is_browser = any(b in q for b in BROWSERS)
    for hwnd, title in _windows():
        t = _norm(title)
        if not t or t in PROTECTED_TITLES or t.startswith("naman"):
            continue
        if not is_browser and any(t.endswith(b) for b in BROWSERS):
            continue
        if all(re.search(rf"\b{re.escape(w)}\b", t) for w in words) or (exe and process_name(hwnd) == exe):
            return hwnd
    return None


def window_handles() -> set[int]:
    return {hwnd for hwnd, _ in _windows()} if WINDOWS else set()


def wait_for_window(app_name: str, before: set[int], timeout: float = 12.0) -> int | None:
    """After launching an app, wait until its window is up.

    Prefers a new window named like the app; after 3 s any new window counts (some
    apps title their window by document, e.g. File Explorer shows the folder)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        hwnd = app_window(app_name)
        if hwnd and hwnd not in before:
            return hwnd
        if time.monotonic() - start > 3:
            new = window_handles() - before
            if new:
                return next(iter(new))
        time.sleep(0.2)
    return app_window(app_name)


def bring_to_front(hwnd: int) -> None:
    _focus(hwnd)


def list_windows() -> list[str]:
    if err := _need_windows():
        return [err]
    return [t for _, t in _windows() if _norm(t) not in PROTECTED_TITLES][:30]


def close_app(name: str) -> str:
    """Politely close matching windows (like clicking X), so apps can ask to save."""
    if err := _need_windows():
        return err
    hits = _match_windows(name)
    if not hits:
        return f"error: no open window matches {name!r}"
    for hwnd, _ in hits:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return f"closed {len(hits)} window(s): " + "; ".join(t for _, t in hits[:5])


def _focus(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    _tap(VK["alt"])  # lets a background process take focus
    user32.SetForegroundWindow(hwnd)


def _foreground_title() -> str:
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def switch_to(name: str) -> str:
    if err := _need_windows():
        return err
    hits = _match_windows(name)
    if not hits:
        return f"error: no open window matches {name!r}"
    hwnd, title = hits[0]
    _focus(hwnd)
    return f"switched to {title}"


# Same shortcuts in Chrome, Edge, Firefox, Brave, VS Code and most tabbed apps.
TAB_KEYS = {"close": "ctrl+w", "new": "ctrl+t", "reopen": "ctrl+shift+t", "next": "ctrl+tab",
            "previous": "ctrl+shift+tab"}
MAX_TABS = 10


def tab(action: str, app: str | None = None, count: int = 1) -> str:
    """Close / open / reopen / cycle tabs in `app`, or in the window in front when no app is named."""
    if err := _need_windows():
        return err
    if action not in TAB_KEYS:
        return f"error: action must be one of {', '.join(TAB_KEYS)}"
    if app:
        hits = _match_windows(app)
        if not hits:
            return f"error: no open window matches {app!r}"
        _focus(hits[0][0])
        time.sleep(0.3)  # give the window time to take focus before the keys arrive
    title = _foreground_title()
    t = _norm(title)
    if not t or t in PROTECTED_TITLES or t.startswith("naman"):
        return "error: no app window is in front. Ask which app's tab they mean."
    count = max(1, min(int(count or 1), MAX_TABS))
    for _ in range(count):
        if (result := press_keys(TAB_KEYS[action])).startswith("error"):
            return result
        time.sleep(0.15)
    return f"{action} tab{'s' * (count > 1)} x{count} in: {title}"


# --- keyboard & clipboard --------------------------------------------------------------


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def type_text(text: str) -> str:
    """Type into whatever window has focus (Unicode-safe). Never presses Enter by itself."""
    if err := _need_windows():
        return err
    text = text.replace("\r", "").replace("\n", " ")[:2000]
    for ch in text:
        for flags in (0x0004, 0x0004 | 0x0002):  # KEYEVENTF_UNICODE, then key up
            inp = _INPUT(type=1)
            inp.ki = _KEYBDINPUT(0, ord(ch), flags, 0, None)
            ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    return f"typed {len(text)} characters"


def parse_hotkey(keys: str) -> list[str] | str:
    parts = [MODIFIER_NAMES.get(p, p) for p in re.split(r"\s*\+\s*", keys.lower().strip()) if p]
    unknown = [p for p in parts if p not in VK]
    if unknown:
        return f"error: unknown key(s): {', '.join(unknown)}"
    if tuple(sorted(parts)) in {tuple(sorted(c)) for c in BLOCKED_COMBOS}:
        return f"error: {keys} is blocked for safety; do it yourself"
    return parts


def press_keys(keys: str) -> str:
    """e.g. "ctrl+s", "ctrl+shift+t", "win+d", "alt+tab", "enter"."""
    if err := _need_windows():
        return err
    parts = parse_hotkey(keys)
    if isinstance(parts, str):
        return parts
    user32 = ctypes.windll.user32
    for p in parts:
        user32.keybd_event(VK[p], 0, 0, 0)
    time.sleep(0.05)
    for p in reversed(parts):
        user32.keybd_event(VK[p], 0, 2, 0)
    return f"pressed {'+'.join(parts)}"


def clipboard(action: str, text: str | None = None) -> str:
    """action: read | write"""
    if err := _need_windows():
        return err
    if action == "read":
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        return out.strip()[:3000] or "(clipboard is empty)"
    if action == "write" and text is not None:
        subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                       input=text, text=True, timeout=10, creationflags=NO_WINDOW)
        return "copied to clipboard"
    return "error: use action read, or write with text"
