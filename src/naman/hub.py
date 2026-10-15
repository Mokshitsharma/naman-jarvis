"""The data behind the control center: jobs, posts worth reading, the weekly schedule,
Claude Code runs and automation bookkeeping. Same SQLite file and lock as Store."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    work_mode TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    posted TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    experience TEXT NOT NULL DEFAULT '',
    skills TEXT NOT NULL DEFAULT '[]',
    salary TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    fit INTEGER NOT NULL DEFAULT 0,
    fit_reasons TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'saved', 'applied', 'hidden')),
    fetched TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS jobs_dedupe ON jobs (dedupe_key);
CREATE TABLE IF NOT EXISTS feed (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'saved', 'hidden')),
    fetched TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY,
    days TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    project_dir TEXT NOT NULL,
    instructions TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    summary TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    started TEXT NOT NULL,
    finished TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_GROUPS = {"weekdays": DAYS[:5], "weekends": DAYS[5:], "daily": DAYS, "everyday": DAYS, "all": DAYS}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dedupe_key(title: str, company: str) -> str:
    """The same job is often posted on several boards: match on title + company."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return f"{norm(title)}|{norm(company)}"


def parse_days(days: str) -> list[str]:
    """'weekdays', 'mon-fri', 'mon,wed,fri', 'Saturday' -> ['mon', ...]."""
    text = days.lower().replace(" ", "")
    if text in DAY_GROUPS:
        return DAY_GROUPS[text]
    out: list[str] = []
    for part in re.split(r"[,/&]|and", text):
        if not part:
            continue
        if "-" in part:
            a, b = (p[:3] for p in part.split("-", 1))
            if a in DAYS and b in DAYS:
                i, j = DAYS.index(a), DAYS.index(b)
                out += DAYS[i:j + 1] if i <= j else DAYS[i:] + DAYS[:j + 1]
                continue
        if part in DAY_GROUPS:
            out += DAY_GROUPS[part]
        elif part[:3] in DAYS:
            out.append(part[:3])
        else:
            raise ValueError(f"unknown day {part!r}: use mon..sun, 'weekdays', 'weekends' or 'daily'")
    return list(dict.fromkeys(out))


def _hhmm(value: str) -> str:
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value.strip().lower())
    if not m:
        raise ValueError(f"time {value!r}: use HH:MM, e.g. 09:30 or 18:00")
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h < 24 and 0 <= mins < 60):
        raise ValueError(f"time {value!r} is out of range")
    return f"{h:02d}:{mins:02d}"


