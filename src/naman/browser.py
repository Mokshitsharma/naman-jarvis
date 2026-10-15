"""Web pages open in Chrome, in your signed-in profile, not Windows' default browser.

`use_chrome()` registers Chrome as Python's preferred browser once at start-up, so
every `webbrowser.open()` in Naman (links, Google/YouTube searches, songs) goes to
Chrome. Links that aren't web pages (whatsapp://, spotify:) still go to their own apps.

NAMAN_BROWSER=default in .env goes back to Windows' default browser;
NAMAN_CHROME_PROFILE picks the Chrome profile (default: the one you used last).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

CHROME_PATHS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")]


def chrome_path() -> str | None:
    return next((p for p in CHROME_PATHS if Path(p).exists()), None) or shutil.which("chrome")


def chrome_profile() -> str:
    """The profile Chrome used last ("Profile 1"): that's where you're signed in."""
    if os.environ.get("NAMAN_CHROME_PROFILE"):
        return os.environ["NAMAN_CHROME_PROFILE"]
    state = Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State"))
    try:
        return json.loads(state.read_text(encoding="utf-8"))["profile"]["last_used"] or "Default"
    except (OSError, ValueError, KeyError, TypeError):
        return "Default"


def chrome_command(*args: str) -> list[str] | None:
    exe = chrome_path()
    return [exe, f"--profile-directory={chrome_profile()}", *args] if exe else None


class Chrome(webbrowser.BaseBrowser):
    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        if not url.lower().startswith(("http://", "https://")):
            os.startfile(url)  # whatsapp://, mailto: ... belong to their own apps
            return True
        cmd = chrome_command(url)
        if cmd is None:
            return False
        subprocess.Popen(cmd)
        return True


def use_chrome() -> bool:
    """Makes webbrowser.open() use Chrome. False when Chrome isn't installed or it's turned off."""
    if os.environ.get("NAMAN_BROWSER", "chrome").lower() != "chrome" or chrome_path() is None:
        return False
    webbrowser.register("chrome", None, Chrome("chrome"), preferred=True)
    return True


def open_app_window(url: str, size: str = "1400,900") -> bool:
    """A page as its own window (no tabs, no address bar), like an installed app."""
    cmd = chrome_command(f"--app={url}", f"--window-size={size}")
    if cmd is None:
        return False
    subprocess.Popen(cmd)
    return True
