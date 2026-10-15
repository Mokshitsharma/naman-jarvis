"""Start Naman when you log in to Windows.

Puts a shortcut in your Startup folder that runs `pythonw -m naman start` (no console
window) from the project folder, so .env and data/ are found. Delete the shortcut, or
run `naman startup off`, to stop it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "Naman.lnk"

# Paths travel in environment variables, never inside the script: no quoting or injection issues.
MAKE_SHORTCUT = r"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:NAMAN_LNK)
$s.TargetPath = $env:NAMAN_TARGET
$s.Arguments = '-m naman start'
$s.WorkingDirectory = $env:NAMAN_DIR
$s.Description = 'Naman, your assistant. Say "Jarvis".'
$s.WindowStyle = 7
$s.Save()
"""


def startup_folder() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path:
    return startup_folder() / SHORTCUT_NAME


def project_dir() -> Path:
    """The folder with .env and data/: the source checkout for an editable install."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").exists() else Path.cwd()


def windowless_python() -> Path:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else exe


def is_enabled() -> bool:
    return sys.platform == "win32" and shortcut_path().exists()


def enable() -> Path:
    if sys.platform != "win32":
        raise RuntimeError("starting with the computer is only set up for Windows")
    path = shortcut_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "NAMAN_LNK": str(path), "NAMAN_TARGET": str(windowless_python()),
           "NAMAN_DIR": str(project_dir())}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", MAKE_SHORTCUT], env=env,
                   check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path


def disable() -> bool:
    try:
        shortcut_path().unlink()
        return True
    except FileNotFoundError:
        return False
