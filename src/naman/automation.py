"""Work Naman does on its own while it runs: job and post searches, a morning briefing,
an evening check-in, and, between them, small live updates: a nudge about open tasks
every few hours and news from your GitHub. Times are in data/automation.json; the live
updates keep quiet during "quiet_hours".

Each job runs once per slot per day. A slot that passed while the laptop was off runs
as soon as Naman starts (after 02:00 the night's job search runs at 07:00 if the laptop
was asleep at 2). Results go to `announce` (Telegram, the orb's text), never spoken:
nobody wants a voice at 2 am.
"""

from __future__ import annotations

import json
import threading
import traceback
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import feed, jobs
from .hub import DAYS, Hub

DEFAULT_CONFIG = {
    "jobs_refresh": ["02:00", "10:00", "14:00", "18:00"],
    "feed_refresh": [],  # the Posts feed: off (the control center no longer shows it)
    "tech_news": ["07:30", "13:00", "19:30"],
    "morning_brief": ["08:00"],
    "evening_checkin": ["21:30"],
    "catch_up_hours": 10,  # a missed slot older than this is skipped, not run late
    "task_nudge_every_h": 3,  # "You have 10 tasks not done", at most this often; 0 = off
    "github_user": "",  # whose public GitHub to watch for stars, forks, issues, followers
    "github_every_min": 15,
    "quiet_hours": ["22:30", "08:00"],  # no live updates in between
}
CHECK_EVERY_S = 30
RETRY_MIN = 15  # a search that reached nothing (offline, throttled) is tried again this much later


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        return {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    except ValueError:
        return dict(DEFAULT_CONFIG)


def due_slots(config: dict[str, Any], done: Callable[[str], bool], now: datetime) -> list[tuple[str, str]]:
    """(job, slot key) pairs whose time today has passed, not run yet, not too stale."""
    out = []
    for job in ("jobs_refresh", "feed_refresh", "tech_news", "morning_brief", "evening_checkin"):
        for hhmm in config.get(job, []):
            h, m = map(int, hhmm.split(":"))
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            key = f"auto:{job}:{now.date().isoformat()}:{hhmm}"
            if at <= now < at + timedelta(hours=config["catch_up_hours"]) and not done(key):
                out.append((job, key))
    return out


def morning_brief(hub: Hub, name: str = "", today: date | None = None) -> str:
    today = today or date.today()
    brief = hub.store.briefing(today)
    lines = [f"Good morning{', ' + name if name else ''}. {today:%A %d %B}."]
    day = DAYS[today.weekday()]
    blocks = hub.blocks(day)
    if blocks:
        lines.append("Today: " + "; ".join(f"{b['start']}-{b['end']} {b['title']}" for b in blocks))
    if brief["overdue"]:
        lines.append("Overdue: " + "; ".join(t["title"] for t in brief["overdue"][:5]))
    if brief["due_today"]:
        lines.append("Due today: " + "; ".join(t["title"] for t in brief["due_today"][:5]))
    top = [t for t in brief["high_priority"] if t not in brief["due_today"]][:3]
    if top:
        lines.append("High priority: " + "; ".join(t["title"] for t in top))
    since = (datetime.combine(today, datetime.min.time()) - timedelta(hours=12)).isoformat()
    fresh = [j for j in hub.jobs(status="new", min_fit=55, limit=50) if j["fetched"] >= since][:5]
    if fresh:
        lines.append("New jobs worth a look:")
        lines += [f"- [{j['fit']}] {j['title']} at {j['company']} ({j['location']}) {j['url']}" for j in fresh]
    done = [r for r in hub.runs(5) if r["finished"] and r["finished"] >= since]
    for r in done:
        lines.append(f"Claude Code build #{r['id']} {r['status']}: {r['summary'].splitlines()[0][:200] if r['summary'] else ''}")
    if len(lines) == 1:
        lines.append("Nothing due today. A good day to pick something from your list.")
    return "\n".join(lines)


def in_quiet_hours(config: dict[str, Any], now: datetime) -> bool:
    start, end = config.get("quiet_hours") or ["00:00", "00:00"]
    hhmm = now.strftime("%H:%M")
    return (start <= hhmm or hhmm < end) if start > end else start <= hhmm < end


def task_nudge(hub: Hub, today: date | None = None) -> str:
    """"You have 10 tasks not done (2 overdue). Next up: ..." or "" with nothing open."""
    today = today or date.today()
    brief = hub.store.briefing(today)
    if not brief["open"]:
        return ""
    overdue, due = len(brief["overdue"]), len(brief["due_today"])
    extra = ", ".join(f"{n} {what}" for n, what in ((overdue, "overdue"), (due, "due today")) if n)
    text = f"You have {brief['open']} task{'s' * (brief['open'] != 1)} not done{f' ({extra})' if extra else ''}."
    nxt = hub.store.list_tasks()[0]
    when = f", due {date.fromisoformat(nxt['due']):%a %d %b}" if nxt["due"] else ""
    return text + f"\nNext up: {nxt['title']}{when}."


def evening_checkin(hub: Hub) -> str:
    open_tasks = hub.store.list_tasks()[:6]
    text = "Evening check-in: what did you get done today? Tell me and I'll tick it off."
    if open_tasks:
        text += "\nStill open:\n" + "\n".join(f"- #{t['id']} {t['title']} ({t['project']})" for t in open_tasks)
    return text


class Automation:
    def __init__(self, hub: Hub, data_dir: Path, announce: Callable[[str], None], user_name: str = "",
                 log: Callable[[str], None] = print, outreach: Any = None) -> None:
        self.hub, self.data_dir, self.announce, self.user_name, self.log = hub, data_dir, announce, user_name, log
        self.outreach = outreach  # outreach.Outreach: drafts application emails after each search
        self.progress = ""  # what the running background job is doing, for the control center
        self.last_result: dict[str, Any] = {}
        self.config_path = data_dir / "automation.json"
        self._stop = threading.Event()
        self._busy = threading.Lock()  # one background job at a time
        self._thread = threading.Thread(target=self._run, daemon=True, name="automation")

    def start(self) -> None:
        load_config(self.config_path)  # writes the defaults on first run
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # Callable from tools and the control center too ("refresh jobs now").
    def refresh_jobs(self, notify: bool = True) -> dict[str, Any]:
        with self._busy:
            llm = jobs.groq_client()
            self.progress = "Starting the job search"
            result: dict[str, Any] = {}
            try:
                result = jobs.refresh(self.hub, self.data_dir / "jobs.json", llm, log=self.log,
                                      progress=lambda text: setattr(self, "progress", text))
                self.progress = "Re-checking jobs the AI skipped earlier"
            finally:
                self.last_result = result
                if not result:
                    self.progress = ""
            jobs.rescore(self.hub, self.data_dir / "jobs.json", llm, log=self.log)  # retry any rate-limited ones
            from . import contacts

            self.progress = "Looking up company contacts for cold emailing"
            self._guard(lambda: contacts.lookup_batch(self.hub, log=self.log), "contacts")
            if self.outreach is not None and result["added"]:
                self.progress = "Drafting application emails"
                self._guard(self.outreach.auto_draft, "outreach")
            if self.outreach is not None:
                self.progress = "Auto-applying"
                self._guard(self.outreach.auto_send, "auto-apply")
            self.progress = ""
        self.hub.put("last:jobs_refresh", datetime.now().isoformat(timespec="seconds"))
        if notify and result["added"]:
            best = "\n".join(f"- [{j['fit']}] {j['title']} at {j['company']} {j['url']}" for j in result["best"][:3])
            self.announce(f"Found {result['added']} new job{'s' * (result['added'] != 1)}. Best matches:\n{best}")
        return result

    @property
    def tech(self) -> Any:
        if not hasattr(self, "_tech"):
            from .technews import TechNews

            self._tech = TechNews(self.hub, llm=jobs.groq_client, log=self.log)
        return self._tech

    def refresh_tech(self) -> dict[str, Any]:
        with self._busy:
            self.progress = "Reading tech news"
            try:
                return self.tech.refresh(self.announce)
            finally:
                self.progress = ""

    def refresh_feed(self) -> dict[str, Any]:
        with self._busy:
            self.progress = "Fetching posts"
            try:
                result = feed.refresh(self.hub, self.data_dir / "feed.json", log=self.log)
            finally:
                self.progress = ""
        self.hub.put("last:feed_refresh", datetime.now().isoformat(timespec="seconds"))
        return result

    def in_background(self, fn: Callable[[], Any], name: str) -> bool:
        """Runs fn on its own thread unless a background job is already running."""
        if self._busy.locked():
            return False
        threading.Thread(target=self._guard, args=(fn, name), daemon=True, name=name).start()
        return True

    def _guard(self, fn: Callable[[], Any], name: str) -> Any:
        try:
            return fn()
        except Exception:
            self.log(f"{name} failed:\n{traceback.format_exc()}")
            return None

    def tick(self, now: datetime | None = None) -> list[str]:
        config = load_config(self.config_path)
        now = now or datetime.now()
        ran = []
        for job, key in due_slots(config, lambda k: self._done(k, now), now):
            self.hub.put(key, now.isoformat(timespec="seconds"))  # first: never twice at once
            if job in ran:  # two missed slots of the same job (laptop was off): one run covers both
                continue
            ran.append(job)
            if job in ("jobs_refresh", "feed_refresh"):
                result = self._guard(self.refresh_jobs if job == "jobs_refresh" else self.refresh_feed, job)
                if not result or not result.get("seen"):
                    # Nothing reached: offline (Wi-Fi sleeps with the laptop) or throttled. Try again later
                    # instead of losing the night's search.
                    retry = now + timedelta(minutes=RETRY_MIN)
                    self.hub.put(key, f"retry:{retry.isoformat(timespec='seconds')}")
                    self.log(f"{job}: nothing reached; trying again at {retry:%H:%M}")
            elif job == "tech_news":
                self._guard(self.refresh_tech, job)
            elif job == "morning_brief":
                self.announce(morning_brief(self.hub, self.user_name))
            elif job == "evening_checkin":
                self.announce(evening_checkin(self.hub))
        if not in_quiet_hours(config, now):
            ran += self._guard(lambda: self.live_updates(config, now), "live updates") or []
        return ran

    def live_updates(self, config: dict[str, Any], now: datetime) -> list[str]:
        """The small stuff between the big jobs, each at most as often as configured."""
        ran = []
        every_h = float(config.get("task_nudge_every_h") or 0)
        if every_h and self._older_than("last:task_nudge", now, timedelta(hours=every_h)):
            self.hub.put("last:task_nudge", now.isoformat(timespec="seconds"))
            if text := task_nudge(self.hub, now.date()):
                self.announce(text)
                ran.append("task_nudge")
        user = config.get("github_user") or ""
        every_min = float(config.get("github_every_min") or 15)
        if user and self._older_than("last:github", now, timedelta(minutes=every_min)):
            self.hub.put("last:github", now.isoformat(timespec="seconds"))
            from . import github

            new = github.snapshot(user)
            old = json.loads(self.hub.get("github:snapshot") or "null")
            self.hub.put("github:snapshot", json.dumps(new))
            news = github.changes(old, new) if old else []
            if news:
                self.announce("\n".join(news[:4]))
            ran.append("github")
        return ran

    def _older_than(self, key: str, now: datetime, age: timedelta) -> bool:
        value = self.hub.get(key)
        return not value or now - datetime.fromisoformat(value) >= age

    def _done(self, key: str, now: datetime) -> bool:
        value = self.hub.get(key)
        if value.startswith("retry:"):
            return now < datetime.fromisoformat(value[6:])
        return bool(value)

    def _run(self) -> None:
        while not self._stop.wait(CHECK_EVERY_S):
            try:
                self.tick()
            except Exception:
                self.log("automation tick failed:\n" + traceback.format_exc())
