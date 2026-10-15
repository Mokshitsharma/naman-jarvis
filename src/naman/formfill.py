"""The form helper: reads an application form on the screen and suggests what to type.

You open a job portal or careers page, say "help me fill this form" (or use the orb's
menu), and a small panel lists every field it can see with an answer and a Copy button.
Answers come from your profile, your memories and your résumé; questions about the job
("Why do you want to join us?") are written from what the page shows. Anything it can't
know (expected salary, notice period, date of birth...) is marked for you to fill, never
guessed; type it once, press Remember, and next time it's known.

Nothing is typed or submitted for you: you paste, check and submit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import screen

PROMPT = """\
This is a screenshot of {name}'s screen showing a form (a job portal, a careers page, an \
application). List every field you can see, top to bottom, and the answer {name} should \
put in each.

Rules:
- Use ONLY the facts below (profile, remembered facts, résumé). Copy values exactly \
(email, phone, links, dates, degree, college).
- status "known": the answer is in the facts. status "written": a free-text question about \
the job or motivation, which you write from the facts and what the page shows (2-5 \
sentences, first person, plain, no flattery). status "unknown": the facts don't say \
(salary, notice period, date of birth, address...): answer "" and never guess.
- Dropdowns, radio buttons, checkboxes: the option to pick, as written on screen.
- Skip search boxes, newsletter sign-ups, cookie banners and buttons.
- page: what the page is, e.g. "Application form: Data Analyst at Acme".

Reply with JSON only, no other text:
{{"page": "...", "fields": [{{"label": "...", "answer": "...", "status": "known|written|unknown"}}]}}

Profile:
{profile}

Remembered facts:
{facts}

Résumé:
{resume}"""


def context(store: Any, config: dict[str, Any], cache_dir: Path | None = None) -> dict[str, str]:
    """What the form helper may use: your profile, memories (form answers you saved
    included) and the text of your main résumé."""
    from . import resumes

    resume = ""
    path = resumes.master_path(config)
    if path is not None:
        try:
            resume = resumes.pdf_text(path, cache_dir)[:6000]
        except Exception:
            resume = ""
    facts = [m["fact"] for m in store.recent_memories(300)]
    profile = store.profile()
    if config.get("gmail_account"):
        profile += f"\n- Email for applications: {config['gmail_account']}"
    return {"name": config.get("from_name") or "the user", "profile": profile or "(none)",
            "facts": "\n".join(f"- {f}" for f in facts) or "(none)", "resume": resume or "(none)"}


def parse(text: str) -> dict[str, Any]:
    """The model's JSON, tolerating a code fence or words around it."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {"page": "", "fields": []}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {"page": "", "fields": []}
    fields = []
    for f in data.get("fields") or []:
        label = str(f.get("label") or "").strip()
        if not label:
            continue
        status = f.get("status") if f.get("status") in {"known", "written", "unknown"} else "unknown"
        answer = str(f.get("answer") or "").strip()
        fields.append({"label": label, "answer": answer if status != "unknown" else "",
                       "status": "unknown" if not answer else status})
    return {"page": str(data.get("page") or "").strip(), "fields": fields}


def scan(client: Any, store: Any, config: dict[str, Any], cache_dir: Path | None = None,
         image_url: str | None = None) -> dict[str, Any]:
    return parse(screen.look(client, PROMPT.format(**context(store, config, cache_dir)), image_url))


def remember_answer(store: Any, label: str, value: str) -> int:
    """A form answer you typed, kept so the next form gets it."""
    return store.remember(f"Form answer, {label.strip().rstrip(':*')}: {value.strip()}")
