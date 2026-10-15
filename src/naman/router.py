"""Skill router: picks which toolsets a command needs before the main model runs.

Sending all ~40 tool definitions with every request would burn Groq's free per-minute
and per-day token limits in a few turns. A small, fast model reads the command and a
one-line description of each toolset (a few hundred tokens) and returns the sets to
load. Keyword rules add any pack the model missed, and take over completely if the
call fails, so routing never blocks Naman.

Obvious commands skip the model call (it costs 0.5-1 s per turn): small talk, "open X",
and short single actions whose keywords clearly name the pack ("volume 30", "play lo-fi",
"remind me in 10 minutes", "weather tomorrow"). Questions and anything multi-step still
go to the model, since they may need a pack no keyword names (Pikachu -> PokéAPI).
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from .tools import Toolset

ROUTER_MODEL = "openai/gpt-oss-20b"
MAX_STEPS = 8  # a longer plan is a job for a routine, or split wrongly

PROMPT = """\
You plan for a laptop assistant. For the user's latest message give:

sets: the toolsets needed. Choose every set that might be needed; when unsure, include \
it. Chatting or general knowledge the assistant already has needs no set. Opening apps, \
websites, files and folders is always available, so it needs no set either. If the \
message follows up on the previous turn ("louder", "do that again", "and tomorrow?"), \
include the previous turn's sets: {previous}.

steps: what to do, in order, as short commands (2-8 words each). Split a compound \
request into its actions; one step when it is one action; [] for chat or a question \
answered from knowledge. Don't add steps the user didn't ask for.

