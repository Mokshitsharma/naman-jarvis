"""Job application emails: tailored drafts, sent with one click once you've read them.

For a job, Naman finds where to write (an address in the post first, then the company's
published hr@ / careers@, see contacts.py), writes a short email using only true facts
from your profile and memory, picks the résumé for the role, and keeps it as a draft.

Nothing goes out on its own. In the control center each draft has "Send": it goes from
your job-application Gmail ("gmail_account" in data/outreach.json) over Gmail's SMTP
with the résumé attached, is marked sent and the job applied, so the same address is
never drafted twice. Sending needs a Google App Password in NAMAN_GMAIL_APP_PASSWORD
(.env); without one, "Open in Gmail" still opens a filled-in compose window to send
by hand, then "Mark sent". A draft with an unfilled [placeholder] is never sent.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import smtplib
import threading
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from . import contacts
from .hub import Hub
from .llm import complete

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    job_id INTEGER,
    company TEXT NOT NULL DEFAULT '',
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attachment TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'skipped')),
    created TEXT NOT NULL,
    sent_at TEXT
);
"""

# Generic defaults; your own (name, Gmail account, résumé per role, signature) live in
# data/outreach.json, which is created from these on first use and is yours to edit.
DEFAULT_CONFIG: dict[str, Any] = {
    "from_name": os.environ.get("NAMAN_USER_NAME", ""),
    "gmail_account": "",  # "Open in Gmail" opens compose in this account
    "auto_draft_min_fit": 60,
    "auto_draft_per_run": 10,
    # Which résumé goes with which kind of role: {regex on the job title: file path}; first
    # match wins, "" matches anything.
    "resumes": {},
    "signature": "",
    # Auto-apply: after each search, drafts for jobs this good are sent on their own (from
    # your Gmail, résumé attached), at most this many a day. Drafts with an unfilled
    # [placeholder] or no résumé file are never sent; they wait for you.
    "auto_send": False,
    "auto_send_min_fit": 70,
    "auto_send_per_day": 100,  # a hard ceiling; the day's real cap is today_cap()
    "daily_target": 100,  # emails a day (job applications + cold emails), after the warm-up
    "warmup_start": 25,  # the first day's cap; it grows by "warmup_step" a day
    "warmup_step": 10,
    "send_window": ["09:30", "19:00"],  # the all-day loop spreads the day's emails over this
    "send_per_tick": 4,
    "cold_emails": True,  # also write to companies' published hiring addresses
}

WRITE_MODEL = "openai/gpt-oss-120b"
WRITE_PROMPT = """\
Write a short job application email from {name} to the hiring team for the job below.

Rules:
- 110-170 words in the body, plain text, no markdown, no emoji.
- Open with the role and where they saw it. Then two or three sentences on why they fit, \
using ONLY facts from "About the candidate" (real projects, real work experience, real skills). \
Never invent numbers, titles, companies or skills. Pick at most three items, the ones \
that match this job best; don't list everything. No vague claims ("comfortable with cloud").
- Mention the attached résumé and ask for a short call or the next step.
- Sound like a capable engineer writing to a person: direct, specific, no flattery, \
no "I hope this email finds you well", no "I am writing to express my interest".
- For an internship, freelance project or remote role, say they're available for that.
- No signature: it's added after the body.

About the candidate:
{about}

Job:
{job}"""
WRITE_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}


COLD_PROMPT = """\
Write a short cold email from {name} to the hiring team at {company}, asking about openings.

Take their experience level and the kinds of roles they're open to (internship, part-time, \
freelance/contract, full-time) from "About the candidate".

Rules:
- 90-140 words in the body, plain text, no markdown, no emoji, no [brackets] or placeholders.
- Open with one line on why {company}, using ONLY "About the company" and "Roles they've \
posted"; if those say nothing useful, keep it general. Never invent facts about the company.
- Two or three sentences on why they fit, using ONLY facts from "About the candidate"; at most three items.
- Say their résumé is attached; ask whether the company has or expects an opening in any of those forms.
- Direct and human: no flattery, no "I hope this email finds you well". No signature.
- subject: short and specific, e.g. "Data/AI engineer: internship or entry-level role".

About the company: {about_company}
Roles they've posted: {roles}

About the candidate:
{about}"""


