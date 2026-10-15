"""Timers and reminders that fire while Naman is running (chat, voice or Telegram).

Every reminder shows a Windows notification with a chime; front ends add their own
channel too (voice speaks it, Telegram messages you). Reminders are stored in SQLite,
so ones that came due while Naman was off fire as soon as it starts again.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from .store import Store

CHECK_EVERY_S = 5

TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $xml.GetElementsByTagName('text')
$t.Item(0).AppendChild($xml.CreateTextNode('Naman')) > $null
$t.Item(1).AppendChild($xml.CreateTextNode($env:NAMAN_TOAST)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def windows_toast(message: str) -> None:
    if platform.system() != "Windows":
        return
    try:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass
    # The text travels in an environment variable, never inside the script: no injection.
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", TOAST_SCRIPT],
        env={**os.environ, "NAMAN_TOAST": message[:250]},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def resolve_due(at: str | None, in_minutes: float | None, now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    if in_minutes is not None:
        if in_minutes <= 0:
            raise ValueError("in_minutes must be positive")
        return now + timedelta(minutes=float(in_minutes))
    if at:
        due = datetime.fromisoformat(at)
        if due.tzinfo is not None:
            due = due.astimezone().replace(tzinfo=None)
        if due <= now:
            raise ValueError(f"{at} is in the past")
        return due
    raise ValueError("give either `at` (YYYY-MM-DDTHH:MM) or `in_minutes`")


class Scheduler:
    def __init__(self, store: Store, notify_desktop: bool = True) -> None:
        self.store = store
        self.listeners: list[Callable[[str], None]] = [windows_toast] if notify_desktop else []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="reminders")

    def add_listener(self, fn: Callable[[str], None]) -> None:
        self.listeners.append(fn)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: datetime | None = None) -> list[str]:
        fired = []
        for r in self.store.due_reminders(now or datetime.now()):
            self.store.mark_fired(r["id"])  # mark first: a failing listener must not repeat it forever
            text = f"Reminder: {r['message']}"
            for fn in self.listeners:
                try:
                    fn(text)
                except Exception:
                    pass
            fired.append(text)
        return fired

    def _run(self) -> None:
        while not self._stop.wait(CHECK_EVERY_S):
            self.tick()
