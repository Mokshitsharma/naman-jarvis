"""Chat model wrapper. Messages use the OpenAI chat format, which Groq speaks natively."""

from __future__ import annotations

import os
from typing import Any, Protocol

DEFAULT_MODEL = "openai/gpt-oss-120b"
# Groq's free quotas are per model, so a smaller model keeps Naman talking when the
# main one runs out for the day.
DEFAULT_FALLBACK = "openai/gpt-oss-20b"


class ChatModel(Protocol):
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Return one assistant message dict (with `tool_calls` when it wants tools)."""
        ...


class GroqChat:
    def __init__(self, model: str | None = None, fallback: str | None = None) -> None:
        from groq import Groq

        # A few quick retries ride out per-minute limits; daily limits go to the fallback.
        self.client = Groq(max_retries=3)
        self.model = model or os.environ.get("NAMAN_MODEL") or DEFAULT_MODEL
        self.fallback = fallback or os.environ.get("NAMAN_FALLBACK_MODEL") or DEFAULT_FALLBACK

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        import groq

        try:
            return self._with_retry(self.model, messages, tools)
        except groq.RateLimitError:
            if not self.fallback or self.fallback == self.model:
                raise
            return self._with_retry(self.fallback, messages, tools)

    def _with_retry(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """gpt-oss models sometimes emit a malformed tool call (e.g. a tool name with
        '<|channel|>' glued on) and Groq rejects it with `tool_use_failed`. Sampling
        again almost always fixes it."""
        import groq

        for attempt in range(3):
            try:
                return self._chat(model, messages, tools)
            except groq.BadRequestError as exc:
                if "tool_use_failed" not in str(exc) or attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def _chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto"
        )
        msg = response.choices[0].message
        out: dict[str, Any] = {"role": "assistant", "content": collapse_repeat(msg.content or "")}
        if msg.tool_calls:
            out["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"},
                }
                for c in msg.tool_calls
            ]
        return out


def collapse_repeat(text: str) -> str:
    """gpt-oss-20b sometimes writes its answer twice back to back ("Set.Set.", or a
    reworded second copy). Keep the last copy when the two halves say the same thing."""
    import difflib
    import re

    stripped = text.strip()
    for m in re.finditer(r"[.!?]\s*(?=\S)", stripped):
        first, second = stripped[: m.end()].strip(), stripped[m.end():].strip()
        if not first or not second or not 0.6 <= len(second) / len(first) <= 1.6:
            continue
        if difflib.SequenceMatcher(None, first.lower(), second.lower()).ratio() >= 0.8:
            return second
    return text


def complete(client: Any, **kwargs: Any) -> Any:
    """client.chat.completions.create, falling back to the smaller model when the main one has
    used up today's free quota (rate limit): job extraction, résumés and replies keep working."""
    model = kwargs.pop("model")
    fallback = os.environ.get("NAMAN_FALLBACK_MODEL") or DEFAULT_FALLBACK
    try:
        return client.chat.completions.create(model=model, **kwargs)
    except Exception as exc:
        if exc.__class__.__name__ != "RateLimitError" or model == fallback:
            raise
        return client.chat.completions.create(model=fallback, **kwargs)


def friendly_error(exc: Exception) -> str:
    """One short sentence a person can act on (also used as spoken text)."""
    name = exc.__class__.__name__
    text = str(exc).lower()
    if name == "RateLimitError":
        import re

        wait = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", text)
        when = ""
        if wait:
            h, m, s = (float(x) if x else 0 for x in wait.groups())
            minutes = int(h * 60 + m + (1 if s else 0))
            when = f" Try again in about {minutes} minute{'s' * (minutes != 1)}." if minutes else " Try again in a minute."
        if "per day" in text or "tpd" in text:
            return ("The free AI quota is used up for now (Groq allows 200,000 tokens a day per model; it frees up "
                    f"gradually).{when or ' Try again later.'}")
        return f"I'm being rate limited.{when or ' Give me a minute and ask again.'}"
    if name in {"APIConnectionError", "ConnectError", "APITimeoutError"}:
        return "I can't reach the AI service. Check the internet connection."
    if name == "AuthenticationError":
        return "The Groq API key was rejected. Check GROQ_API_KEY in .env."
    return f"Something went wrong ({name}). Try again."
