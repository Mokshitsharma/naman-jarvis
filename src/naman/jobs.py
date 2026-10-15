"""Job finder: fetch openings, pull out the details that matter, score the fit.

Sources (no logins, no API keys):
- LinkedIn's public job-search pages (the ones shown to signed-out visitors), filtered
  to internship / entry / associate level. Search and detail pages, ~1 request a second.
- LinkedIn hiring posts: "We're hiring AI/ML freshers, DM me / mail your CV to ...".
  Many openings are only ever posted this way. Found by web search on public posts
  (site:linkedin.com/posts), each read in full; posts behind the sign-up wall are skipped.
- Freelance projects: Freelancer.com's public API and Truelancer (Upwork, Fiverr and Guru
  block automated access).
- Remotive and Jobicy public APIs for remote roles open to India.

Each new job's text goes to the small model once, which extracts experience, skills,
salary, work mode and contact details (emails, phone numbers, "DM me", apply links).
For hiring posts it also names the role and company and drops posts that aren't
openings (courses, "top 10 tips"...). LinkedIn's job pages rarely publish salary or
recruiter contacts to signed-out visitors, so those stay empty unless the post states
them. The fit score is plain arithmetic (see `fit()`), so it can be explained: skills
45, role 25, experience 20, location 10.

Settings live in data/jobs.json (created on first run; edit it freely).
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .hub import Hub

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126 Safari/537.36"}
TIMEOUT = 20.0
PAUSE_S = 1.0  # between LinkedIn requests: polite, and avoids being rate limited
EXTRACT_MODEL = "openai/gpt-oss-20b"

DEFAULT_CONFIG: dict[str, Any] = {
    "roles": ["Data Analyst", "Data Scientist", "Data Engineer", "AI/ML Engineer", "Machine Learning Engineer",
              "Agentic AI Engineer", "Generative AI Engineer"],
    # Where you want to work: these cities get every role searched; elsewhere only remote,
    # internship and freelance/contract roles are searched (in `country`).
    "locations": [],  # e.g. ["Bengaluru"]
    "country": "India",
    "search_remote": True,
    "search_internships": True,
    "search_freelance": True,
    # Score bonus for what you prefer, shown in the reasons ("remote +10"). On-site roles
    # outside your cities get only a few location points, so they sink down the list.
    "prefer": {"home": 10, "remote": 10, "freelance": 10, "internship": 6},
    "companies": [],
    "hiring_post_queries": [
        "\"we are hiring\" data analyst",
        "hiring data analyst fresher",
        "hiring data analyst intern",
        "\"we are hiring\" data scientist",
        "hiring data scientist fresher",
        "hiring data scientist intern",
        "\"we are hiring\" data engineer",
        "hiring data engineer fresher",
        "hiring data engineer intern",
        "\"we are hiring\" AI/ML engineer",
        "hiring AI/ML engineer fresher",
        "hiring AI/ML engineer intern",
        "\"we are hiring\" machine learning engineer",
        "hiring machine learning engineer fresher",
        "hiring machine learning engineer intern",
        "\"we are hiring\" AI engineer",
        "hiring AI engineer fresher",
        "hiring AI engineer intern",
        "\"we are hiring\" GenAI engineer",
        "hiring GenAI engineer fresher",
        "hiring GenAI engineer intern",
        "\"we are hiring\" agentic AI",
        "hiring agentic AI fresher",
        "hiring agentic AI intern",
        "\"we are hiring\" python developer AI",
        "hiring python developer AI fresher",
        "hiring python developer AI intern",
        "\"#hiring\" data analyst remote",
        "\"#hiring\" AI ML remote",
        "\"looking for\" data analyst intern",
        "\"looking for\" AI ML intern",
        "hiring data science intern remote",
        "freelance data analyst",
        "freelance AI developer"
    ],
    "hiring_posts_per_query": 20,
    "max_post_age_days": 7,
    # Nothing older than this is stored, from any source (and stored jobs drop out of the lists
    # once they pass it, unless saved or applied).
    "max_age_days": 7,
    "remote_boards": True,
    "posted_within_hours": 72,
    "max_new_per_run": 40,
    "max_new_posts_per_run": 60,
    "experience_years": 1,
    # Words in a job's location that mean "near you" (lower case), e.g. ["bengaluru", "karnataka"].
    "home": [],
    "skills": ["python", "sql", "javascript", "typescript", "java", "dart", "node.js", "express", "mongodb",
               "postgresql", "react", "next.js", "react native", "flutter", "fastapi", "machine learning",
               "deep learning", "pytorch", "scikit-learn", "xgboost", "lightgbm", "nlp", "llm", "langchain",
               "langgraph", "rag", "generative ai", "reinforcement learning", "shap", "power bi", "pandas", "numpy",
               "data analysis", "statistics", "docker", "git", "rest api", "aws", "linux"],
    "role_words": ["ai", "ml", "machine learning", "data scien", "data analy", "data engin", "analyst", "agentic",
                   "agent", "genai", "generative", "llm", "nlp", "analytics", "full stack", "fullstack", "software",
                   "backend", "python", "developer", "engineer"],
    # Freelance projects (Freelancer.com's public API, Truelancer). Upwork and Fiverr block
    # automated access, so they can't be searched.
    "freelance_sites": ["freelancer", "truelancer"],
    "freelance_queries": ["data analysis", "data science", "machine learning", "python", "AI agent",
                          "power bi", "data engineering", "chatbot"],
    "max_new_freelance_per_run": 25,
    # Free AI quota: only this many new jobs per run are read by the model (the most promising
    # first); the rest by keyword rules. Leaves room for the posts you paste yourself.
    "ai_extract_per_run": 30,
    # Freelance pay floor, in US dollars: lower-paying gigs are capped at 40 ("pay too low").
    "min_hourly_usd": 8,
    "min_fixed_usd": 50,
    # Remote boards: these terms are searched on Remotive and Jobicy.
    "remote_terms": ["data analyst", "data science", "data engineer", "machine learning", "ai agent"],
}


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        return {**DEFAULT_CONFIG, **json.loads(path.read_text(encoding="utf-8"))}
    except ValueError:
        return dict(DEFAULT_CONFIG)


# --- small HTML helpers ---------------------------------------------------------------------


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(br|/p|/li|/div|/h\d)\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<li[^>]*>", "\n- ", fragment, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text, re.S)
    return _text(m.group(1)) if m else ""


# --- sources ----------------------------------------------------------------------------------


def linkedin_search(keywords: str, location: str, hours: int, client: httpx.Client,
                    extra: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """`extra`: LinkedIn's own filters, e.g. {"f_WT": "2"} remote only, {"f_JT": "I"} internships."""
    params = {"keywords": keywords, "location": location, "f_TPR": f"r{hours * 3600}", "f_E": "1,2,3", "start": 0}
    r = client.get("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                   params={**params, **(extra or {})})
    if r.status_code != 200:
        return []
    jobs = []
    for card in r.text.split("<li")[1:]:
        job_id = re.search(r"urn:li:jobPosting:(\d+)", card)
        title = _first(r'base-search-card__title[^>]*>(.*?)</h3>', card)
        if not job_id or not title:
            continue
        jobs.append({
            "source": "linkedin", "external_id": job_id.group(1), "title": title,
            "company": _first(r'base-search-card__subtitle[^>]*>(.*?)</h4>', card),
            "location": _first(r'job-search-card__location[^>]*>(.*?)</span>', card),
            "posted": (re.search(r'datetime="([^"]+)"', card) or [None, ""])[1],
            "url": f"https://www.linkedin.com/jobs/view/{job_id.group(1)}",
            **({"work_mode": "remote"} if (extra or {}).get("f_WT") == "2" else {}),
            **({"internship": True} if (extra or {}).get("f_JT") == "I" else {}),
            **({"freelance": True} if (extra or {}).get("f_JT") == "C,T" else {}),
        })
    return jobs


def linkedin_details(job: dict[str, Any], client: httpx.Client) -> dict[str, Any]:
    r = client.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['external_id']}")
    if r.status_code != 200:
        return job
    page = r.text
    criteria = dict(zip(re.findall(r'job-criteria-subheader[^>]*>\s*(.*?)\s*<', page, re.S),
                        re.findall(r'job-criteria-text[^>]*>\s*(.*?)\s*<', page, re.S)))
    description = _first(r'show-more-less-html__markup[^>]*>(.*?)</div>', page)
    extra = [f"{k}: {html.unescape(v.strip())}" for k, v in criteria.items()]
    salary = _first(r'salary[^>]*compensation__salary[^>]*>(.*?)<', page)
    return {**job, "description": description, "criteria": "; ".join(extra), "salary": salary}


