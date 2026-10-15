"""Reading the screen: a screenshot goes to a vision model with your question.

"What's on my screen?", "read me this error", "summarise this page", and "apply to the
job on my screen" (the post is read off the screen, then handled like a pasted post).
Nothing is saved: the screenshot lives in memory for the one request. Only the screen
you ask about is captured, only when you ask.
"""

from __future__ import annotations

import base64
import io
import os
import re
from typing import Any

VISION_MODEL = "qwen/qwen3.8-27b"  # Groq, free tier; NAMAN_VISION_MODEL changes it
MAX_SIDE = 1600  # big enough to read text, small enough to send quickly

ASK_PROMPT = """\
This is a screenshot of the user's laptop screen. {question}
Answer from what's visible only; quote text exactly when it matters (errors, numbers, \
names). If something can't be read, say so. Plain text, no markdown."""

POST_PROMPT = """\
This is a screenshot of the user's screen. If it shows a job opening (a job post, a \
LinkedIn hiring post, a listing), transcribe it: the title, company, location, \
requirements, how to apply, and any email address or link, exactly as written. If there \
is no job opening on screen, reply with exactly NONE."""


def capture() -> str:
    """The whole screen (every monitor) as a PNG data URL, scaled down to MAX_SIDE."""
    from PIL import ImageGrab

    image = ImageGrab.grab(all_screens=True)
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def look(client: Any, prompt: str, image_url: str | None = None) -> str:
    # Not llm.complete: its fallback model can't see images.
    response = client.chat.completions.create(model=os.environ.get("NAMAN_VISION_MODEL") or VISION_MODEL, messages=[
        {"role": "user", "content": [{"type": "text", "text": prompt},
                                     {"type": "image_url", "image_url": {"url": image_url or capture()}}]}])
    text = response.choices[0].message.content or ""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models think out loud


def ask(client: Any, question: str = "", image_url: str | None = None) -> str:
    return look(client, ASK_PROMPT.format(question=question or "Describe what the user is looking at."), image_url)


def job_post(client: Any, image_url: str | None = None) -> str:
    """The job post on screen as text, or "" if there isn't one."""
    text = look(client, POST_PROMPT, image_url)
    return "" if text.strip().upper().startswith("NONE") or len(text) < 60 else text
