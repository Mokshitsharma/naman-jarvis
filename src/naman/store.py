"""Long-term storage: memories about the user and their work tasks, in one SQLite file."""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    fact TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    project TEXT NOT NULL,
    category TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    due TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    created TEXT NOT NULL,
    completed TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    message TEXT NOT NULL,
    due TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routines (
    name TEXT PRIMARY KEY,
    steps TEXT NOT NULL,
    updated TEXT NOT NULL
);
"""

# Words too common to say which memories matter for a message.
STOPWORDS = {"the", "and", "for", "you", "your", "are", "was", "with", "that", "this", "what", "who", "how", "can",
             "please", "about", "from", "have", "has", "his", "her", "him", "they", "them", "then", "and", "not",
             "voice", "jarvis", "naman", "tell", "give", "some", "any", "all", "its", "into", "out", "now", "just",
             "want", "need", "like", "does", "did", "will", "would", "should", "could", "there", "here", "when"}

def _stem(word: str) -> str:
    """'internships' -> 'internship', 'companies' -> 'compan': enough for LIKE matching."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3]
    if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


PRIORITY_ORDER = "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"


class Store:
    def __init__(self, path: Path | str, profile_path: Path | str | None = None) -> None:
        """`profile_path`: a Markdown file about the user, read into every conversation.
        Defaults to profile.md next to the database."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        default = Path(path).parent / "profile.md" if str(path) != ":memory:" else None
        self.profile_path = Path(profile_path) if profile_path else default
        # Telegram handlers and the CLI may touch the store from different threads.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self.lock, self.db:
            return self.db.execute(sql, args).lastrowid or 0

    # --- memories ------------------------------------------------------------------------

    def remember(self, fact: str) -> int:
        existing = self._rows("SELECT id FROM memories WHERE lower(fact) = lower(?)", (fact.strip(),))
        if existing:
            return existing[0]["id"]
        return self._write("INSERT INTO memories (fact, created) VALUES (?, ?)", (fact.strip(), _now()))

    def forget(self, memory_id: int) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def search_memories(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        if not words:
            return self.recent_memories(limit)
        clause = " OR ".join("lower(fact) LIKE ?" for _ in words)
        return self._rows(
            f"SELECT id, fact, created FROM memories WHERE {clause} ORDER BY id DESC LIMIT ?",
            (*[f"%{w}%" for w in words], limit),
        )

    def relevant_memories(self, message: str, limit: int = 12) -> list[dict[str, Any]]:
        """Memories that share the most words with `message`, best first."""
        words = {_stem(w) for w in re.findall(r"\w+", message.lower()) if len(w) > 2 and w not in STOPWORDS}
        if not words:
            return []
        clause = " OR ".join("lower(fact) LIKE ?" for _ in words)
        rows = self._rows(f"SELECT id, fact, created FROM memories WHERE {clause}", tuple(f"%{w}%" for w in words))
        score = lambda r: sum(w in r["fact"].lower() for w in words)  # noqa: E731
        return sorted(rows, key=lambda r: (-score(r), -r["id"]))[:limit]

    def profile(self) -> str:
        """The user's profile (Markdown), or "" if there is none."""
        try:
            return self.profile_path.read_text(encoding="utf-8").strip() if self.profile_path else ""
        except OSError:
            return ""

    def recent_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT id, fact, created FROM memories ORDER BY id DESC LIMIT ?", (limit,))

    # --- tasks ---------------------------------------------------------------------------

    def add_task(self, title: str, project: str, category: str, priority: str, due: str | None) -> int:
        if due:
            date.fromisoformat(due)  # raises ValueError on a bad date
        return self._write(
            "INSERT INTO tasks (title, project, category, priority, due, created) VALUES (?, ?, ?, ?, ?, ?)",
            (title.strip(), project.strip(), category.strip().lower(), priority, due or None, _now()),
        )

    def complete_task(self, task_id: int) -> bool:
        with self.lock, self.db:
            cur = self.db.execute(
                "UPDATE tasks SET status = 'done', completed = ? WHERE id = ? AND status = 'open'",
                (_now(), task_id),
            )
            return cur.rowcount > 0

    def list_tasks(self, project: str | None = None, status: str = "open") -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE status = ?"
        args: list[Any] = [status]
        if project:
            sql += " AND lower(project) = lower(?)"
            args.append(project)
        sql += f" ORDER BY {PRIORITY_ORDER}, due IS NULL, due, id"
        return self._rows(sql, tuple(args))

    def projects(self) -> list[str]:
        return [r["project"] for r in self._rows("SELECT DISTINCT project FROM tasks ORDER BY project")]

    def briefing(self, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        open_tasks = self.list_tasks()
        overdue = [t for t in open_tasks if t["due"] and t["due"] < today.isoformat()]
        due_today = [t for t in open_tasks if t["due"] == today.isoformat()]
        by_project: dict[str, int] = {}
        for t in open_tasks:
            by_project[t["project"]] = by_project.get(t["project"], 0) + 1
        done_recently = self._rows(
            "SELECT title, project FROM tasks WHERE status = 'done' AND completed >= ? ORDER BY completed DESC",
            (today.isoformat(),),
        )
        return {
            "date": today.isoformat(),
            "open": len(open_tasks),
            "overdue": overdue,
            "due_today": due_today,
            "high_priority": [t for t in open_tasks if t["priority"] == "high"][:5],
            "open_by_project": by_project,
            "done_today": done_recently,
        }


    # --- reminders -----------------------------------------------------------------------

    def add_reminder(self, message: str, due: datetime) -> int:
        return self._write("INSERT INTO reminders (message, due) VALUES (?, ?)",
                           (message.strip(), due.isoformat(timespec="seconds")))

    def pending_reminders(self) -> list[dict[str, Any]]:
        return self._rows("SELECT id, message, due FROM reminders WHERE fired = 0 ORDER BY due")

    def due_reminders(self, now: datetime) -> list[dict[str, Any]]:
        return self._rows("SELECT id, message, due FROM reminders WHERE fired = 0 AND due <= ? ORDER BY due",
                          (now.isoformat(timespec="seconds"),))

    def mark_fired(self, reminder_id: int) -> None:
        self._write("UPDATE reminders SET fired = 1 WHERE id = ?", (reminder_id,))

    def cancel_reminder(self, reminder_id: int) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM reminders WHERE id = ? AND fired = 0", (reminder_id,)).rowcount > 0

    # --- routines ------------------------------------------------------------------------

    def save_routine(self, name: str, steps: str) -> None:
        self._write("INSERT INTO routines (name, steps, updated) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET steps = excluded.steps, updated = excluded.updated",
                    (name.strip().lower(), steps.strip(), _now()))

    def get_routine(self, name: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT name, steps FROM routines WHERE name = ?", (name.strip().lower(),))
        return rows[0] if rows else None

    def routines(self) -> list[dict[str, Any]]:
        return self._rows("SELECT name, steps FROM routines ORDER BY name")

    def delete_routine(self, name: str) -> bool:
        with self.lock, self.db:
            return self.db.execute("DELETE FROM routines WHERE name = ?", (name.strip().lower(),)).rowcount > 0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")