def web_search(query: str, limit: int, attempts: int = 3) -> list[dict[str, Any]]:
    """Web search with retries: the search engines throttle bursts of queries, so wait
    and try again instead of losing the query. Their date filters find almost nothing
    for LinkedIn posts, so callers filter by date themselves."""
    from ddgs import DDGS

    for attempt in range(attempts):
        try:
            return DDGS().text(query, max_results=limit)
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    return []


def post_time(url: str) -> datetime | None:
    """When a LinkedIn post was published, read from its id: the id's top bits are the Unix
    time in milliseconds (activity-7472635279254794241 -> 2026-09-29). No page fetch needed."""
    m = re.search(r"activity[-:](\d{15,})", url)
    if not m:
        return None
    try:
        return datetime.fromtimestamp((int(m.group(1)) >> 22) / 1000, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def hiring_posts(query: str, limit: int, client: httpx.Client, max_age_days: int = 7) -> list[dict[str, Any]]:
    """Public LinkedIn posts announcing openings, found by web search and read in full.

    The search engines' own date filter finds almost nothing for LinkedIn posts, so the
    year goes in the query and each post's publish date is checked here instead."""
    oldest = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    phrase = "" if "hiring" in query.lower() or "looking for" in query.lower() else '"hiring" '
    results = web_search(f"site:linkedin.com/posts {phrase}{query} {oldest.year}", limit)
    posts = []
    for r in results:
        url = r.get("href", "").split("?")[0]
        if "linkedin.com/posts/" not in url:
            continue
        when = post_time(url)
        if when is not None and when < oldest:
            continue  # too old: known from the id, so the page isn't even fetched
        try:
            page = client.get(url).text
        except httpx.HTTPError:
            continue
        body = _first(r'attributed-text-segment-list__content[^>]*>(.*?)</p>', page)
        if len(body) < 80:  # the sign-up wall, or not a real post
            continue
        author, profile, posted = "", "", ""
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            if data.get("@type") == "SocialMediaPosting":
                who = data.get("author") or {}
                author, profile, posted = who.get("name", ""), who.get("url", ""), data.get("datePublished", "")
                break
        if not posted and when is not None:  # some pages carry no date: the id always does
            posted = when.isoformat(timespec="seconds")
        try:
            if posted and datetime.fromisoformat(posted.replace("Z", "+00:00")) < oldest:
                continue  # an old post: the opening is probably filled
        except ValueError:
            pass
        activity = re.search(r"activity-(\d+)", url)
        posts.append({"source": "linkedin_post", "external_id": activity.group(1) if activity else url, "url": url,
                      "title": post_title(r.get("title", ""), body), "company": "",
                      "location": "", "posted": posted, "description": body, "author": author,
                      "author_url": profile,
                      "links": list(dict.fromkeys(re.findall(r"https?://(?:lnkd\.in|forms\.gle|bit\.ly)/[\w-]+", body)))})
        time.sleep(PAUSE_S)
    return posts


HIRING_PREFIX = re.compile(r"^\s*(?:(?:we\s*(?:'re|’re| are)?|now|urgently)\s+)?(?:hiring|looking for)\s*[:!\-–—|]*\s*",
                           re.I)


def post_title(search_title: str, body: str) -> str:
    """A readable title for a post: the search result's title unless it's only hashtags,
    else the post's first line with words in it; "We're hiring:" in front is dropped."""
    for candidate in [search_title.split("|")[0], *body.splitlines()[:6]]:
        text = re.sub(r"#\w+|[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", candidate).strip(" .:-|*")
        stripped = HIRING_PREFIX.sub("", text).strip(" .:-|*")
        for option in (stripped, text):  # "We're hiring: Data Analyst" -> "Data Analyst"; else the line as is
            if len(re.findall(r"[A-Za-z]{3,}", option)) >= 2:
                return option[:120]
    return "Hiring post"


def freelancer_projects(query: str, client: httpx.Client) -> list[dict[str, Any]]:
    """Open projects on Freelancer.com (its public API: no login needed to search)."""
    r = client.get("https://www.freelancer.com/api/projects/0.1/projects/active/",
                   params={"query": query, "limit": 20, "full_description": "true", "job_details": "true",
                           "sort_field": "time_updated"})
    if r.status_code != 200:
        return []
    out = []
    for pr in r.json().get("result", {}).get("projects", []):
        budget, cur = pr.get("budget") or {}, (pr.get("currency") or {}).get("code", "")
        pay = ""
        if budget.get("minimum"):
            pay = f"{cur} {budget['minimum']:g}-{budget.get('maximum') or budget['minimum']:g}"
            pay += " /hour" if pr.get("type") == "hourly" else " fixed"
        out.append({"source": "freelancer", "external_id": str(pr["id"]), "title": html.unescape(pr["title"]),
                    "company": "Freelancer.com client", "location": "Remote", "work_mode": "remote", "freelance": True,
                    "url": f"https://www.freelancer.com/projects/{pr.get('seo_url', pr['id'])}",
                    "posted": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(pr.get("time_submitted") or 0)),
                    "salary": pay, "description": html.unescape(pr.get("description") or pr.get("preview_description", "")),
                    "criteria": "Skills: " + ", ".join(j.get("name", "") for j in pr.get("jobs") or [])})
    return out