REPLY_PROMPT = """\
Write {name}'s reply to this job post, to be sent as {channel}.

Rules:
- 60-120 words, plain text, no markdown, no emoji, no hashtags.
- Say which role they're applying for, then one or two specific reasons they fit, using ONLY \
facts from "About the candidate" that match what the post asks for. Never invent anything.
- Answer what the post asks for (location, a portfolio or GitHub link) when "About the candidate" \
has it. Availability, internship duration, notice period, start date, expected salary: \
only if "About the candidate" states them; if the post asks and it doesn't, write a placeholder \
in square brackets for them to fill, e.g. "[notice period]". Never guess these.
- Say their résumé is attached and close with a simple ask (a call, the next step).
- Direct and human; no "I hope this message finds you well", no flattery.
- subject: an email subject line if this goes by email, else "".

About the candidate:
{about}

Post:
{job}"""
REPLY_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "text": {"type": "string"}},
    "required": ["subject", "text"],
    "additionalProperties": False,
}


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        return {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    except ValueError:
        return dict(DEFAULT_CONFIG)


def fix_links(text: str, config: dict[str, Any]) -> str:
    """Applies "link_fixes" from data/outreach.json ({wrong: right}): an old profile link or a
    dead domain in a résumé or in your facts never reaches an employer."""
    import re as _re

    fixes = config.get("link_fixes") or {}
    if not fixes or not text:
        return text
    pattern = _re.compile("|".join(_re.escape(k) for k in sorted(fixes, key=len, reverse=True)))
    return pattern.sub(lambda m: fixes[m.group(0)], text)


def resume_for(title: str, config: dict[str, Any], job: dict[str, Any] | None = None,
               cache_dir: Path | None = None) -> str:
    """The résumé to send: the best-scoring one for the job (see resumes.py); without a job
    or any readable résumé, the first "resumes" pattern that matches the title."""
    if job is not None and config.get("resume_files"):
        try:
            from . import resumes

            ranked = resumes.rank(job, config, cache_dir)
            if ranked and ranked[0]["score"] > 0:
                return ranked[0]["file"]
        except Exception:
            pass
    return resume_by_title(title, config)


def resume_by_title(title: str, config: dict[str, Any]) -> str:
    low = f" {title.lower()} "
    for pattern, path in config["resumes"].items():
        if (not pattern or re.search(pattern, low)) and Path(path).exists():
            return path
    return ""


PLACEHOLDER = re.compile(r"\[[^\]\n]{2,40}\]")  # "[notice period]": left for you to fill in
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465


def full_body(item: dict[str, Any], config: dict[str, Any]) -> str:
    return fix_links(f"{item['body']}\n\n{config['signature']}".strip(), config)


def build_message(item: dict[str, Any], config: dict[str, Any]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((config.get("from_name") or "", config["gmail_account"]))
    msg["To"] = item["to_email"]
    msg["Subject"] = item["subject"]
    msg.set_content(full_body(item, config))
    path = Path(item["attachment"])
    kind = (mimetypes.guess_type(path.name)[0] or "application/octet-stream").split("/")
    msg.add_attachment(path.read_bytes(), maintype=kind[0], subtype=kind[1], filename=path.name)
    return msg


def gmail_compose_url(item: dict[str, Any], config: dict[str, Any]) -> str:
    query = {"view": "cm", "fs": "1", "to": item["to_email"], "su": item["subject"],
             "body": full_body(item, config)}
    if config.get("gmail_account"):
        query["authuser"] = config["gmail_account"]
    return "https://mail.google.com/mail/?" + urlencode(query)


class Outreach:
    def __init__(self, hub: Hub, data_dir: Path, announce: Callable[[str], None] = lambda t: None,
                 llm: Callable[[], Any] | None = None, log: Callable[[str], None] = print) -> None:
        self.hub, self.store = hub, hub.store
        self.config_path = data_dir / "outreach.json"
        self.cache_dir = data_dir / "resumes" / "cache"  # PDF text, so scoring is instant after the first read
        self.tailored_dir = data_dir / "resumes" / "tailored"
        self.announce, self.log = announce, log
        self._llm_factory = llm
        self._send_lock = threading.Lock()  # one send at a time: a double click never sends twice
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    @property
    def config(self) -> dict[str, Any]:
        return load_config(self.config_path)

    # --- data -----------------------------------------------------------------------------

    def items(self, status: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        sql = "SELECT * FROM outreach" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.store._rows(sql, (status, limit) if status else (limit,))
        cfg = self.config
        for r in rows:
            r["gmail_url"] = gmail_compose_url(r, cfg)
            r["attachment_name"] = Path(r["attachment"]).name if r["attachment"] else ""
        return rows

    def item(self, item_id: int) -> dict[str, Any] | None:
        rows = [r for r in self.items(limit=100_000) if r["id"] == item_id]
        return rows[0] if rows else None

    def edit(self, item_id: int, fields: dict[str, str]) -> None:
        allowed = {k: v for k, v in fields.items() if k in {"subject", "body", "to_email"}}
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            self.store._write(f"UPDATE outreach SET {sets} WHERE id = ? AND status = 'draft'", (*allowed.values(), item_id))

    def mark(self, item_id: int, status: str) -> None:
        """"sent" after you've sent it from Gmail (the job becomes "applied"), or "skipped"."""
        if status not in {"sent", "skipped", "draft"}:
            raise ValueError("status must be sent, skipped or draft")
        item = self.item(item_id)
        if item is None:
            raise ValueError(f"no email #{item_id}")
        self.store._write("UPDATE outreach SET status = ?, sent_at = ? WHERE id = ?",
                          (status, datetime.now().isoformat(timespec="seconds") if status == "sent" else None, item_id))
        if status == "sent" and item["job_id"]:
            self.hub.set_job_status(item["job_id"], "applied")

    def can_send(self) -> bool:
        return bool(self.config.get("gmail_account") and os.environ.get("NAMAN_GMAIL_APP_PASSWORD"))

    def send(self, item_id: int, smtp: Callable[[], Any] | None = None) -> dict[str, Any]:
        """Sends a draft from your Gmail with its résumé attached, then marks it sent (and the
        job applied). Refuses an unfilled [placeholder], a missing address or résumé file,
        and anything already sent. Sends are serialised and the draft is re-read inside the
        lock, so two clicks (or Send and Send all at once) can't send it twice."""
        with self._send_lock:
            return self._send(item_id, smtp)

    def _send(self, item_id: int, smtp: Callable[[], Any] | None) -> dict[str, Any]:
        item = self.item(item_id)
        if item is None:
            raise ValueError(f"no email #{item_id}")
        if item["status"] != "draft":
            raise ValueError(f"email #{item_id} is {item['status']}, not a draft")
        if "@" not in item["to_email"]:
            raise ValueError("no email address: type one in first")
        cfg = self.config
        if hole := PLACEHOLDER.search(f"{item['subject']}\n{item['body']}"):
            raise ValueError(f"fill in {hole.group(0)} before sending")
        if not item["attachment"] or not Path(item["attachment"]).is_file():  # the email says it's attached
            raise ValueError(f"no résumé file to attach{': ' + item['attachment'] if item['attachment'] else ''}")
        password = os.environ.get("NAMAN_GMAIL_APP_PASSWORD", "").replace(" ", "")
        if not (cfg.get("gmail_account") and (password or smtp)):
            raise RuntimeError('sending is off: set "gmail_account" in data/outreach.json and NAMAN_GMAIL_APP_PASSWORD '
                               "in .env (a Google App Password), or use Open in Gmail")
        msg = build_message(item, cfg)
        with (smtp() if smtp else smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)) as server:
            if not smtp:
                server.login(cfg["gmail_account"], password)
            server.send_message(msg)
        self.mark(item_id, "sent")
        self.log(f"outreach: sent #{item_id} to {item['to_email']}")
        return {"id": item_id, "to_email": item["to_email"], "attached": item["attachment_name"]}

    def send_all(self, pause_s: float = 1.5, smtp: Callable[[], Any] | None = None) -> dict[str, Any]:
        """"Send all" in Outreach: every draft, one at a time with a short pause (a burst looks
        like spam). Drafts send() refuses (a [placeholder], no résumé) stay as drafts and are
        listed with the reason."""
        import time

        sent, skipped = [], []
        for item in self.items("draft"):
            try:
                self.send(item["id"], smtp=smtp)
            except ValueError as exc:
                skipped.append({"id": item["id"], "company": item["company"] or item["to_email"], "why": str(exc)})
                continue
            except (RuntimeError, OSError) as exc:  # no password, Gmail refused, offline: stop here
                return {"sent": sent, "skipped": skipped, "stopped": str(exc)}
            sent.append({"id": item["id"], "company": item["company"] or item["to_email"]})
            if pause_s:
                time.sleep(pause_s)
        return {"sent": sent, "skipped": skipped, "stopped": ""}

    def contacted(self, email: str) -> bool:
        return bool(self.store._rows("SELECT 1 FROM outreach WHERE lower(to_email) = lower(?) AND status IN "
                                     "('sent', 'draft') LIMIT 1", (email,)))

    # --- drafting ---------------------------------------------------------------------------

    def _about(self, job: dict[str, Any]) -> str:
        facts = self.store.relevant_memories(f"{job['title']} {' '.join(job.get('skills', []))} project", 14)
        facts = [m["fact"] for m in facts if not m["fact"].startswith(("Private:", "Personal contact", "Work contact"))]
        profile = self.store.profile().split("- Facts starting")[0]
        return profile + "\n" + "\n".join(f"- {f}" for f in facts)

    def write(self, job: dict[str, Any]) -> tuple[str, str]:
        llm = self._llm_factory() if self._llm_factory else None
        if llm is None:
            raise RuntimeError("no AI model available to write the email")
        posting = (f"Title: {job['title']}\nCompany: {job.get('company') or 'not named'}\n"
                   f"Location: {job.get('location', '')} ({job.get('work_mode', '')})\n"
                   f"Experience: {job.get('experience', '')}\nSkills: {', '.join(job.get('skills', []))}\n"
                   f"Seen on: {'a LinkedIn post' if job['source'] == 'linkedin_post' else job['source']}\n"
                   f"Summary: {job.get('summary', '')}\n\n{job.get('description', '')[:1800]}")
        response = complete(llm, 
            model=WRITE_MODEL, reasoning_effort="low",
            messages=[{"role": "user", "content": WRITE_PROMPT.format(name=self.config["from_name"],
                                                                      about=self._about(job), job=posting)}],
            response_format={"type": "json_schema", "json_schema": {"name": "email", "strict": True,
                                                                    "schema": WRITE_SCHEMA}},
        )
        data = json.loads(response.choices[0].message.content or "{}")
        return fix_links(data["subject"].strip(), self.config), fix_links(data["body"].strip(), self.config)

    def draft(self, job_id: int, to_email: str = "", find_contacts: bool = True) -> dict[str, Any] | str:
        job = self.hub.job(job_id)
        if job is None:
            return f"error: no job #{job_id}"
        existing = self.store._rows("SELECT id, status FROM outreach WHERE job_id = ? AND status != 'skipped'",
                                    (job_id,))
        if existing:
            return f"error: job #{job_id} already has email #{existing[0]['id']} ({existing[0]['status']})"
        found = None
        if find_contacts and not to_email and job.get("company") and "client" not in job["company"].lower():
            found = contacts.lookup(self.hub, job["company"])
        email = to_email or contacts.best_email(job, found)
        if not email:
            people = (found or {}).get("people", [])
            hint = f" HR on LinkedIn: {', '.join(p['name'] for p in people[:3])}." if people else ""
            return f"error: no email address for {job['company'] or 'this job'}; apply via the link.{hint}"
        if self.contacted(email):
            return f"error: {email} already has an email (sent or drafted)"
        subject, body = self.write(job)
        chosen = self.best_resume(job_id) if self.config.get("resume_files") else None
        item_id = self.store._write(
            "INSERT INTO outreach (job_id, company, to_email, subject, body, attachment, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, job.get("company", ""), email, subject, body,
             chosen["file"] if chosen else resume_for(job["title"], self.config, job, self.cache_dir),
             datetime.now().isoformat(timespec="seconds")))
        return self.item(item_id) or {}

    # --- résumés and replies ---------------------------------------------------------------

    def tailored_for(self, job_id: int) -> list[Path]:
        return sorted(self.tailored_dir.glob(f"*-{job_id}.pdf")) if self.tailored_dir.exists() else []

    def resume_scores(self, job_id: int) -> list[dict[str, Any]]:
        from . import resumes

        job = self.hub.job(job_id)
        if job is None:
            raise ValueError(f"no job #{job_id}")
        return resumes.rank(job, self.config, self.cache_dir, extra=self.tailored_for(job_id))

    def tailor(self, job_id: int) -> dict[str, Any]:
        from . import resumes

        job = self.hub.job(job_id)
        if job is None:
            raise ValueError(f"no job #{job_id}")
        llm = self._llm_factory() if self._llm_factory else None
        if llm is None:
            raise RuntimeError("no AI model available")
        return resumes.tailor(job, self.config, llm, self._about(job), self.tailored_dir, self.cache_dir)

    def best_resume(self, job_id: int, threshold: int | None = None) -> dict[str, Any] | None:
        """The résumé to send for this job. Your own best one; when even that scores below
        "tailor_below" (default 70), a tailored one is made (once per job) and used if it
        scores higher."""
        ranked = self.resume_scores(job_id)
        best = ranked[0] if ranked else None
        threshold = int(threshold if threshold is not None else self.config.get("tailor_below", 70))
        if (best is None or best["score"] < threshold) and not self.tailored_for(job_id) and self._llm_factory:
            try:
                made = self.tailor(job_id)
            except Exception as exc:  # rate limit, Chrome missing: fall back to your best
                self.log(f"auto-tailor for job #{job_id} failed: {exc!r}")
                return best
            made = made | {"tailored": True, "auto_tailored": True}
            if best is None or made["score"] > best["score"]:
                return made
        return best

    def known_resume(self, path: str) -> bool:
        """Only these files may be copied or opened from the control center."""
        target = Path(path).resolve()
        from . import resumes

        allowed = [p.resolve() for p in resumes.resume_files(self.config)]
        return target in allowed or (self.tailored_dir.resolve() in target.parents and target.suffix == ".pdf")

    def reply(self, job_id: int, refresh: bool = False) -> dict[str, Any]:
        """A reply for this post: a LinkedIn comment/DM, or an email body when the post asks
        for email. Kept, so it's there next time; `refresh` writes a new one."""
        job = self.hub.job(job_id)
        if job is None:
            raise ValueError(f"no job #{job_id}")
        key = f"reply:{job_id}"
        if not refresh and (saved := self.hub.get(key)):
            data = json.loads(saved)
        else:
            llm = self._llm_factory() if self._llm_factory else None
            if llm is None:
                raise RuntimeError("no AI model available")
            channel = ("email" if "@" in (job.get("contact") or "") else
                       "a LinkedIn message to the person who posted it" if job["source"] == "linkedin_post" else
                       "a proposal on the freelance site" if job["source"] in {"freelancer", "truelancer"} else
                       "the application form's message box")
            response = complete(llm, 
                model=WRITE_MODEL, reasoning_effort="low",
                messages=[{"role": "user", "content": REPLY_PROMPT.format(
                    name=self.config["from_name"] or "the applicant", channel=channel, about=self._about(job),
                    job=self._posting(job))}],
                response_format={"type": "json_schema", "json_schema": {"name": "reply", "strict": True,
                                                                        "schema": REPLY_SCHEMA}},
            )
            data = json.loads(response.choices[0].message.content or "{}") | {"channel": channel}
            data["text"], data["subject"] = fix_links(data["text"], self.config), fix_links(data["subject"], self.config)
            self.hub.put(key, json.dumps(data))
        return data | {"resume": self.best_resume(job_id), "to_email": self.email_for(job)}

    def analyse_paste(self, text: str, url: str = "") -> dict[str, Any]:
        """A post you pasted: read and scored, saved as a job, a reply written and the best
        résumé chosen; a tailored one is tried whenever yours score below "paste_tailor_below"
        (85): you picked this post yourself, so it's worth the extra minute."""
        from . import jobs as jobs_mod

        cfg_path = self.config_path.parent / "jobs.json"
        job = jobs_mod.from_paste(text, url, jobs_mod.load_config(cfg_path), self._llm_factory() if self._llm_factory else None)
        job_id = self.hub.add_job(job)
        if job_id is None:  # pasted before: use the stored one
            from .hub import dedupe_key

            rows = self.store._rows("SELECT id FROM jobs WHERE (source = 'pasted' AND external_id = ?) OR dedupe_key = ? "
                                    "ORDER BY id DESC LIMIT 1", (job["external_id"], dedupe_key(job["title"], job.get("company", ""))))
            job_id = rows[0]["id"]
        self.hub.set_job_status(job_id, "saved")  # you chose it: it stays, however old it gets
        resume = self.best_resume(job_id, int(self.config.get("paste_tailor_below", 85)))
        reply = self.reply(job_id) | {"resume": resume}
        stored = self.hub.job(job_id) or job
        return {"job_id": job_id, "title": stored["title"], "company": stored.get("company", ""),
                "location": stored.get("location", ""), "fit": stored["fit"], "fit_reasons": stored["fit_reasons"],
                "experience": stored.get("experience", ""), "salary": stored.get("salary", ""), "url": stored.get("url", ""),
                **reply}

    def email_for(self, job: dict[str, Any]) -> str:
        """Where to email this application: an address in the post, else the company's
        published hr@/careers@ (only what's already looked up: no slow search here)."""
        known = contacts.cached(self.hub, job["company"]) if job.get("company") else None
        return contacts.best_email(job, known)

    def mail(self, job_id: int, to_email: str = "", text: str = "", subject: str = "") -> dict[str, Any]:
        """Everything for "Open mail": the reply as an Outreach item (so "Mark sent" works), a
        Gmail compose link with address, subject and text filled in, and the résumé to attach.
        `text`/`subject`/`to_email` are the user's edits from the panel."""
        job = self.hub.job(job_id)
        if job is None:
            raise ValueError(f"no job #{job_id}")
        reply = self.reply(job_id)
        email = (to_email or reply["to_email"]).strip()
        if not email:
            raise ValueError("no email address for this job: use Find contacts, or type one in")
        resume = reply["resume"]
        subject = subject or reply.get("subject") or f"Application: {job['title']}"
        body = text or reply["text"]
        existing = self.store._rows("SELECT id FROM outreach WHERE job_id = ? AND status = 'draft'", (job_id,))
        if existing:
            item_id = existing[0]["id"]
            self.store._write("UPDATE outreach SET to_email = ?, subject = ?, body = ?, attachment = ? WHERE id = ?",
                              (email, subject, body, resume["file"] if resume else "", item_id))
        else:
            item_id = self.store._write(
                "INSERT INTO outreach (job_id, company, to_email, subject, body, attachment, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, job.get("company", ""), email, subject, body, resume["file"] if resume else "",
                 datetime.now().isoformat(timespec="seconds")))
        item = self.item(item_id) or {}
        return {"id": item_id, "to_email": email, "gmail_url": item.get("gmail_url", ""),
                "resume": resume}

    def _posting(self, job: dict[str, Any]) -> str:
        return (f"Title: {job['title']}\nCompany: {job.get('company') or 'not named'}\n"
                f"Location: {job.get('location', '')} ({job.get('work_mode', '')})\n"
                f"Experience: {job.get('experience', '')}\nSkills: {', '.join(job.get('skills', []))}\n"
                f"Contact / how to apply: {job.get('contact', '')}\n\n{job.get('description', '')[:2200]}")

    def auto_draft(self) -> int:
        """Drafts for the best new jobs that don't have one yet (runs after the nightly search)."""
        cfg = self.config
        made = 0
        for job in self.hub.jobs(status="new", min_fit=int(cfg["auto_draft_min_fit"]), limit=80,
                                 include_description=False):
            if made >= int(cfg["auto_draft_per_run"]):
                break
            if self.store._rows("SELECT 1 FROM outreach WHERE job_id = ? LIMIT 1", (job["id"],)):
                continue
            try:
                result = self.draft(job["id"])
            except Exception as exc:  # rate limit, network: the rest wait for the next run
                self.log(f"outreach: draft for job #{job['id']} failed: {exc!r}")
                break
            if isinstance(result, dict):
                made += 1
        if made:
            self.announce(f"{made} application email{'s' * (made != 1)} drafted for your best new jobs. Open the "
                          "control center (Outreach) to review and send them.")
        return made

    def today_cap(self, now: datetime | None = None) -> int:
        """How many emails may go out today: "daily_target", reached by a warm-up (start at
        "warmup_start", "warmup_step" more each day since the first auto-sent email), so a
        personal Gmail isn't flagged for going from 0 to 100 overnight."""
        cfg, today = self.config, (now or datetime.now()).date()
        target = min(int(cfg["auto_send_per_day"]), int(cfg["daily_target"]))
        first = self.hub.get("auto_send:first_day")
        days = (today - datetime.fromisoformat(first).date()).days if first else 0
        return max(1, min(target, int(cfg["warmup_start"]) + int(cfg["warmup_step"]) * days))

    def sent_today(self, now: datetime | None = None) -> int:
        today = (now or datetime.now()).date().isoformat()
        return len(self.store._rows("SELECT 1 FROM outreach WHERE status = 'sent' AND sent_at >= ?", (today,)))

    def auto_send(self, smtp: Callable[[], Any] | None = None, now: datetime | None = None,
                  paced: bool = False) -> list[dict[str, Any]]:
        """Auto-apply: sends drafts with the résumé attached until today's cap (today_cap).
        Job applications go first, best fit first (fit >= "auto_send_min_fit"), then cold
        emails to companies. `paced` (the all-day loop) spreads the day's emails over
        "send_window" and sends a few at a time. A draft send() refuses (a [placeholder],
        no résumé) is left for you. Off unless "auto_send" is true."""
        cfg, now = self.config, now or datetime.now()
        if not cfg.get("auto_send"):
            return []
        today = now.date().isoformat()
        if smtp is None and not self.can_send():
            if not self.hub.get(f"auto_send:warned:{today}"):  # once a day, not after every search
                self.hub.put(f"auto_send:warned:{today}", "1")
                self.announce("Auto-apply is on but can't send yet: add NAMAN_GMAIL_APP_PASSWORD to .env "
                              "(a Google App Password). Your drafts are waiting in Outreach.")
            return []
        cap, done = self.today_cap(now), self.sent_today(now)
        left = cap - done
        if paced:  # by this point in the window, this share of the day's emails may be out
            start, end = (datetime.combine(now.date(), datetime.strptime(t, "%H:%M").time())
                          for t in cfg["send_window"])
            if not start <= now <= end:
                return []
            share = (now - start) / (end - start)
            left = min(left, int(cap * share) + 1 - done, int(cfg["send_per_tick"]))
        if left <= 0:
            return []
        jobs_first, cold = [], []
        for item in self.items("draft"):
            job = self.hub.job(item["job_id"]) if item["job_id"] else None
            if job and job["fit"] >= int(cfg["auto_send_min_fit"]):
                jobs_first.append((-job["fit"], item, job))
            elif not item["job_id"] and cfg.get("cold_emails", True):
                cold.append((item["id"], item, None))
        sent = []
        for _, item, job in sorted(jobs_first, key=lambda c: c[0]) + sorted(cold, key=lambda c: c[0]):
            if len(sent) >= left:
                break
            try:
                self.send(item["id"], smtp=smtp)
            except ValueError as exc:  # needs you: a [placeholder], no address, no résumé file
                self.log(f"outreach: auto-apply skipped #{item['id']}: {exc}")
                continue
            except (RuntimeError, OSError) as exc:  # Gmail refused or offline: try next time
                self.log(f"outreach: auto-apply stopped: {exc!r}")
                break
            if not self.hub.get("auto_send:first_day"):
                self.hub.put("auto_send:first_day", today)
            sent.append({"id": item["id"], "title": job["title"] if job else "cold email",
                         "company": item["company"] or (job or {}).get("company", ""),
                         "to_email": item["to_email"], "fit": job["fit"] if job else None})
        if sent:
            applied = [x for x in sent if x["fit"] is not None]
            colds = [x for x in sent if x["fit"] is None]
            parts = []
            if applied:
                lines = "\n".join(f"- {x['title']} at {x['company'] or x['to_email']} ({x['fit']})" for x in applied[:3])
                more = f"\n(+{len(applied) - 3} more)" if len(applied) > 3 else ""
                parts.append(f"Applied to {len(applied)} job{'s' * (len(applied) != 1)} by email, résumé attached:\n{lines}{more}")
            if colds:
                names = ", ".join(x["company"] or x["to_email"] for x in colds[:3])
                parts.append(f"Cold-emailed {len(colds)} compan{'ies' if len(colds) != 1 else 'y'} ({names}"
                             f"{', ...' if len(colds) > 3 else ''}), résumé attached.")
            self.announce("\n".join(parts) + f"\nToday: {done + len(sent)}/{cap}.")
        return sent

    # --- cold emails: companies, not job posts -------------------------------------------

    def cold_resume(self) -> str:
        cfg = self.config
        master = cfg.get("master_resume") or ""
        if master and Path(master).is_file():
            return master
        return resume_by_title("data analyst machine learning ai engineer", cfg)

    def write_cold(self, company: dict[str, Any]) -> tuple[str, str]:
        llm = self._llm_factory() if self._llm_factory else None
        if llm is None:
            raise RuntimeError("no AI model available to write the email")
        facts = self.store.relevant_memories("data analysis machine learning AI project python", 12)
        facts = [m["fact"] for m in facts if not m["fact"].startswith(("Private:", "Personal contact", "Work contact"))]
        about_me = self.store.profile().split("- Facts starting")[0] + "\n" + "\n".join(f"- {f}" for f in facts)
        roles = ", ".join(sorted({j["title"] for j in company.get("jobs", [])})[:4]) or "none listed"
        response = complete(llm, model=WRITE_MODEL, reasoning_effort="low", messages=[{"role": "user", "content":
            COLD_PROMPT.format(name=self.config["from_name"] or "the applicant", company=company["company"],
                               about_company=(company.get("about") or "nothing known")[:600], roles=roles,
                               about=about_me)}],
            response_format={"type": "json_schema", "json_schema": {"name": "email", "strict": True,
                                                                    "schema": WRITE_SCHEMA}})
        data = json.loads(response.choices[0].message.content or "{}")
        return fix_links(data["subject"].strip(), self.config), fix_links(data["body"].strip(), self.config)

    def cold_draft(self, company: dict[str, Any]) -> dict[str, Any] | str:
        """A cold email to a company's published hiring address (careers@/hr@ first)."""
        emails = sorted((e["email"] for e in company.get("emails", [])), key=contacts.rank)
        email = next((e for e in emails if not self.contacted(e)), "")
        if not email:
            return f"error: no new address for {company['company']}"
        if self.store._rows("SELECT 1 FROM outreach WHERE lower(company) = lower(?) AND job_id IS NULL LIMIT 1",
                            (company["company"],)):
            return f"error: {company['company']} already has a cold email"
        resume = self.cold_resume()
        if not resume:
            return "error: no résumé file to attach (set master_resume in data/outreach.json)"
        subject, body = self.write_cold(company)
        item_id = self.store._write(
            "INSERT INTO outreach (job_id, company, to_email, subject, body, attachment, created) VALUES (NULL, ?, ?, ?, ?, ?, ?)",
            (company["company"], email, subject, body, resume, datetime.now().isoformat(timespec="seconds")))
        return self.item(item_id) or {}

    def auto_cold(self, limit: int = 10) -> int:
        """Cold drafts for looked-up companies with a published address nobody has written to,
        enough to keep today's sending going. Best-matching companies first."""
        if not self.config.get("cold_emails", True):
            return 0
        waiting = len([r for r in self.items("draft") if not r["job_id"]])
        need = min(limit, max(0, self.today_cap() - self.sent_today() - waiting))
        made = 0
        for company in contacts.companies(self.hub):
            if made >= need:
                break
            if not company["emails"]:
                continue
            try:
                result = self.cold_draft(company)
            except Exception as exc:  # rate limit, network: the rest wait for the next round
                self.log(f"outreach: cold draft for {company['company']} failed: {exc!r}")
                break
            if isinstance(result, dict):
                made += 1
        return made