class Hub:
    def __init__(self, store: Store) -> None:
        self.store = store
        with store.lock:
            store.db.executescript(SCHEMA)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return self.store._rows(sql, args)

    def _write(self, sql: str, args: tuple = ()) -> int:
        return self.store._write(sql, args)

    def _change(self, sql: str, args: tuple = ()) -> bool:
        with self.store.lock, self.store.db:
            return self.store.db.execute(sql, args).rowcount > 0

    # --- jobs ----------------------------------------------------------------------------

    def has_job(self, source: str, external_id: str, title: str = "", company: str = "") -> bool:
        return bool(self._rows("SELECT 1 FROM jobs WHERE (source = ? AND external_id = ?) OR dedupe_key = ? LIMIT 1",
                               (source, external_id, dedupe_key(title, company) if title else "\x00")))

    def add_job(self, job: dict[str, Any]) -> int | None:
        """Stores a job; None if it's already known (same posting, or same title at the same company)."""
        if self.has_job(job["source"], job["external_id"], job["title"], job.get("company", "")):
            return None
        fields = ["source", "external_id", "title", "company", "location", "work_mode", "url", "posted",
                  "description", "summary", "experience", "salary", "contact", "fit"]
        values = [job.get(f, "" if f != "fit" else 0) for f in fields]
        values += [json.dumps(job.get("skills", [])), json.dumps(job.get("fit_reasons", [])),
                   dedupe_key(job["title"], job.get("company", "")), _now()]
        cols = ", ".join(fields + ["skills", "fit_reasons", "dedupe_key", "fetched"])
        return self._write(f"INSERT OR IGNORE INTO jobs ({cols}) VALUES ({', '.join('?' * len(values))})",
                           tuple(values)) or None

    def jobs(self, status: str | None = None, min_fit: int = 0, query: str = "", limit: int = 300,
             include_description: bool = True, where: str = "", max_age_days: int | None = 7) -> list[dict[str, Any]]:
        """Best fit first. Jobs posted more than `max_age_days` ago are left out unless you
        saved or applied to them (None: everything)."""
        cols = "*" if include_description else ("id, source, title, company, location, work_mode, url, posted, summary, "
                                                  "experience, skills, salary, contact, fit, fit_reasons, status, fetched")
        sql, args = f"SELECT {cols} FROM jobs WHERE fit >= ?", [min_fit]
        if status:
            sql += " AND status = ?"
            args.append(status)
        else:
            sql += " AND status != 'hidden'"
        if query:
            sql += " AND (lower(title) LIKE ? OR lower(company) LIKE ? OR lower(skills) LIKE ? OR lower(location) LIKE ?)"
            args += [f"%{query.lower()}%"] * 4
        if where in {"home", "remote", "freelance", "internship"}:
            sql += " AND fit_reasons LIKE ?"  # the bonus reason ("remote +10") marks them
            args.append(f'%"{where} +%')
        if max_age_days is not None:
            cutoff = (datetime.now() - timedelta(days=max_age_days)).date().isoformat()
            sql += (" AND (status IN ('saved', 'applied') OR "
                    "substr(CASE WHEN posted != '' THEN posted ELSE fetched END, 1, 10) >= ?)")
            args.append(cutoff)
        sql += " ORDER BY fit DESC, fetched DESC LIMIT ?"
        args.append(limit)
        rows = self._rows(sql, tuple(args))
        for r in rows:
            r["skills"], r["fit_reasons"] = json.loads(r["skills"]), json.loads(r["fit_reasons"])
        return rows

    def job(self, job_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            return None
        rows[0]["skills"], rows[0]["fit_reasons"] = json.loads(rows[0]["skills"]), json.loads(rows[0]["fit_reasons"])
        return rows[0]

    def update_job(self, job_id: int, fields: dict[str, Any]) -> None:
        allowed = {"title", "company", "location", "summary", "experience", "skills", "salary", "contact",
                   "work_mode", "fit", "fit_reasons"}
        values = {k: json.dumps(v) if k in {"skills", "fit_reasons"} else v for k, v in fields.items() if k in allowed}
        if values:
            sets = ", ".join(f"{k} = ?" for k in values)
            self._write(f"UPDATE jobs SET {sets} WHERE id = ?", (*values.values(), job_id))

    def set_job_status(self, job_id: int, status: str) -> bool:
        if status not in {"new", "saved", "applied", "hidden"}:
            raise ValueError("status must be new, saved, applied or hidden")
        return self._change("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))

    # --- feed ----------------------------------------------------------------------------

    def add_post(self, item: dict[str, Any]) -> int | None:
        return self._write("INSERT OR IGNORE INTO feed (source, url, title, snippet, topic, score, fetched) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (item["source"], item["url"], item["title"], item.get("snippet", ""), item.get("topic", ""),
                            int(item.get("score", 0)), _now())) or None

    def posts(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            return self._rows("SELECT * FROM feed WHERE status = ? ORDER BY fetched DESC, score DESC LIMIT ?",
                              (status, limit))
        return self._rows("SELECT * FROM feed WHERE status != 'hidden' ORDER BY fetched DESC, score DESC LIMIT ?",
                          (limit,))

    def set_post_status(self, post_id: int, status: str) -> bool:
        if status not in {"new", "saved", "hidden"}:
            raise ValueError("status must be new, saved or hidden")
        return self._change("UPDATE feed SET status = ? WHERE id = ?", (status, post_id))

    # --- schedule ------------------------------------------------------------------------

    def add_block(self, days: str, start: str, end: str, title: str) -> int:
        day_list = parse_days(days)
        start, end = _hhmm(start), _hhmm(end)
        if end <= start:
            raise ValueError("end must be after start (blocks can't cross midnight; split them)")
        return self._write("INSERT INTO schedule (days, start, end, title) VALUES (?, ?, ?, ?)",
                           (",".join(day_list), start, end, title.strip()))

    def blocks(self, day: str | None = None) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM schedule ORDER BY start")
        return [r for r in rows if day is None or day in r["days"].split(",")]

    def delete_block(self, block_id: int) -> bool:
        return self._change("DELETE FROM schedule WHERE id = ?", (block_id,))

    def clear_schedule(self) -> int:
        with self.store.lock, self.store.db:
            return self.store.db.execute("DELETE FROM schedule").rowcount

    def now_and_next(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now()
        day, hhmm = DAYS[now.weekday()], now.strftime("%H:%M")
        today = self.blocks(day)
        current = next((b for b in today if b["start"] <= hhmm < b["end"]), None)
        upcoming = [b for b in today if b["start"] > hhmm]
        return {"day": day, "time": hhmm, "now": current, "next": upcoming[0] if upcoming else None,
                "rest_of_today": upcoming}

    # --- Claude Code runs ----------------------------------------------------------------

    def add_run(self, project_dir: str, instructions: str, log_path: str = "") -> int:
        return self._write("INSERT INTO runs (project_dir, instructions, log_path, started) VALUES (?, ?, ?, ?)",
                           (project_dir, instructions, log_path, _now()))

    def set_run_log(self, run_id: int, log_path: str) -> None:
        self._write("UPDATE runs SET log_path = ? WHERE id = ?", (log_path, run_id))

    def finish_run(self, run_id: int, ok: bool, summary: str) -> None:
        self._write("UPDATE runs SET status = ?, summary = ?, finished = ? WHERE id = ?",
                    ("done" if ok else "failed", summary, _now(), run_id))

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def run(self, run_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM runs WHERE id = ?", (run_id,))
        return rows[0] if rows else None

    # --- small key/value state (last automation runs, etc.) --------------------------------

    def get(self, key: str, default: str = "") -> str:
        rows = self._rows("SELECT value FROM state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def put(self, key: str, value: str) -> None:
        self._write("INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value))