def _find_lists(node: Any, key: str) -> list[list[dict[str, Any]]]:
    """Every list of dicts with `key` inside a JSON tree (Next.js pages bury their data)."""
    found = []
    if isinstance(node, list):
        if node and all(isinstance(x, dict) for x in node) and key in node[0]:
            found.append(node)
        for x in node:
            found += _find_lists(x, key)
    elif isinstance(node, dict):
        for v in node.values():
            found += _find_lists(v, key)
    return found


def truelancer(query: str, client: httpx.Client) -> list[dict[str, Any]]:
    """Open projects on Truelancer (Indian freelance marketplace), from the data its search page embeds."""
    r = client.get("https://www.truelancer.com/freelance-jobs", params={"q": query})
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if r.status_code != 200 or not m:
        return []
    out, seen = [], set()
    for items in _find_lists(json.loads(m.group(1)), "budget"):
        for pr in items:
            if not pr.get("id") or not pr.get("title") or pr["id"] in seen:
                continue
            seen.add(pr["id"])
            kind = ((pr.get("displayjobtype") or {}).get("displayvalue") or "").lower()
            pay = f"{pr.get('currency', '')} {pr['budget']}{' /hour' if kind == 'hourly' else ''}" if pr.get("budget") else ""
            slug = re.sub(r"[^a-z0-9]+", "-", pr["title"].lower()).strip("-")
            out.append({"source": "truelancer", "external_id": str(pr["id"]), "title": html.unescape(pr["title"]),
                        "company": "Truelancer client", "location": "Remote", "work_mode": "remote", "freelance": True,
                        "url": f"https://www.truelancer.com/freelance-project/{slug}-{pr['id']}",
                        "posted": pr.get("created_at", ""), "salary": pay,
                        "description": _text(html.unescape(pr.get("description") or ""))})
    return out


