"""Everything Naman can do, as tools the model can call, grouped into toolsets.

Only the `core` toolset goes into every request; the router adds the others a command
needs. That keeps each request small, which matters on free-tier token limits.

Tools marked `needs_approval` pause for a yes/no first. Opening apps, files and sites
is harmless, so it runs straight away unless NAMAN_CONFIRM_ACTIONS is set. Shutting
down, restarting and data-changing MCP tools always ask.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

from .apps import open_app
from .hub import DAYS, Hub
from .reminders import resolve_due
from .skills import info, system
from .store import Store

CATEGORIES = ["coding", "learning", "job-search", "meeting", "admin", "personal", "health", "errand", "other"]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Any]
    needs_approval: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class Toolset:
    name: str
    description: str  # what the router reads to decide whether a command needs this set
    tools: list[Tool] = field(default_factory=list)
    always: bool = False


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required}


# Opening one of these runs code, so open_path refuses them; programs go through open_app.
RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi",
            ".scr", ".lnk", ".jar", ".py", ".pyw", ".reg", ".hta", ".cpl", ".url"}


def _known_folders() -> list[Path]:
    """Where people keep things. On many Windows laptops Desktop and Documents live in OneDrive."""
    home = Path.home()
    names = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
    folders = [home / n for n in names] + [home / "OneDrive" / n for n in names]
    folders += [Path(f"{d}:/") for d in "DE" if Path(f"{d}:/").exists()]
    return [f for f in folders if f.exists()]


def _resolve(path: str) -> Path:
    """The path as given, or the same place under OneDrive (~/Desktop -> ~/OneDrive/Desktop)."""
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if p.exists():
        return p
    try:
        rel = p.relative_to(Path.home())
    except ValueError:
        return p
    alt = Path.home() / "OneDrive" / rel
    if alt.exists():
        return alt
    # Case/extension slips: "internship details" for "Internship details.xlsx"
    parent = alt.parent if alt.parent.exists() else p.parent
    if parent.exists():
        want = p.name.lower()
        for child in parent.iterdir():
            if child.name.lower() == want or child.stem.lower() == want or child.stem.lower() == Path(want).stem:
                return child
    return p


SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "appdata", "$recycle.bin", "system volume information",
             "pipcache", "npmcache", "python313", "dist", "build", ".next", ".cache"}


def find_files(name: str, limit: int = 10, max_depth: int = 4) -> list[str]:
    """Files and folders whose name contains all the words of `name`, in the usual places."""
    words = [w for w in name.lower().replace("_", " ").split() if w]
    if not words:
        return []
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for e in entries:
            label = e.name.lower().replace("_", " ")
            if all(w in label for w in words) and e.path not in seen:
                seen.add(e.path)
                hits.append((depth, e.path))
            if depth < max_depth and e.is_dir(follow_symlinks=False) and e.name.lower() not in SKIP_DIRS \
                    and not e.name.startswith("."):
                walk(Path(e.path), depth + 1)

    for root in _known_folders():
        walk(root, 1 if root.drive and root == Path(root.anchor) else 0)
        if len(hits) >= limit * 3:
            break
    return [path for _, path in sorted(hits)[:limit]]


def _open_path(path: str) -> str:
    p = _resolve(path)
    if not p.exists():
        found = find_files(Path(path).stem.replace("-", " "), 3)
        hint = f" Similar: {'; '.join(found)}" if found else ""
        return f"error: {path} does not exist.{hint}"
    if p.is_file() and p.suffix.lower() in RUNNABLE:
        return f"error: {p.suffix} files run code; not opening it"
    system_name = platform.system()
    if system_name == "Windows":
        os.startfile(str(p))
    else:
        subprocess.Popen(["open" if system_name == "Darwin" else "xdg-open", str(p)])
    return f"opened {p}"


def _open_url(url: str) -> str:
    if urlparse(url).scheme not in ("http", "https"):
        return "error: only http(s) URLs can be opened"
    webbrowser.open(url)
    return f"opened {url}"


def _search_web(query: str, site: str = "google") -> str:
    base = {
        "google": "https://www.google.com/search?q=",
        "youtube": "https://www.youtube.com/results?search_query=",
    }.get(site, "https://www.google.com/search?q=")
    webbrowser.open(base + quote_plus(query))
    return f"opened {site} results for {query!r}"


def _spotify_play(query: str) -> str:
    from .skills import spotify  # needs Windows UI Automation; loaded on first use

    return spotify.play(query)


@dataclass
class Services:
    """What the newer skills use besides the store. Front ends fill in the parts they run
    (automation, the control center); tools look them up when called."""
    hub: Hub
    builder: Any = None  # claude_code.Builder
    automation: Any = None  # automation.Automation, when running for real
    outreach: Any = None  # outreach.Outreach: application email drafts
    open_control_center: Callable[[], str] | None = None
    show_form_helper: Callable[[], None] | None = None  # the orb's form helper panel


def whats_next(store: Store, hub: Hub, now: datetime | None = None) -> dict[str, Any]:
    """Where the user should be now and what to pick up: schedule + tasks + reminders."""
    now = now or datetime.now()
    slot = hub.now_and_next(now)
    brief = store.briefing(now.date())
    focus = brief["overdue"][:3] + [t for t in brief["due_today"] if t not in brief["overdue"]][:3]
    focus += [t for t in brief["high_priority"] if t not in focus][:3]
    if not focus:
        focus = store.list_tasks()[:3]
    return {
        "time": now.strftime("%A %H:%M"),
        "schedule_now": slot["now"], "schedule_next": slot["next"],
        "suggested_tasks": [{k: t[k] for k in ("id", "title", "project", "priority", "due")} for t in focus[:5]],
        "open_tasks": brief["open"],
        "next_reminders": store.pending_reminders()[:3],
    }


def build_toolsets(store: Store, confirm_actions: bool = False, services: Services | None = None) -> dict[str, Toolset]:
    """confirm_actions=True asks before opening apps, files and websites too."""
    ask = confirm_actions
    svc = services or Services(hub=Hub(store))
    hub = svc.hub

    def list_jobs(min_fit: int = 50, status: str = "new", query: str = "", limit: int = 8,
                  where: str = "") -> list[dict] | str:
        st = None if status in ("", "all") else status
        rows = hub.jobs(status=st, min_fit=int(min_fit), query=query or "", limit=max(1, min(int(limit), 20)),
                        include_description=False, where=where or "",
                        max_age_days=None if st in ("hidden", "saved", "applied") else 7)
        if not rows:
            return "no matching jobs stored yet; refresh_jobs fetches new ones"
        return [{"id": j["id"], "fit": j["fit"], "title": j["title"], "company": j["company"], "location": j["location"],
                 "experience": j["experience"], "salary": j["salary"], "source": j["source"], "url": j["url"]}
                for j in rows]

    def job_details(job_id: int) -> dict | str:
        job = hub.job(int(job_id))
        if job is None:
            return f"error: no job #{job_id}"
        job["description"] = job["description"][:1800]
        job.pop("dedupe_key", None)
        return job

    def refresh_jobs() -> str:
        if svc.automation is None:
            return "error: job search runs with the orb (`naman start`); it isn't running here"
        started = svc.automation.in_background(svc.automation.refresh_jobs, "jobs-refresh")
        return ("searching LinkedIn listings, LinkedIn hiring posts and remote boards now (a few minutes); "
                "I'll message the best matches" if started else "a search is already running; results soon")

    def whats_next_tool() -> dict:
        return whats_next(store, hub)

    def add_block(days: str, start: str, end: str, title: str) -> dict:
        block_id = hub.add_block(days, start, end, title)
        return {"block_id": block_id, "days": hub.blocks()[-1]["days"] if hub.blocks() else days}

    def list_schedule(day: str | None = None) -> list[dict] | str:
        day = (day or "").lower()[:3] or None
        if day == "tod":
            day = DAYS[datetime.now().weekday()]
        rows = hub.blocks(day if day in DAYS else None)
        return rows or "no schedule saved yet"

    def build(folder: str, instructions: str, new_project: bool = False) -> str:
        if svc.builder is None:
            return "error: the Claude Code builder isn't available here"
        return svc.builder.start(folder, instructions, bool(new_project))

    def draft_application(job_id: int, to_email: str = "") -> dict | str:
        if svc.outreach is None:
            return "error: email drafting isn't available here"
        result = svc.outreach.draft(int(job_id), to_email or "")
        if isinstance(result, str):
            return result
        return {k: result[k] for k in ("id", "to_email", "subject", "body", "attachment_name")} | {
            "next": "the user reviews it in the control center (Outreach) and presses Send, or asks you to send it "
                    "(send_application)"}

    def send_application(email_id: int) -> dict | str:
        if svc.outreach is None:
            return "error: email sending isn't available here"
        try:
            return svc.outreach.send(int(email_id)) | {"status": "sent; the job is marked applied"}
        except (ValueError, RuntimeError, OSError) as exc:
            return f"error: {exc}"

    def resume_match(job_id: int) -> list[dict] | str:
        if svc.outreach is None:
            return "error: not available here"
        return [{k: r[k] for k in ("name", "score", "missing")} for r in svc.outreach.resume_scores(int(job_id))[:5]]

    def tailor_resume(job_id: int) -> dict | str:
        if svc.outreach is None:
            return "error: not available here"
        result = svc.outreach.tailor(int(job_id))
        return {k: result[k] for k in ("file", "score", "missing", "removed")}

    def reply_to_post(job_id: int) -> dict | str:
        if svc.outreach is None:
            return "error: not available here"
        r = svc.outreach.reply(int(job_id))
        return {"text": r["text"], "subject": r["subject"], "send_as": r["channel"],
                "attach": r["resume"]["name"] if r["resume"] else None,
                "next": "the user copies it from the control center (Copy reply, Copy résumé) and sends it"}

    def read_screen(question: str = "") -> str:
        from . import jobs as jobs_mod, screen

        client = jobs_mod.groq_client()
        if client is None:
            return "error: no AI model available"
        return screen.ask(client, question)

    def apply_from_screen() -> dict | str:
        from . import jobs as jobs_mod, screen

        client = jobs_mod.groq_client()
        if client is None or svc.outreach is None:
            return "error: not available here"
        post = screen.job_post(client)
        if not post:
            return "error: I can't see a job post on the screen"
        r = svc.outreach.analyse_paste(post)
        out = {"job_id": r["job_id"], "title": r["title"], "company": r["company"], "fit": r["fit"],
               "resume": r["resume"]["name"] if r.get("resume") else None}
        if r.get("to_email"):
            mail = svc.outreach.mail(r["job_id"])
            return out | {"email_id": mail["id"], "to_email": mail["to_email"],
                          "next": "an application email with the résumé is drafted; tell the user and offer to send "
                                  "it (send_application with this email_id) or review it in Outreach"}
        return out | {"reply": r["text"], "next": "no email address in the post: the reply and résumé are ready in "
                                                  "the control center (Paste a post / Jobs); apply via the post's link"}

    def fill_form() -> str:
        if svc.show_form_helper is None:
            return "error: the form helper runs with the orb (naman start)"
        svc.show_form_helper()
        return ("the form helper is reading the screen; a panel with each field, its answer and a Copy button "
                "opens next to the orb in a few seconds. Say so briefly.")

    def tech_updates(category: str = "", limit: int = 6) -> list[dict] | str:
        auto = svc.automation
        if auto is None:
            return "error: tech updates aren't running here"
        rows = auto.tech.items(category or "")[: max(1, min(int(limit or 6), 15))]
        if not rows:
            return "nothing yet: the first tech news run is still to come (or say 'refresh tech news')"
        return [{k: r[k] for k in ("headline", "summary", "details", "source", "published", "important")} for r in rows]

    def company_contacts(company: str) -> dict:
        from . import contacts

        return contacts.lookup(hub, company)

    def open_center() -> str:
        if svc.open_control_center is None:
            return "error: the control center runs with the orb (`naman start`)"
        return svc.open_control_center()

    def add_task(title: str, project: str, category: str, priority: str, due: str | None = None) -> dict:
        task_id = store.add_task(title, project, category, priority, due)
        return {"task_id": task_id, "project": project, "category": category, "priority": priority, "due": due}

    def set_reminder(message: str, at: str | None = None, in_minutes: float | None = None) -> dict:
        due = resolve_due(at, in_minutes)
        return {"reminder_id": store.add_reminder(message, due), "due": due.isoformat(timespec="minutes")}

    def run_routine(name: str) -> str:
        routine = store.get_routine(name)
        if routine is None:
            names = ", ".join(r["name"] for r in store.routines()) or "none yet"
            return f"error: no routine called {name!r}. Saved routines: {names}"
        return f"Routine '{routine['name']}'. Do these steps now, in order, with your tools:\n{routine['steps']}"

    core = Toolset("core", "always available", always=True, tools=[
        Tool("get_time", "Current local date, time and weekday. Use before working with dates.", _obj({}),
             lambda: datetime.now().strftime("%A %d %B %Y, %H:%M")),
        Tool("open_app", "Open an installed app by the name the user says: 'VS Code', 'Chrome', 'Spotify', "
             "'Calculator', 'Settings', 'Word'. Returns once the window is up (or brings it to the front if "
             "it's already open), so the next step can use the app straight away.",
             _obj({"name": {"type": "string"}}), open_app, ask),
        Tool("open_url", "Open a web page in the laptop's browser.", _obj({"url": {"type": "string"}}), _open_url, ask),
        Tool("search_web", "Open a Google or YouTube results page in the browser.",
             _obj({"query": {"type": "string"}, "site": {"type": "string", "enum": ["google", "youtube"]}}),
             _search_web, ask),
        Tool("open_path", "Open a file or folder, e.g. 'C:/code', '~/Downloads'. For programs use open_app. "
             "Desktop/Documents may be under OneDrive; this checks there too.",
             _obj({"path": {"type": "string"}}), _open_path, ask),
        Tool("find_file", "Find files or folders by name words on the laptop (Desktop, Documents, Downloads, "
             "OneDrive, D:, E:). Use before open_path when you don't know the exact path.",
             _obj({"name": {"type": "string", "description": "words from the name, e.g. 'internship details'"}}),
             lambda name: find_files(name) or f"nothing named like {name!r} in the usual places"),
        Tool("open_control_center", "Open the control center window: jobs, hiring posts, feed, tasks, schedule, "
             "Claude Code builds, memory.", _obj({}), open_center),
    ])

    tasks = Toolset("tasks", "to-do items, work planning, projects, deadlines, daily briefing / 'what's my day'", tools=[
        Tool("add_task", "Add a work item. Classify it yourself: project (reuse one only if it truly belongs), "
             "category, priority. Convert relative dates to YYYY-MM-DD.",
             _obj({"title": {"type": "string"}, "project": {"type": "string"},
                   "category": {"type": "string", "enum": CATEGORIES},
                   "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                   "due": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"}}),
             add_task),
        Tool("list_tasks", "List tasks, highest priority first.",
             _obj({"project": {"type": ["string", "null"]}, "status": {"type": "string", "enum": ["open", "done"]}}),
             lambda project=None, status="open": store.list_tasks(project, status)),
        Tool("complete_task", "Mark a task done by id.", _obj({"task_id": {"type": "integer"}}),
             lambda task_id: {"completed": store.complete_task(task_id)}),
        Tool("daily_briefing", "Overdue, due today, high priority and per-project open work.", _obj({}),
             lambda: store.briefing()),
        Tool("whats_next", "What to do now: the current and next block of the user's schedule, the tasks to "
             "pick up (overdue, due today, high priority) and upcoming reminders. Use for 'what next?', "
             "'what should I do now?', 'I'm free'.", _obj({}), whats_next_tool),
        Tool("add_schedule_block", "Add a recurring block to the weekly schedule, e.g. days 'weekdays', "
             "start '10:00', end '18:00', title 'Work'. Days: mon..sun, 'mon-fri', 'weekends', 'daily'.",
             _obj({"days": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                   "title": {"type": "string"}}), add_block),
        Tool("list_schedule", "The weekly schedule, or one day's ('mon', 'today').",
             _obj({"day": {"type": ["string", "null"]}}, []), list_schedule),
        Tool("delete_schedule_block", "Delete a schedule block by id.", _obj({"block_id": {"type": "integer"}}),
             lambda block_id: {"deleted": hub.delete_block(int(block_id))}),
        Tool("clear_schedule", "Delete the whole weekly schedule (before replacing it with a new one).", _obj({}),
             lambda: {"deleted_blocks": hub.clear_schedule()}, needs_approval=True),
    ])

    memory = Toolset("memory", "facts about the user: preferences, people, contacts, phone numbers, "
                     "home city, 'remember that...', 'what do you know about me'", always=True, tools=[
        Tool("remember", "Save one lasting fact about the user. Never store passwords or keys.",
             _obj({"fact": {"type": "string"}}), lambda fact: {"memory_id": store.remember(fact)}),
        Tool("recall", "Search saved facts by keywords.", _obj({"query": {"type": "string"}}),
             lambda query: store.search_memories(query)),
        Tool("forget", "Delete a saved fact by id.", _obj({"memory_id": {"type": "integer"}}),
             lambda memory_id: {"deleted": store.forget(memory_id)}),
    ])

    laptop = Toolset("laptop", "control the computer: volume ('turn it up/down', louder, quieter), mute, "
                     "brightness, play/pause/next song, "
                     "close/switch apps and windows, browser tabs (close/new/reopen/next tab), type text, keyboard shortcuts, clipboard, screenshot, "
                     "battery/CPU status, lock, sleep, restart, shut down", tools=[
        Tool("volume", "Get or change the speaker volume.",
             _obj({"action": {"type": "string", "enum": ["set", "up", "down", "mute", "unmute", "get"]},
                   "level": {"type": ["integer", "null"], "description": "0-100 for set; step size for up/down"}},
                  ["action"]), system.volume),
        Tool("media", "Control whatever is playing (Spotify, YouTube, etc.).",
             _obj({"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}), system.media),
        Tool("brightness", "Set screen brightness 0-100.", _obj({"level": {"type": "integer"}}), system.brightness),
        Tool("close_app", "Close an app's windows politely (like clicking X; unsaved work gets a save prompt). "
             "For a single tab, use `tab` instead.",
             _obj({"name": {"type": "string"}}), system.close_app),
        Tool("switch_to", "Bring an open app's window to the front.", _obj({"name": {"type": "string"}}),
             system.switch_to),
        Tool("tab", "Close, open, reopen or move between tabs in a browser or other tabbed app. When the user "
             "says 'tab', use this, never close_app (that closes every window of the app). Call it ONCE per "
             "request: put the number of tabs in `count`, and the app the user named in `app` (empty only "
             "when they name none, e.g. 'close this tab').",
             _obj({"action": {"type": "string", "enum": list(system.TAB_KEYS)},
                   "app": {"type": ["string", "null"], "description": "e.g. 'chrome', 'edge'; empty = window in front"},
                   "count": {"type": ["integer", "null"], "description": "how many tabs, default 1, max 10"}},
                  ["action"]), system.tab),
        Tool("list_windows", "Titles of open windows.", _obj({}), system.list_windows),
        Tool("type_text", "Type text into the focused window. Doesn't press Enter.",
             _obj({"text": {"type": "string"}}), system.type_text),
        Tool("press_keys", "Press a shortcut, e.g. 'ctrl+s', 'ctrl+t', 'win+d', 'alt+tab', 'enter'.",
             _obj({"keys": {"type": "string"}}), system.press_keys),
        Tool("clipboard", "Read the clipboard, or copy text to it.",
             _obj({"action": {"type": "string", "enum": ["read", "write"]}, "text": {"type": ["string", "null"]}},
                  ["action"]), system.clipboard),
        Tool("screenshot", "Save a screenshot to Pictures/Screenshots.", _obj({}), system.screenshot),
        Tool("system_status", "Battery, charging, CPU, memory and free disk.", _obj({}), system.system_status),
        Tool("lock_screen", "Lock the laptop.", _obj({}), system.lock_screen),
        Tool("power", "Sleep, restart or shut down the laptop (restart/shutdown after 30 s).",
             _obj({"action": {"type": "string", "enum": ["sleep", "restart", "shutdown"]}}), system.power,
             needs_approval=True),
        Tool("cancel_shutdown", "Cancel a pending restart or shutdown.", _obj({}), system.cancel_shutdown),
    ])

    knowledge = Toolset("knowledge", "weather, news headlines, and looking things up on the web: facts, "
                        "people, sports scores, prices, definitions, anything current", tools=[
        Tool("weather", "Current weather and forecast for a city. Use the user's home city from memory if "
             "they don't say one.", _obj({"city": {"type": "string"}, "days": {"type": "integer"}}, ["city"]),
             info.weather),
        Tool("news", "Top headlines (India edition), optionally about a topic.",
             _obj({"topic": {"type": ["string", "null"]}}, []), info.news),
        Tool("web_lookup", "Search the web and read result snippets to answer a question. Say where the "
             "answer came from.", _obj({"query": {"type": "string"}}), info.web_lookup),
        Tool("tech_updates", "Latest tech news already collected and explained: new AI models (OpenAI, Anthropic, "
             "Google, Meta...), language and framework releases, dev tools. Use for 'what's new in AI/tech'.",
             _obj({"category": {"type": "string", "enum": ["", "AI models", "AI tools & agents",
                                                           "Languages & frameworks", "Dev tools & cloud", "Industry"]},
                   "limit": {"type": "integer"}}, []), tech_updates),
    ])

    screen = Toolset("screen", "look at the user's screen: 'what's on my screen', 'read this', 'what does this "
                     "error mean', 'summarise this page', 'apply to the job on my screen'", tools=[
        Tool("read_screen", "Look at the user's screen now and answer about it (read text, explain an error, "
             "summarise a page). Pass what they asked.", _obj({"question": {"type": "string"}}, []), read_screen),
        Tool("fill_form", "Help fill the application form / job portal page on the user's screen: opens a panel "
             "listing each field with a suggested answer and a Copy button. For 'help me fill this form'.", _obj({}),
             fill_form),
        Tool("apply_from_screen", "Read the job post on the user's screen, score it, pick the résumé and draft the "
             "application (email with résumé when the post has an address). Takes up to a minute.", _obj({}),
             apply_from_screen),
    ])

    media = Toolset("media", "play music, songs, playlists, videos, podcasts on Spotify or YouTube", tools=[
        Tool("spotify_play", "Play music in Spotify: opens Spotify itself (no open_app needed), searches, plays "
             "the top result and confirms it started. Make the query specific: 'old songs' -> 'old hindi "
             "classics' if that's their taste; an artist, mood or playlist name works best.",
             _obj({"query": {"type": "string"}}), _spotify_play, ask),
        Tool("play_youtube", "Play the top YouTube video for a query, e.g. 'lo-fi beats', 'Arijit Singh songs'.",
             _obj({"query": {"type": "string"}}), info.play_youtube, ask),
    ])

    messaging = Toolset("messaging", "send or draft WhatsApp messages to people", tools=[
        Tool("whatsapp_draft", "Open WhatsApp with a message typed in; the user presses send. Look up the "
             "person's number with recall first.",
             _obj({"message": {"type": "string"}, "phone": {"type": ["string", "null"]}}, ["message"]),
             info.whatsapp_draft),
    ])

    reminders = Toolset("reminders", "timers, alarms, reminders: 'remind me', 'in 10 minutes', 'at 6 pm'", tools=[
        Tool("set_reminder", "Set a timer or reminder. Use in_minutes for 'in X minutes/hours', or at "
             "(YYYY-MM-DDTHH:MM, local time) for a clock time; get_time first.",
             _obj({"message": {"type": "string"}, "at": {"type": ["string", "null"]},
                   "in_minutes": {"type": ["number", "null"]}}, ["message"]), set_reminder),
        Tool("list_reminders", "Upcoming reminders.", _obj({}), store.pending_reminders),
        Tool("cancel_reminder", "Cancel a reminder by id.", _obj({"reminder_id": {"type": "integer"}}),
             lambda reminder_id: {"cancelled": store.cancel_reminder(reminder_id)}),
    ])

    routines = Toolset("routines", "routines / shortcuts: 'when I say X do Y', 'good morning', "
                       "'work mode', running or editing a saved routine", tools=[
        Tool("save_routine", "Save a routine: a name the user will say and the steps to run, in plain words.",
             _obj({"name": {"type": "string"}, "steps": {"type": "string"}}),
             lambda name, steps: store.save_routine(name, steps) or f"saved routine {name!r}"),
        Tool("run_routine", "Get a saved routine's steps, then carry them out.", _obj({"name": {"type": "string"}}),
             run_routine),
        Tool("list_routines", "Saved routines.", _obj({}), store.routines),
        Tool("delete_routine", "Delete a routine.", _obj({"name": {"type": "string"}}),
             lambda name: {"deleted": store.delete_routine(name)}),
    ])

    jobs = Toolset("jobs", "job search: openings, hiring posts, fit scores, applying, the control center, "
                   "LinkedIn posts worth reading, new tool launches", tools=[
        Tool("list_jobs", "The jobs already found (refreshed every night): LinkedIn listings and hiring posts, "
             "freelance projects, remote boards; best fit first, last 7 days. ALWAYS use this to answer questions "
             "about jobs. status: new, saved, applied, hidden, all; where: home (near the user), remote, freelance, internship; "
             "query filters title/company/skills/location.",
             _obj({"min_fit": {"type": "integer"}, "status": {"type": "string",
                                                              "enum": ["new", "saved", "applied", "hidden", "all"]},
                   "query": {"type": "string"}, "limit": {"type": "integer"},
                   "where": {"type": "string", "enum": ["", "home", "remote", "freelance", "internship"]}}, []),
             list_jobs),
        Tool("job_details", "Everything about one job: description, experience, skills, salary, contact, why it "
             "fits.", _obj({"job_id": {"type": "integer"}}), job_details),
        Tool("mark_job", "Mark a job saved, applied or hidden.",
             _obj({"job_id": {"type": "integer"}, "status": {"type": "string", "enum": ["saved", "applied", "hidden",
                                                                                       "new"]}}),
             lambda job_id, status: {"updated": hub.set_job_status(int(job_id), status)}),
        Tool("refresh_jobs", "Start a new search for openings (takes 10-20 minutes, runs every night anyway). ONLY "
             "when the user explicitly asks to search / fetch / find NEW jobs now; never to answer a question.",
             _obj({}), refresh_jobs),
        Tool("draft_application", "Write a tailored application email for a job (finds the HR/careers address, "
             "picks the right résumé). It's a draft: nothing is sent until the user asks.",
             _obj({"job_id": {"type": "integer"}, "to_email": {"type": "string"}}, ["job_id"]),
             draft_application),
        Tool("send_application", "Send a drafted application email (by its email id, from draft_application or the "
             "Outreach list) from the user's Gmail with the résumé attached. ONLY when the user explicitly asks to "
             "send it.", _obj({"email_id": {"type": "integer"}}), send_application, needs_approval=True),
        Tool("resume_match", "Score the user's résumés against a job (ATS-style keyword match): best first, with "
             "missing keywords.", _obj({"job_id": {"type": "integer"}}), resume_match),
        Tool("tailor_resume", "Write a one-page résumé tailored to a job from the user's own résumés and facts "
             "(nothing invented; unsupported skills/numbers are removed), saved as a PDF, with its score.",
             _obj({"job_id": {"type": "integer"}}), tailor_resume),
        Tool("reply_to_post", "Write a reply to a job post (LinkedIn DM/comment, email or proposal) and pick the "
             "résumé to attach. The user copies and sends it.", _obj({"job_id": {"type": "integer"}}), reply_to_post),
        Tool("company_contacts", "A company's website, published emails (hr@, careers@...) and HR / recruiter "
             "LinkedIn profiles, for cold emailing and connecting. Takes up to a minute the first time.",
             _obj({"company": {"type": "string"}}), company_contacts),
        Tool("list_posts", "Recent posts worth reading: new AI tools, launches, hiring posts (LinkedIn, Hacker News).",
             _obj({"limit": {"type": "integer"}}, []),
             lambda limit=8: [{k: p[k] for k in ("id", "source", "title", "snippet", "url")}
                              for p in hub.posts(limit=max(1, min(int(limit), 20)))] or "no posts yet"),
    ])

    builder = Toolset("build", "build or change software with Claude Code (an autonomous coding agent): "
                      "'build a ... app', 'add a feature to project X', coding jobs", tools=[
        Tool("build_with_claude_code", "Hand a coding job to Claude Code, which works on its own in the background "
             "in that folder and makes the decisions itself; you get a message when it's done. Give the full "
             "folder path (for a new project: a new folder under the user's projects folder and new_project true) and clear, "
             "complete instructions including what the user said about stack and features.",
             _obj({"folder": {"type": "string"}, "instructions": {"type": "string"},
                   "new_project": {"type": "boolean"}}, ["folder", "instructions"]), build, needs_approval=True),
        Tool("build_status", "Claude Code builds: running, done or failed, with their reports.", _obj({}),
             lambda: svc.builder.status() if svc.builder else "no builder here"),
    ])

    sets = [core, tasks, memory, laptop, knowledge, screen, media, messaging, reminders, routines, jobs, builder]
    return {s.name: s for s in sets}


def build_tools(store: Store, confirm_actions: bool = False) -> dict[str, Tool]:
    """Every built-in tool by name."""
    return {t.name: t for s in build_toolsets(store, confirm_actions).values() for t in s.tools}


def run_tool(tool: Tool, args: dict[str, Any]) -> str:
    """Run a tool and always return a string for the model, errors included."""
    try:
        result = tool.run(**args)
    except TypeError as exc:
        return f"error: bad arguments for {tool.name}: {exc}"
    except Exception as exc:
        return f"error: {exc.__class__.__name__}: {exc}"
    return result if isinstance(result, str) else json.dumps(result, default=str)
