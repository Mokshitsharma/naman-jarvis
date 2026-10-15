"""Posts worth reading: new tools, launches and hiring posts, for the control center.

- LinkedIn: public posts found by web search (site:linkedin.com/posts) on the topics in
  data/feed.json, from the past week. Reading someone's LinkedIn feed needs their login,
  which Naman doesn't use; public posts are what search engines can see.
- Hacker News: launches ("Show HN", "launch", "open source") with real traction.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .hub import Hub

DEFAULT_CONFIG: dict[str, Any] = {
    "linkedin_topics": ["new AI tool launch", "Claude Code", "AI agents open source", "LLM developer tools",
                        "hiring AI ML engineer India fresher", "logistics tech India startup",
                        "data science internship India"],
    "hn_queries": ["Show HN", "launch", "open source AI"],
    "hn_min_points": 80,
    "per_topic": 6,
}


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        return {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    except ValueError:
        return dict(DEFAULT_CONFIG)


def linkedin_posts(topic: str, limit: int) -> list[dict[str, Any]]:
    from datetime import date

    from .jobs import web_search

    # The engines' date filter finds almost nothing for LinkedIn posts; the year keeps it recent.
    results = web_search(f"site:linkedin.com/posts {topic} {date.today().year}", limit)
    return [{"source": "linkedin", "url": r["href"], "title": r.get("title", "").replace(" | LinkedIn", ""),
             "snippet": r.get("body", ""), "topic": topic}
            for r in results if "linkedin.com/posts" in r.get("href", "")]


def hacker_news(query: str, min_points: int, client: httpx.Client) -> list[dict[str, Any]]:
    since = int(time.time()) - 3 * 86400
    r = client.get("https://hn.algolia.com/api/v1/search_by_date",
                   params={"query": query, "tags": "story",
                           "numericFilters": f"points>{min_points},created_at_i>{since}"})
    if r.status_code != 200:
        return []
    return [{"source": "hackernews", "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "title": h.get("title", ""), "snippet": f"{h.get('points', 0)} points, {h.get('num_comments', 0)} comments",
             "topic": query, "score": h.get("points", 0)} for h in r.json().get("hits", [])]


def refresh(hub: Hub, config_path: Path, log=print) -> dict[str, Any]:
    config = load_config(config_path)
    found: list[dict[str, Any]] = []
    for topic in config["linkedin_topics"]:
        try:
            found += linkedin_posts(topic, int(config["per_topic"]))
        except Exception as exc:  # search engines rate limit now and then: skip the topic
            log(f"feed: {topic!r} failed: {exc.__class__.__name__}")
        time.sleep(2)
    with httpx.Client(timeout=30.0) as client:
        for query in config["hn_queries"]:
            try:
                found += hacker_news(query, int(config["hn_min_points"]), client)
            except (httpx.HTTPError, ValueError) as exc:
                log(f"feed: hn {query!r} failed: {exc.__class__.__name__}")
    added = [item for item in found if item["title"] and hub.add_post(item)]
    log(f"feed: {len(found)} seen, {len(added)} new")
    return {"seen": len(found), "added": len(added)}