def remotive(search: str, client: httpx.Client) -> list[dict[str, Any]]:
    r = client.get("https://remotive.com/api/remote-jobs", params={"search": search, "limit": 20})
    if r.status_code != 200:
        return []
    return [{"source": "remotive", "external_id": str(j["id"]), "title": j["title"], "company": j["company_name"],
             "location": j.get("candidate_required_location") or "Remote", "work_mode": "remote",
             "url": j["url"], "posted": j.get("publication_date", ""), "salary": j.get("salary") or "",
             "freelance": j.get("job_type") in {"freelance", "contract"},
             "description": _text(j.get("description", ""))} for j in r.json().get("jobs", [])]


def jobicy(tag: str, client: httpx.Client) -> list[dict[str, Any]]:
    r = client.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 20, "tag": tag})
    if r.status_code != 200:
        return []
    out = []
    for j in r.json().get("jobs", []):
        pay = ""
        if j.get("annualSalaryMin"):
            pay = f"{j.get('salaryCurrency', '')} {j['annualSalaryMin']}-{j.get('annualSalaryMax', '')}/year".strip()
        out.append({"source": "jobicy", "external_id": str(j["id"]), "title": html.unescape(j["jobTitle"]),
                    "company": j.get("companyName", ""), "location": j.get("jobGeo") or "Remote",
                    "work_mode": "remote", "url": j["url"], "posted": j.get("pubDate", ""), "salary": pay,
                    "description": _text(j.get("jobDescription", ""))})
    return out


# --- extraction and scoring ---------------------------------------------------------------------

