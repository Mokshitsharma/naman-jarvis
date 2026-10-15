"""Tech updates: model releases, language and framework news, dev tools, explained.

Official feeds (OpenAI, Google, DeepMind, Hugging Face, GitHub, Java, Python, Node.js,
AWS) plus tech press (The Verge, TechCrunch, InfoQ), and Hacker News for what has no feed
(Anthropic, Meta, Mistral, DeepSeek...) and for whatever developers are talking about.
Each new item is read (its page when it can be fetched, else the feed's summary) and
explained by the model: a headline, two sentences, and the details that matter to a
developer, with a category and whether it's big news. Big news becomes a notification.

Only what the sources say is used; the model is told not to add facts. Items older than
a week aren't fetched; the list keeps 30 days.
"""

from __future__ import annotations

import html
import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .hub import Hub
from .llm import complete

FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google AI": "https://blog.google/technology/ai/rss/",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "GitHub Changelog": "https://github.blog/changelog/feed/",
    "Java": "https://inside.java/feed.xml",
    "Python": "https://blog.python.org/feeds/posts/default?alt=rss",
    "Node.js": "https://nodejs.org/en/feed/blog.xml",
    "AWS ML": "https://aws.amazon.com/blogs/machine-learning/feed/",
    "The Verge": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "TechCrunch": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "InfoQ": "https://feed.infoq.com/",
}
# Hacker News: labs without a feed, and big launches in general (only with real traction).
HN_QUERIES = ["Anthropic", "Claude", "Gemini", "OpenAI", "GPT", "Llama", "Mistral", "DeepSeek", "Qwen",
              "release", "launch", "open source model", "Java", "Python", "TypeScript", "Rust"]
HN_MIN_POINTS = 150
MAX_AGE_DAYS = 7
KEEP_DAYS = 30
EXPLAIN_PER_RUN = 25  # free-tier friendly; the rest wait for the next run
MODEL = "openai/gpt-oss-120b"
CATEGORIES = ["AI models", "AI tools & agents", "Languages & frameworks", "Dev tools & cloud", "Industry", "Other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tech_news (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    published TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    headline TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT '',
    important INTEGER NOT NULL DEFAULT 0,
    relevant INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL
);
"""

EXPLAIN_PROMPT = """\
Explain this tech news item for a software/AI developer in India who is job hunting \
(Python, data, ML, LLM apps). Use ONLY what the source text says; never add version \
numbers, dates, benchmarks or features that aren't in it. If the text is thin, say less.

- relevant: false for things a developer wouldn't care about (gadgets, lawsuits with no \
tech change, marketing, funding without a product, opinion pieces); true otherwise.
- headline: what happened, in under 12 words ("OpenAI releases GPT-x with ...").
- summary: two plain sentences.
- details: 3-6 short bullets: what's new or changed, what it replaces or improves, who it's \
for, how to try it or what it means for developers.
- category: one of {categories}.
- important: true only for big news: a new frontier/open model, a major language or \
framework release, a major tool launch. Most items are false.

