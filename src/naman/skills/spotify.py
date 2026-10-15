"""Play music in Spotify: open its search page for the query and press Play on the top result.

No keystrokes: in Spotify a key that lands in the wrong place is a shortcut (space =
play/pause), and Chromium's focus reporting is too unreliable to check first. Instead:

- Spotify web app (installed from Chrome/Edge): launch it straight at
  open.spotify.com/search/<query> with the Start Menu shortcut's own command, then
  close the older Spotify window so they don't pile up.
- Spotify desktop app: open the spotify:search:<query> link, which it handles in place.

Both expose the same accessible names: the top result's Play button is the first
button in the "Search results" grid, and the player's Play button turning into "Pause"
confirms playback.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from . import uia
from .system import BROWSERS, NO_WINDOW, WINDOWS, _norm, _windows, process_name, wait_for_window, window_handles

RESULTS = "Search results"
PLAYER = "Player controls"
WM_CLOSE = 0x0010


def spotify_windows() -> list[int]:
    found = []
    for hwnd, title in _windows():
        t = _norm(title)
        if process_name(hwnd) == "spotify.exe":  # desktop app: the title is the song while playing
            found.append(hwnd)
        elif t.startswith("spotify") and not any(t.endswith(b) for b in BROWSERS):  # web app
            found.append(hwnd)
    return found


def spotify_window() -> int | None:
    wins = spotify_windows()
    return wins[0] if wins else None


@lru_cache(maxsize=1)
def web_app_command() -> str | None:
    """The command line of the Start Menu's Spotify web-app shortcut, e.g.
    chrome_proxy.exe --profile-directory="Profile 1" --app-id=...; None if there is none."""
    dirs = [Path(os.environ.get("APPDATA", "")), Path(os.environ.get("PROGRAMDATA", ""))]
    links = [p for d in dirs for p in (d / "Microsoft/Windows/Start Menu/Programs").rglob("Spotify*.lnk")]
    for link in links:
        script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
                  "@{t=$s.TargetPath;a=$s.Arguments} | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                 timeout=15, creationflags=NO_WINDOW).stdout
            info = json.loads(out or "{}")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
        if "--app-id=" in (info.get("a") or ""):
            return f'"{info["t"]}" {info["a"]}'
    return None


def _results_button(root):
    grid = uia.find(root, RESULTS)
    button = grid and uia.find(grid, control=uia.BUTTON)
    if not button:
        return None, ""
    row = uia.find(grid, control=uia.DATA_ITEM)
    return button, uia.name_of(row) if row else ""


def _playing(root) -> bool:
    player = uia.find(root, PLAYER)
    return bool(player) and "Pause" in uia.children_names(player)


def _search_desktop(hwnd: int, query: str) -> tuple[object, str] | None:
    root = uia.element(hwnd)
    _, before = _results_button(root)
    os.startfile(f"spotify:search:{quote(query)}")
    # The results refresh in place; wait for the top row to change (or just appear).
    uia.wait_for(lambda: (top := _results_button(root)[1]) and top != before, 8, 0.2)
    return root, "desktop"


def _search_web(query: str) -> tuple[object, int | None] | str:
    command = web_app_command()
    if command is None:
        return "error: Spotify isn't installed (no Spotify app in the Start Menu). Use play_youtube instead."
    url = f"https://open.spotify.com/search/{quote(query)}"
    before = window_handles()
    subprocess.Popen(f"{command} --app-launch-url-for-shortcuts-menu-item={url}")
    wait_for_window("spotify", before, timeout=15)
    hwnd = uia.wait_for(lambda: next((h for h in spotify_windows() if h not in before), None), 5)
    if not hwnd:
        return "error: Spotify didn't open (is it still loading?)"
    return uia.element(hwnd), hwnd


def play(query: str) -> str:
    if not WINDOWS:
        return "error: Spotify control only works on Windows for now"
    query = query.strip()
    if not query:
        return "error: say what to play"
    older = spotify_windows()
    desktop = next((h for h in older if process_name(h) == "spotify.exe"), None)
    new_window = None
    if desktop:
        root, _ = _search_desktop(desktop, query)
    else:
        opened = _search_web(query)
        if isinstance(opened, str):
            return opened
        root, new_window = opened

    # A fresh page builds its accessibility tree lazily, so keep asking.
    found = uia.wait_for(lambda: (r := _results_button(root))[0] and r, 15, 0.25)
    if not found:
        return f"error: Spotify showed no results for {query!r} (not logged in, or offline?)"
    button, top = found
    if uia.name_of(button).startswith("Pause"):
        result = f"'{top}' is already playing on Spotify"
    else:
        uia.invoke(button)
        started = uia.wait_for(lambda: _playing(root), 10, 0.25)  # the web player can take a few seconds
        result = (f"playing '{top}' on Spotify (top result for {query!r})" if started
                  else f"pressed play on '{top}' in Spotify, but couldn't confirm it started")
    if new_window:
        import ctypes

        for hwnd in older:  # the new window has taken over; don't leave a pile of Spotify windows
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    return result