EXTRACT_PROMPT = """\
Extract facts from this job post. Use only what the post says; never guess.
- is_job_post: true if it announces a specific opening someone can apply to; false for \
courses, paid training, tips, generic "follow for jobs" or job-list-selling posts.
- role: the job title ("AI/ML Engineer Intern"); company: the hiring company, or "" if not named.
- location: city/country or "Remote", or "".
- summary: one sentence on what the job is.
- experience: the experience asked for, as written ("0-2 years", "Freshers", "3+ years"), or "Not stated".
- min_years: the minimum years as a number, 0 for freshers/interns, -1 if not stated.
- skills: up to 12 key skills/technologies, lowercase.
- salary: pay, CTC or stipend as written, or "".
- contact: every way to reach the hirer written in the post: emails, phone/WhatsApp \
numbers, "DM me", named recruiters; or "".
- apply: how to apply (a link, "email your CV to ...", "comment interested"), or "".
- work_mode: remote, hybrid, onsite or unknown."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_post": {"type": "boolean"}, "role": {"type": "string"}, "company": {"type": "string"},
        "location": {"type": "string"}, "summary": {"type": "string"}, "experience": {"type": "string"},
        "min_years": {"type": "number"}, "skills": {"type": "array", "items": {"type": "string"}},
        "salary": {"type": "string"}, "contact": {"type": "string"}, "apply": {"type": "string"},
        "work_mode": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]},
    },
    "required": ["is_job_post", "role", "company", "location", "summary", "experience", "min_years", "skills",
                 "salary", "contact", "apply", "work_mode"],
    "additionalProperties": False,
}

EMAIL = r"[\w.+-]+@[\w-]+\.[\w.]+"
PHONE = r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}\b"


def extract_with_rules(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """No-AI fallback: good enough to list and score a job."""
    text = f"{job.get('title', '')}\n{job.get('description', '')}"
    low = text.lower()
    years = re.search(r"(\d+)\s*\+?\s*(?:-|to)?\s*(\d+)?\s*\+?\s*(?:years|yrs)", low)
    contacts = list(dict.fromkeys(re.findall(EMAIL, text) + re.findall(PHONE, text)))
    mode = "remote" if "remote" in low else "hybrid" if "hybrid" in low else "unknown"
    opening = re.search(r"hiring|apply|send (your )?(cv|resume)|walk-?in|opening|vacanc|dm me|join our team", low)
    seeker = re.search(r"open to work|looking for (a |an )?(job|role|opportunit)|seeking (a |an )?(job|role)|my (cv|resume)",
                       low)
    return {
        "is_job_post": job["source"] != "linkedin_post" or bool(opening and not seeker), "role": "", "company": "",
        "extracted_by": "rules",
        "location": "", "summary": "", "experience": years.group(0) if years else "Not stated",
        "min_years": int(years.group(1)) if years else -1,
        "skills": [s for s in config["skills"] if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", low)][:12],
        "salary": job.get("salary", ""), "contact": ", ".join(contacts), "apply": "",
        "work_mode": job.get("work_mode") or mode,
    }


def extract(job: dict[str, Any], config: dict[str, Any], client: Any | None) -> dict[str, Any]:
    if client is None:
        return extract_with_rules(job, config)
    post = (f"Title: {job['title']}\nCompany: {job.get('company', '')}\nLocation: {job.get('location', '')}\n"
            f"{job.get('criteria', '')}\nSalary field: {job.get('salary', '')}\n\n{job.get('description', '')[:2500]}")
    try:
        from .llm import complete

        response = complete(client, 
            model=EXTRACT_MODEL, reasoning_effort="low",
            messages=[{"role": "system", "content": EXTRACT_PROMPT}, {"role": "user", "content": post}],
            response_format={"type": "json_schema", "json_schema": {"name": "job", "strict": True,
                                                                    "schema": EXTRACT_SCHEMA}},
        )
        data = json.loads(response.choices[0].message.content or "{}")
    except Exception:
        return extract_with_rules(job, config)
    data["skills"] = [s.strip().lower() for s in data.get("skills", []) if s.strip()][:12]
    if not data.get("salary"):
        data["salary"] = job.get("salary", "")
    if job.get("work_mode") == "remote":
        data["work_mode"] = "remote"
    return data


EMPTY = {"not stated", "not mentioned", "n/a", "na", "none", "null", "-", "not specified", "unknown"}


def tidy(value: str) -> str:
    """Model output cleanup: "Not stated" means empty; HTML entities decoded (some pages
    encode them twice); all-lowercase names get capitals ("kimberly-clark" -> "Kimberly-Clark")."""
    value = html.unescape(html.unescape(value or "")).strip()
    if value.lower().strip(" .") in EMPTY:
        return ""
    if value and value == value.lower() and re.search(r"[a-z]", value):
        value = re.sub(r"\b([a-z])", lambda m: m.group(1).upper(), value)
    return value


def apply_extraction(job: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    """Merge extracted facts into the job. For hiring posts, the post's role/company/
    location become the job's, and contact also says who posted it."""
    job = {**job, **{k: data[k] for k in ("summary", "experience", "min_years", "skills", "salary", "work_mode")
                     if k in data}}
    job["salary"] = html.unescape(job.get("salary") or "").strip()
    if job["salary"].lower().strip(" .") in EMPTY:
        job["salary"] = ""
    for key in ("role", "company", "location"):
        data[key] = tidy(data.get(key, ""))
    data["contact"] = "" if (data.get("contact") or "").lower().strip(" .") in EMPTY else data.get("contact", "")
    data["apply"] = "" if (data.get("apply") or "").lower().strip(" .") in EMPTY else data.get("apply", "")
    job["title"] = html.unescape(html.unescape(job["title"]))
    contact = [data.get("contact", "")]
    if data.get("apply") and data["apply"].strip() != data.get("contact", "").strip():
        contact.append(f"Apply: {data['apply']}")
    if job["source"] == "linkedin_post":
        job["title"] = data.get("role") or job["title"]
        job["company"] = data.get("company") or job.get("company", "")
        job["location"] = data.get("location") or job.get("location", "")
        if job.get("links") and not any(link in " ".join(contact) for link in job["links"]):
            contact.append("Links: " + " ".join(job["links"][:3]))
        if job.get("author"):
            contact.append(f"Posted by {job['author']}" + (f" ({job['author_url']})" if job.get("author_url") else ""))
    job["contact"] = "\n".join(c for c in contact if c)
    return job


def _skill_match(job_skill: str, mine: list[str]) -> bool:
    """Exact, or one contains the other as whole words ("react" ~ "react.js", not "go" ~ "mongodb")."""
    j = job_skill.lower().strip()
    words = lambda a, b: re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", b)  # noqa: E731
    return any(s == j or (len(s) > 2 and words(s, j)) or (len(j) > 2 and words(j, s)) for s in mine)


SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|architect|manager|head|director)\b", re.I)
OPEN_TO_INDIA = re.compile(r"india|worldwide|anywhere|global|apac|asia|remote$", re.I)


def open_to_india(job: dict[str, Any]) -> bool:
    """Remote-board jobs often hire only in some regions ("USA, Canada"). LinkedIn searches
    and hiring posts are already about India."""
    if job["source"] in {"linkedin", "linkedin_post", "freelancer", "truelancer", "pasted"}:
        return True
    return bool(OPEN_TO_INDIA.search((job.get("location") or "Remote").strip()))


