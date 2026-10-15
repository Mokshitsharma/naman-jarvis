"""What's new on your GitHub: stars, forks, issues and followers, from the public API.

No token needed (the public API allows 60 requests an hour; a check is two). Each check
is compared with the last one, kept in the hub, and the differences become short
notifications ("ToolForge got a new star"). The first check only remembers where things
stand, so you don't get a burst about everything that already exists.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

API = "https://api.github.com"


def get(path: str) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "naman-assistant"}
    if token := os.environ.get("GITHUB_TOKEN"):  # optional: a higher rate limit
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(API + path, headers=headers), timeout=15) as res:
        return json.load(res)


def snapshot(user: str, fetch: Callable[[str], Any] = get) -> dict[str, Any]:
    repos = fetch(f"/users/{user}/repos?per_page=100&type=owner&sort=updated")
    profile = fetch(f"/users/{user}")
    return {"followers": profile.get("followers", 0),
            "repos": {r["name"]: {"stars": r["stargazers_count"], "forks": r["forks_count"],
                                  "issues": r["open_issues_count"]} for r in repos if not r.get("fork")}}


def changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """What went up since the last check, as short sentences. Things going down (an
    unstar, a closed issue) aren't news."""
    out = []
    gained = new["followers"] - old.get("followers", new["followers"])
    if gained > 0:
        out.append(f"You have {gained} new GitHub follower{'s' * (gained != 1)} ({new['followers']} now).")
    before = old.get("repos", {})
    for name, now in new["repos"].items():
        was = before.get(name)
        if was is None:
            if before:  # a repo you just made (not the first check)
                out.append(f"New repo on your GitHub: {name}.")
            continue
        for key, one, many in (("stars", "a new star", "new stars"), ("forks", "a new fork", "new forks"),
                               ("issues", "a new issue or PR", "new issues or PRs")):
            diff = now[key] - was.get(key, now[key])
            if diff > 0:
                out.append(f"{name} got {one if diff == 1 else f'{diff} {many}'}.")
    return out