Examples:
"open spotify and play old songs" -> sets [media], steps ["Open Spotify", "Play old songs on Spotify"]
"close chrome, mute and lock the laptop" -> sets [laptop], steps ["Close Chrome", "Mute the sound", "Lock the laptop"]
"how's the weather in Pune, do I need a jacket?" -> sets [knowledge], steps ["Get Pune's weather"] \
(a question: look it up and answer; don't act unless asked)
"how are you?" -> sets [], steps []

Toolsets:
{catalog}"""

# Offline fallback: regexes per built-in set. MCP sets match on their own names.
KEYWORDS = {
    "tasks": r"\b(tasks?|todo|to-do|to do|deadline|due|project|plan|briefing|my day|plate|finish|finished|done|"
             r"completed|need to|have to|what next|what's next|whats next|next\?|schedule|timetable|free now)\b",
    "jobs": r"\b(jobs?|openings?|hiring|vacanc\w*|apply|applied|resume|cv|linkedin|posts?|control cent\w*|"
            r"dashboard|fit score|internships?)\b",
    "build": r"\b(claude code|build (me )?(a|an|the)|code (it|this|a|an)|create (a|an) (app|project|website|tool)|"
             r"coding agent)\b",
    "laptop": r"\b(volume|louder|quieter|turn (it |the \w+ )?(up|down)|mute|brightness|dim|pause|resume|next song|"
              r"previous|skip|close|switch|tabs?|"
              r"window|type|press|shortcut|copy|paste|clipboard|screenshot|battery|charging|cpu|lock|sleep|"
              r"restart|shut ?down|power)\b",
    "knowledge": r"\b(weather|rain|temperature|forecast|news|headlines?|who|what is|what's|when|where|how much|"
                 r"price|score|search|look up|google|define|meaning|tech|released?|launch\w*|new model)\b",
    "screen": r"\b(screen|read (this|that|it)|look at (this|that)|this (page|error|post|email|job|form)|fill (this|the|it|out)|forms?)\b",
    "media": r"\b(play|songs?|music|video|youtube|spotify|playlist|podcast|listen)\b",
    "messaging": r"\b(whatsapp|message|text|send|tell \w+ that)\b",
    "reminders": r"\b(remind|reminder|timer|alarm|minutes|hours|at \d)\b",
    "routines": r"\b(routine|shortcut|when i say|good morning|good night|mode)\b",
}


# Packs a keyword hit names reliably; "knowledge" only for weather and news.
CLEAR_PACKS = {"laptop", "media", "reminders", "tasks", "routines", "messaging", "jobs", "screen"}
CLEAR_KNOWLEDGE = r"\b(weather|rain|temperature|forecast|news|headlines?)\b"
SMALL_TALK = (r"^(hi|hello|hey|thanks|thank you|ok|okay|cool|great|nice|bye|good ?bye|yes|yeah|no|nope|"
              r"good (morning|afternoon|evening|night)|how are you|what's up|nothing|never ?mind)\b")
DIRECT = r"^(please )?(open|launch|start|close|quit|switch to|go to)\b"
MULTI_STEP = r"\b(and|then|also|after|before|if)\b"


def obvious(message: str, keyword_sets: list[str]) -> bool:
    """True when the keyword rules alone pick the right packs, so the model call can be skipped."""
    text = re.sub(r"^\[voice\]\s*", "", message.lower()).strip(" .!?")
    words = len(text.split())
    if re.search(MULTI_STEP, text) or words > 14:
        return False
    if re.match(SMALL_TALK, text) and words <= 5:
        return True
    if re.match(DIRECT, text) and words <= 8:
        return True
    if not keyword_sets:
        return False
    for name in keyword_sets:
        if name == "knowledge":
            if not re.search(CLEAR_KNOWLEDGE, text):
                return False
        elif name not in CLEAR_PACKS:  # MCP servers and anything new: let the model decide
            return False
    return True


class Router(Protocol):
    def choose(self, message: str, toolsets: dict[str, Toolset], previous: list[str]) -> list[str]: ...


def optional_sets(toolsets: dict[str, Toolset]) -> dict[str, Toolset]:
    return {name: s for name, s in toolsets.items() if not s.always}


class KeywordRouter:
    def choose(self, message: str, toolsets: dict[str, Toolset], previous: list[str]) -> list[str]:
        text = message.lower()
        chosen = []
        for name, s in optional_sets(toolsets).items():
            if name.startswith("mcp:"):
                words = {w for w in re.split(r"[^a-z0-9]+", s.description.lower()) if len(w) > 3}
                hit = any(re.search(rf"\b{re.escape(w)}", text) for w in words)
            else:
                hit = bool(re.search(KEYWORDS.get(name, r"$^"), text))
            if hit:
                chosen.append(name)
        if not chosen and len(text.split()) <= 4:
            chosen = [p for p in previous if p in toolsets]  # short follow-up: "louder", "again"
        return chosen


class GroqRouter:
    def __init__(self, model: str = ROUTER_MODEL) -> None:
        from groq import Groq

        self.client = Groq(max_retries=1, timeout=15.0)
        self.model = model
        self.fallback = KeywordRouter()
        self.skipped = 0  # model calls saved by the fast path

    def choose(self, message: str, toolsets: dict[str, Toolset], previous: list[str]) -> list[str]:
        return self.route(message, toolsets, previous)[0]

    def route(self, message: str, toolsets: dict[str, Toolset], previous: list[str]) -> tuple[list[str], list[str]]:
        """(toolsets to load, plan steps in order). The plan comes out of the same model
        call as the toolsets, so it costs nothing extra; obvious commands get no plan."""
        options = optional_sets(toolsets)
        if not options:
            return [], []
        by_keyword = self.fallback.choose(message, toolsets, previous)
        if obvious(message, by_keyword):
            self.skipped += 1
            return by_keyword, []
        catalog = "\n".join(f"- {name}: {s.description}" for name, s in options.items())
        schema = {
            "type": "object",
            "properties": {"sets": {"type": "array", "items": {"type": "string", "enum": list(options)}},
                           "steps": {"type": "array", "items": {"type": "string"}}},
            "required": ["sets", "steps"],
            "additionalProperties": False,
        }
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": PROMPT.format(previous=", ".join(previous) or "none",
                                                                catalog=catalog)},
                    {"role": "user", "content": message},
                ],
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "plan", "strict": True, "schema": schema}},
                reasoning_effort="low",
            )
            data = json.loads(response.choices[0].message.content or "{}")
            sets = list(data.get("sets", []))
            steps = [str(step).strip() for step in data.get("steps", []) if str(step).strip()]
        except Exception:
            return by_keyword, []
        # Keyword hits are added as a safety net: a missed pack costs a failed command,
        # an extra one only a few hundred tokens.
        sets += by_keyword
        return [s for s in dict.fromkeys(sets) if s in options], steps[:MAX_STEPS]