def is_kind(job: dict[str, Any], kind: str, home: list[str] | tuple[str, ...] = ()) -> bool:
    text = f"{job.get('title', '')} {job.get('criteria', '')} {job.get('experience', '')}".lower()
    if kind == "remote":
        return job.get("work_mode") == "remote" or "remote" in f"{job.get('location', '')} {job['title']}".lower()
    if kind == "internship":
        return bool(job.get("internship")) or bool(re.search(r"\bintern(ship)?s?\b|\btrainee\b", text))
    if kind == "freelance":
        if job.get("freelance") or job.get("source") in {"freelancer", "truelancer"}:
            return True  # the flag isn't stored, so re-scoring a stored job relies on its source
        return bool(re.search(r"\bfreelanc|\bcontract(ual)?\b|\bper project\b|"
                                                             r"\bpart[- ]time\b", text))
    if kind == "home":
        return any(h in (job.get("location") or "").lower() for h in home)
    return False


USD_RATE = {"usd": 1.0, "$": 1.0, "eur": 1.08, "€": 1.08, "gbp": 1.27, "£": 1.27, "inr": 0.012, "₹": 0.012,
            "rs": 0.012, "aud": 0.66, "cad": 0.73, "sgd": 0.74, "aed": 0.27, "nzd": 0.6}


def pay_in_usd(salary: str) -> tuple[float, str] | None:
    """A gig's pay as (US dollars, "hour" or "fixed"), from "USD 15-25 /hour", "INR 600-1500 fixed",
    "EUR 20-50 /hour". The top of a range counts. None when it can't be read."""
    text = (salary or "").lower().replace(",", "")
    cur = next((c for c in USD_RATE if c in text), None)
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
    if not cur or not nums:
        return None
    return max(nums) * USD_RATE[cur], "hour" if re.search(r"/\s*h(ou)?r|hourly|per hour", text) else "fixed"


def fit(job: dict[str, Any], config: dict[str, Any]) -> tuple[int, list[str]]:
    """0-100 and the reasons. Skills 45, role 25, experience 20, location 10, plus the bonus
    for what you prefer (`prefer`: home, remote, freelance, internship); capped at 45 when
    it needs years more than you have."""
    reasons: list[str] = []
    mine = [s.lower() for s in config["skills"]]
    skills = job.get("skills") or []
    matched = [s for s in skills if _skill_match(s, mine)]
    if job.get("extracted_by") == "rules":  # only skills from my own list were looked for: can't see what's missing
        skill_pts = round(30 * min(1.0, len(matched) / 6))
    else:
        skill_pts = round(45 * min(1.0, len(matched) / max(3, len(skills)))) if skills else 15
    if skills:
        reasons.append(f"skills {len(matched)}/{len(skills)}" + (f": {', '.join(matched[:5])}" if matched else ""))
        missing = [s for s in skills if s not in matched][:4]
        if missing:
            reasons.append("missing: " + ", ".join(missing))
    title = job["title"].lower()
    role_hits = [w for w in config["role_words"] if re.search(rf"(?<![a-z]){re.escape(w)}", title)]
    strong = {"ai", "ml", "machine learning", "data scien", "data analy", "data engin", "agentic", "genai",
              "generative", "llm", "nlp", "analytics"}
    role_pts = 25 if strong & set(role_hits) else 14 if role_hits else 0
    reasons.append("target role" if role_pts == 25 else "related role" if role_pts else "off-target role")
    need = job.get("min_years", -1)
    if need is None or need < 0:
        stated = re.search(r"(\d+)\s*\+?\s*(?:-|–|to)?\s*\d*\s*\+?\s*(?:year|yr)", job.get("experience", "").lower())
        need = int(stated.group(1)) if stated else -1
    if SENIOR.search(job["title"]):
        need = max(need, 4)  # "Senior ..." means years of experience, whatever the post says
    have = config["experience_years"]
    if need is None or need < 0:
        exp_pts = 12
        reasons.append("experience not stated")
    elif need <= have:
        exp_pts = 20
        reasons.append(f"needs {need}+ yrs: fits")
    elif need <= have + 1:
        exp_pts = 12
        reasons.append(f"needs {need}+ yrs: a stretch")
    else:
        exp_pts = 0
        reasons.append(f"needs {need}+ yrs: too senior")
    place = f"{job.get('location', '')} {job.get('work_mode', '')}".lower()
    if job.get("work_mode") == "remote" or "remote" in place:
        loc_pts = 10
    elif any(h in place for h in config["home"]):
        loc_pts = 10
    elif is_kind(job, "freelance"):
        loc_pts = 8  # freelance work is mostly done from home
    elif "india" in place or any(c in place for c in ("bengaluru", "bangalore", "pune", "hyderabad", "mumbai",
                                                       "delhi", "gurugram", "noida", "chennai", "ahmedabad",
                                                       "mohali", "kolkata", "jaipur", "bhopal")):
        loc_pts = 3
        reasons.append("on-site away from home")
    else:
        loc_pts = 1 if place.strip() else 5
    total = skill_pts + role_pts + exp_pts + loc_pts
    for kind, bonus in (config.get("prefer") or {}).items():
        if bonus and is_kind(job, kind, config.get("home") or ()):
            total += int(bonus)
            reasons.append(f"{kind} +{bonus}")
    total = min(100, total)
    if job.get("source") in {"freelancer", "truelancer"} and (pay := pay_in_usd(job.get("salary", ""))):
        usd, kind = pay
        floor = float(config.get("min_hourly_usd", 0) if kind == "hour" else config.get("min_fixed_usd", 0))
        if floor and usd < floor:
            total = min(total, 40)
            reasons.append(f"pay too low (~${usd:.0f}{'/hour' if kind == 'hour' else ' total'})")
    if exp_pts == 0:
        total = min(total, 45)  # a role needing years more than you have isn't a fit, however good the skills
    return total, reasons


