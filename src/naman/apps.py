"""Find and open laptop apps by the name a person would say ("open VS Code").

On Windows the catalogue comes from Get-StartApps: every Start Menu app, including
Store apps like Calculator and Settings, with an id that `shell:AppsFolder` launches.
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

# What people say -> the Start Menu name.
ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code": "visual studio code",
    "visual studio code": "visual studio code",
    "chrome": "google chrome",
    "browser": "google chrome",
    "edge": "microsoft edge",
    "explorer": "file explorer",
    "files": "file explorer",
    "my files": "file explorer",
    "calc": "calculator",
    "terminal": "terminal",
    "cmd": "command prompt",
    "command line": "command prompt",
    "ms word": "word",
}


@dataclass(frozen=True)
class App:
    name: str
    app_id: str


def _norm(text: str) -> str:
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    text = re.sub(r"^\s*(open|launch|start|run)\b", " ", text)  # "open vs code" -> "vs code"
    text = re.sub(r"\b(the|app|application|please)\b", " ", text)
    return " ".join(text.split())


@lru_cache(maxsize=1)
def installed_apps() -> tuple[App, ...]:
    if platform.system() != "Windows":
        return ()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        rows = json.loads(out or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ()
    rows = [rows] if isinstance(rows, dict) else rows
    return tuple(App(r["Name"], r["AppID"]) for r in rows if r.get("Name") and r.get("AppID"))


def find_app(query: str, apps: tuple[App, ...] | None = None) -> App | None:
    apps = installed_apps() if apps is None else apps
    q = _norm(query)
    q = ALIASES.get(q, q)
    if not q or not apps:
        return None
    by_name = {}
    for app in apps:
        by_name.setdefault(_norm(app.name), app)  # first wins on duplicate names

    if q in by_name:
        return by_name[q]
    words = q.split()
    # Most to least specific; the shortest name wins within a tier.
    tiers = [
        lambda n: n.startswith(q + " "),  # "spotify" -> "spotify premium"
        lambda n: all(re.search(rf"\b{re.escape(w)}\b", n) for w in words),  # whole words: "word" -> "... word 2007"
        lambda n: n.startswith(q),  # "calc" -> "calculator"
        lambda n: all(re.search(rf"\b{re.escape(w)}", n) for w in words),  # word prefixes
    ]
    for matches in tiers:
        hits = [n for n in by_name if matches(n)]
        if hits:
            return by_name[min(hits, key=len)]
    close = difflib.get_close_matches(q, list(by_name), n=1, cutoff=0.75)
    return by_name[close[0]] if close else None


def open_app(name: str) -> str:
    """Opens the app and returns once its window is up, so the next step can use it.
    An app that is already open is brought to the front instead of opened twice."""
    system = platform.system()
    if system == "Windows":
        from .skills.system import app_window, bring_to_front, wait_for_window, window_handles

        app = find_app(name)
        if app is not None:
            hwnd = app_window(app.name)
            if hwnd:
                bring_to_front(hwnd)
                return f"{app.name} was already open; brought it to the front"
            before = window_handles()
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app.app_id}"])
            ready = wait_for_window(app.name, before)
            return f"opened {app.name}" + ("" if ready else " (its window hasn't appeared yet)")
        exe = shutil.which(name)
        if exe:
            os.startfile(exe)
            return f"opened {exe}"
        suggestions = difflib.get_close_matches(_norm(name), [_norm(a.name) for a in installed_apps()], n=3, cutoff=0.4)
        hint = f" Closest installed apps: {', '.join(suggestions)}." if suggestions else ""
        return f"error: no installed app matches {name!r}.{hint}"
    if system == "Darwin":
        result = subprocess.run(["open", "-a", name], capture_output=True, text=True)
        return f"opened {name}" if result.returncode == 0 else f"error: {result.stderr.strip()}"
    exe = shutil.which(name)
    if not exe:
        return f"error: no app named {name!r} on PATH"
    subprocess.Popen([exe])
    return f"opened {name}"