Source: {source}
Title: {title}
Text:
{text}"""
EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {"relevant": {"type": "boolean"}, "headline": {"type": "string"}, "summary": {"type": "string"},
                   "details": {"type": "array", "items": {"type": "string"}},
                   "category": {"type": "string", "enum": CATEGORIES}, "important": {"type": "boolean"}},
    "required": ["relevant", "headline", "summary", "details", "category", "important"],
    "additionalProperties": False,
}


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def parse_feed(xml: str, source: str) -> list[dict[str, Any]]:
    """RSS <item>s or Atom <entry>s as {source, title, url, published, snippet}."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        fields: dict[str, str] = {}
        for child in el:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "link" and child.get("href") and child.get("rel", "alternate") == "alternate":
                fields.setdefault("link", child.get("href", ""))
            elif child.text and name not in fields:
                fields[name] = child.text
        when = _date(fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date", ""))
        url = (fields.get("link") or "").strip()
        if not url or when is None:
            continue
        out.append({"source": source, "title": _text(fields.get("title", "")), "url": url,
                    "published": when.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "snippet": _text(fields.get("description") or fields.get("summary") or fields.get("content", ""))[:1200]})
    return out


def hacker_news(client: httpx.Client, since: datetime) -> list[dict[str, Any]]:
    seen, out = set(), []
    for query in HN_QUERIES:
        try:
            hits = client.get("https://hn.algolia.com/api/v1/search", params={
                "query": query, "tags": "story",
                "numericFilters": f"points>{HN_MIN_POINTS},created_at_i>{int(since.timestamp())}"}).json()["hits"]
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        for h in hits:
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
            if url in seen:
                continue
            seen.add(url)
            out.append({"source": "Hacker News", "title": h.get("title", ""), "url": url,
                        "published": datetime.fromtimestamp(h["created_at_i"], timezone.utc).isoformat(timespec="seconds"),
                        "snippet": f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments on Hacker News."})
        time.sleep(0.3)
    return out


def article_text(client: httpx.Client, url: str) -> str:
    """The page's paragraphs, when the site lets us read it (many don't: the feed summary
    is used then)."""
    try:
        page = client.get(url, timeout=12).text
    except httpx.HTTPError:
        return ""
    paras = [_text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", page, re.S | re.I)]
    return " ".join(p for p in paras if len(p) > 40)[:5000]


class TechNews:
    def __init__(self, hub: Hub, llm: Callable[[], Any] | None = None, log: Callable[[str], None] = print,
                 client: httpx.Client | None = None) -> None:
        self.hub, self.store, self.log = hub, hub.store, log
        self._llm_factory = llm
        self.client = client or httpx.Client(timeout=15, follow_redirects=True,
                                             headers={"User-Agent": "Mozilla/5.0 (naman personal assistant)"})
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    def items(self, category: str = "", limit: int = 150) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tech_news WHERE relevant = 1 AND headline != ''"
        args: list[Any] = []
        if category:
            sql += " AND category = ?"
            args.append(category)
        rows = self.store._rows(sql + " ORDER BY published DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["details"] = json.loads(r["details"] or "[]")
            r["important"] = bool(r["important"])
        return rows

    def collect(self, now: datetime | None = None) -> int:
        """New items from every source, stored unexplained. Returns how many were new."""
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(days=MAX_AGE_DAYS)
        found: list[dict[str, Any]] = []
        for source, url in FEEDS.items():
            try:
                found += parse_feed(self.client.get(url).text, source)
            except httpx.HTTPError as exc:
                self.log(f"tech news: {source} failed: {exc.__class__.__name__}")
        found += hacker_news(self.client, since)
        added = 0
        stamp = datetime.now().isoformat(timespec="seconds")
        for item in found:
            if datetime.fromisoformat(item["published"]) < since or not item["title"]:
                continue
            with self.store.lock, self.store.db:
                cur = self.store.db.execute(
                    "INSERT OR IGNORE INTO tech_news (url, source, title, published, snippet, created) VALUES (?, ?, ?, ?, ?, ?)",
                    (item["url"], item["source"], item["title"], item["published"], item["snippet"], stamp))
                added += cur.rowcount
        cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        self.store._write("DELETE FROM tech_news WHERE published < ?", (cutoff,))
        return added

    def explain_pending(self, limit: int = EXPLAIN_PER_RUN) -> list[dict[str, Any]]:
        """Explains the newest unexplained items. Returns the important ones."""
        llm = self._llm_factory() if self._llm_factory else None
        if llm is None:
            return []
        rows = self.store._rows("SELECT * FROM tech_news WHERE headline = '' AND relevant = 1 "
                                "ORDER BY published DESC LIMIT ?", (limit,))
        big = []
        for row in rows:
            text = article_text(self.client, row["url"]) if row["source"] != "Hacker News" or "ycombinator" not in row["url"] else ""
            try:
                response = complete(llm, model=MODEL, reasoning_effort="low", messages=[{"role": "user", "content":
                    EXPLAIN_PROMPT.format(categories=", ".join(CATEGORIES), source=row["source"], title=row["title"],
                                          text=(text or row["snippet"] or row["title"])[:4500])}],
                    response_format={"type": "json_schema", "json_schema": {"name": "news", "strict": True,
                                                                            "schema": EXPLAIN_SCHEMA}})
                data = json.loads(response.choices[0].message.content or "{}")
            except Exception as exc:  # rate limit, bad JSON: try again next run
                self.log(f"tech news: explaining #{row['id']} failed: {exc.__class__.__name__}")
                break
            self.store._write("UPDATE tech_news SET headline = ?, summary = ?, details = ?, category = ?, important = ?, "
                              "relevant = ? WHERE id = ?",
                              (data["headline"].strip() or row["title"], data["summary"].strip(),
                               json.dumps([d.strip() for d in data["details"] if d.strip()][:6]), data["category"],
                               int(data["important"]), int(data["relevant"]), row["id"]))
            if data["relevant"] and data["important"]:
                big.append({**row, "headline": data["headline"], "summary": data["summary"]})
        return big

    def refresh(self, announce: Callable[[str], None] | None = None) -> dict[str, Any]:
        added = self.collect()
        big = self.explain_pending()
        if announce and big:
            lines = [f"- {b['headline']}" for b in big[:3]]
            more = f"\n(+{len(big) - 3} more in Tech updates)" if len(big) > 3 else ""
            announce("Big tech news:\n" + "\n".join(lines) + more)
        self.hub.put("last:tech_refresh", datetime.now().isoformat(timespec="seconds"))
        return {"added": added, "important": len(big)}