# --- the run -------------------------------------------------------------------------------------


def too_old(job: dict[str, Any], days: int) -> bool:
    """Posted more than `days` ago (by its own date, or for LinkedIn posts the id's)."""
    when = post_time(job.get("url", "")) if job.get("source") == "linkedin_post" else None
    if when is None and job.get("posted"):
        try:
            when = datetime.fromisoformat(str(job["posted"]).replace("Z", "+00:00").split(".")[0].replace(" ", "T"))
        except ValueError:
            return False
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when < datetime.now(timezone.utc) - timedelta(days=days)


def _fresh(found: list[dict[str, Any]], hub: Hub, max_age_days: int = 7) -> list[dict[str, Any]]:
    new, seen = [], set()
    for job in found:
        if too_old(job, max_age_days):
            continue
        key = (job["source"], job["external_id"])
        if key in seen or hub.has_job(job["source"], job["external_id"], job["title"], job.get("company", "")):
            continue
        seen.add(key)
        if open_to_india(job):
            new.append(job)
    return new


def refresh(hub: Hub, config_path: Path, llm: Any | None = None, log=print,
            progress=lambda text: None) -> dict[str, Any]:
    """Fetch, enrich, score and store new jobs. Returns counts and the best new ones.
    `progress(text)` is told what it's doing ("Searching LinkedIn 3/32"), for the control center.
    Jobs are stored as they're done, so they show up while the run goes on."""
    config = load_config(config_path)
    hours = int(config["posted_within_hours"])
    found: list[dict[str, Any]] = []
    posts: list[dict[str, Any]] = []
    with httpx.Client(headers=UA, timeout=TIMEOUT, follow_redirects=True) as client:
        home = config.get("country") or "India"
        searches: list[tuple[str, str, dict[str, str]]] = [(role, loc, {}) for role in config["roles"]
                                                           for loc in config["locations"]]
        if config["search_remote"]:
            searches += [(role, home, {"f_WT": "2"}) for role in config["roles"]]
        if config["search_internships"]:
            searches += [(f"{role} intern", home, {"f_JT": "I"}) for role in config["roles"]]
        if config.get("search_freelance"):
            searches += [(role, home, {"f_JT": "C,T"}) for role in config["roles"]]  # contract, temporary
        searches += [(company, home, {}) for company in config["companies"]]
        for i, (keywords, location, extra) in enumerate(searches, 1):
            progress(f"Searching LinkedIn {i}/{len(searches)}")
            try:
                found += linkedin_search(keywords, location, hours, client, extra)
            except httpx.HTTPError as exc:
                log(f"jobs: linkedin search {keywords!r} failed: {exc.__class__.__name__}")
            time.sleep(PAUSE_S)
        if config["remote_boards"]:
            progress("Searching remote job boards")
            for term in config["remote_terms"]:
                for source in (remotive, jobicy):
                    try:
                        found += source(term if source is remotive else term.split()[-1], client)
                    except (httpx.HTTPError, ValueError) as exc:
                        log(f"jobs: {source.__name__} failed: {exc.__class__.__name__}")
        freelance: list[dict[str, Any]] = []
        sites = {"freelancer": freelancer_projects, "truelancer": truelancer}
        for site in config.get("freelance_sites", []):
            progress(f"Searching {site.capitalize()} for freelance projects")
            for query in config.get("freelance_queries", []):
                try:
                    freelance += sites[site](query, client)
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    log(f"jobs: {site} {query!r} failed: {exc.__class__.__name__}")
                time.sleep(0.5)
        for i, query in enumerate(config["hiring_post_queries"], 1):
            progress(f"Reading LinkedIn hiring posts {i}/{len(config['hiring_post_queries'])}")
            try:
                posts += hiring_posts(query, int(config["hiring_posts_per_query"]), client,
                                      int(config["max_post_age_days"]))
            except Exception as exc:  # the search engine rate limits now and then: skip the query
                log(f"jobs: hiring posts {query!r} failed: {exc.__class__.__name__}")

        age = int(config.get("max_age_days", 7))
        new = _fresh(found, hub, age)
        # Cheap pre-score on the title so the budget goes to the most promising posts.
        new.sort(key=lambda j: -fit({**j, "skills": [], "min_years": -1}, config)[0])
        gigs = _fresh(freelance, hub, age)
        gigs.sort(key=lambda j: -fit({**j, "skills": [], "min_years": -1}, config)[0])
        todo = (new[:int(config["max_new_per_run"])] + _fresh(posts, hub, age)[:int(config["max_new_posts_per_run"])]
                + gigs[:int(config.get("max_new_freelance_per_run", 25))])
        added, skipped = [], 0
        ai_budget = int(config.get("ai_extract_per_run", 30))
        # Hiring posts and the best-looking listings get the model; the rest keyword rules.
        todo.sort(key=lambda j: (j["source"] != "linkedin_post", -fit({**j, "skills": [], "min_years": -1}, config)[0]))
        for i, job in enumerate(todo, 1):
            progress(f"Reading job details {i}/{len(todo)} ({len(added)} new so far)")
            if job["source"] == "linkedin":
                try:
                    job = linkedin_details(job, client)
                except httpx.HTTPError:
                    pass
                time.sleep(PAUSE_S)
            data = extract(job, config, llm if i <= ai_budget else None)
            if job["source"] == "linkedin_post" and not data.get("is_job_post", True):
                skipped += 1
                continue
            job = apply_extraction(job, data)
            job["fit"], job["fit_reasons"] = fit(job, config)
            if hub.add_job(job):
                added.append(job)
    best = sorted(added, key=lambda j: -j["fit"])[:5]
    log(f"jobs: {len(found)} listings + {len(posts)} posts + {len(freelance)} freelance seen, {len(todo)} new, "
        f"{len(added)} stored, {skipped} non-job posts dropped")
    return {"seen": len(found) + len(posts) + len(freelance), "new": len(todo), "added": len(added),
            "best": [{"title": j["title"], "company": j.get("company", ""), "fit": j["fit"], "url": j["url"]}
                     for j in best]}


def rescore(hub: Hub, config_path: Path, llm: Any | None = None, log=print, ai_limit: int = 10) -> int:
    """Re-extract jobs stored by the keyword fallback (when the model was rate limited),
    then recompute every fit score with the current rules. Returns how many changed."""
    config = load_config(config_path)
    changed = 0
    for row in (hub.jobs(limit=100_000, include_description=True, max_age_days=None)
                + hub.jobs(status="hidden", limit=100_000, max_age_days=None)):
        job = dict(row)
        if not job["summary"] and llm is not None and ai_limit > 0:
            ai_limit -= 1
            data = extract(job, config, llm)
            if data.get("extracted_by") != "rules":
                if job["source"] == "linkedin_post" and not data.get("is_job_post", True):
                    hub.set_job_status(job["id"], "hidden")
                    changed += 1
                    continue
                job = apply_extraction({**job, "links": [], "author": ""}, data) | {"contact": job["contact"] or ""}
                if data.get("contact") and data["contact"] not in job["contact"]:
                    job["contact"] = "\n".join(c for c in (data["contact"], job["contact"]) if c)
        elif not job["summary"]:
            job["extracted_by"] = "rules"
        if job["source"] == "linkedin_post":
            job["title"] = job["title"] if not job["title"].lstrip().startswith("#") else \
                post_title("", job.get("description", ""))
            job["company"], job["location"] = tidy(job["company"]), tidy(job["location"])
        job["title"] = tidy(job["title"]) or job["title"]
        job["contact"] = "\n".join(line for line in (job.get("contact") or "").splitlines()
                                   if line.lower().strip(" .") not in EMPTY)
        job["salary"] = "" if (job.get("salary") or "").lower().strip(" .") in EMPTY else job.get("salary", "")
        fit_score, reasons = fit(job, config)
        hub.update_job(job["id"], {k: job[k] for k in ("title", "company", "location", "summary", "experience",
                                                       "skills", "salary", "contact", "work_mode") if k in job}
                       | {"fit": fit_score, "fit_reasons": reasons})
        changed += 1
    log(f"jobs: rescored {changed}")
    return changed


def from_paste(text: str, url: str, config: dict[str, Any], llm: Any | None) -> dict[str, Any]:
    """A job post you pasted, read like any other: role, company, contact... and scored.
    The same text pasted twice is the same job."""
    import hashlib

    text = text.strip()
    if len(text) < 40:
        raise ValueError("paste the whole post (it's too short to read)")
    url = url.strip()
    if url and not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    when = post_time(url) if url else None
    job = {"source": "pasted", "external_id": hashlib.sha1(text.encode("utf-8")).hexdigest()[:16],
           "title": post_title("", text), "company": "", "location": "", "url": url,
           "posted": (when or datetime.now(timezone.utc)).isoformat(timespec="seconds"), "description": text,
           "links": list(dict.fromkeys(re.findall(r"https?://\S+", text)))[:3]}
    data = extract(job, config, llm)
    job = apply_extraction({**job, "source": "linkedin_post"}, data) | {"source": "pasted"}
    job["fit"], job["fit_reasons"] = fit(job, config)
    return job


def groq_client() -> Any | None:
    try:
        from groq import Groq

        # Many retries: a job run makes dozens of calls and the free tier allows a few
        # thousand tokens a minute. The SDK waits as long as Groq says; slow is fine at night.
        return Groq(max_retries=8, timeout=60.0)
    except Exception:
        return None


def linkedin_search_url(keywords: str, location: str = "India") -> str:
    return f"https://www.linkedin.com/jobs/search/?keywords={quote(keywords)}&location={quote(location)}"
