"""Find who to write to at a company: published emails and HR / recruiter profiles.

Only addresses the company (or someone) has published are collected: its careers,
contact and about pages, and web search results that show an address at its domain.
Personal addresses are never guessed ("firstname@company.com"): guesses bounce, and
bounces get your Gmail flagged as a spammer. People (HR, recruiters, talent
acquisition) come from public LinkedIn profiles found by web search, to connect with.

Results are cached per company for 30 days.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .hub import Hub
from .jobs import UA, web_search

NOT_COMPANY_SITES = ("linkedin.", "glassdoor.", "naukri.", "indeed.", "ambitionbox.", "wikipedia.", "facebook.",
                     "instagram.", "twitter.", "x.com", "crunchbase.", "zaubacorp.", "justdial.", "youtube.",
                     "tofler.", "internshala.", "foundit.", "monster", "shine.com", "apna.co", "cutshort.",
                     "wellfound.", "instahyre.", "quora.", "reddit.", "medium.", "github.", "google.", "bing.",
                     "zoominfo.", "rocketreach.", "apollo.io", "signalhire.", "lusha.", "dnb.com", "owler.")
PAGES = ["/careers", "/contact", "", "/contact-us", "/jobs", "/about"]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp)$|^(example|test|user|name|your|email)@|sentry|wixpress|"
                  r"@example\.|@domain\.|@email\.", re.I)
ROLE_RANK = [("hr", 0), ("career", 0), ("jobs", 0), ("recruit", 0), ("talent", 0), ("hiring", 0), ("people", 1),
             ("hello", 2), ("contact", 2), ("info", 2), ("team", 2), ("support", 4), ("sales", 5), ("billing", 6)]
CACHE_DAYS = 30


def rank(email: str) -> int:
    """Lower is better for job outreach: hr@ / careers@ first, sales@ / support@ last."""
    local = email.split("@")[0].lower()
    for word, score in ROLE_RANK:
        if word in local:
            return score
    return 3  # a person's address published by the company


def clean_emails(text: str, domain: str = "") -> list[str]:
    found = []
    text = re.sub(r"\\?u00(3[ce]|22|27)", " ", text)  # JSON-escaped <, >, quotes glued to addresses
    for e in EMAIL.findall(text):
        e = e.strip(".").lower()
        if JUNK.search(e):
            continue
        if domain and not (e.endswith("@" + domain) or e.endswith("." + domain)):
            continue
        found.append(e)
    return list(dict.fromkeys(found))


GENERIC = {"pvt", "ltd", "private", "limited", "inc", "llp", "the", "india", "in", "solutions", "systems",
           "technologies", "technology", "tech", "software", "services", "group", "labs", "it", "co", "company",
           "global", "consulting", "by", "and", "of"}


def domain_score(host: str, company: str) -> int:
    """How well a website's name matches the company: 3 = the domain is the name ("tarento" ->
    tarento.com), 2 = it starts with it, 1 = it contains a distinctive word, 0 = no match.
    Generic words ("solutions", "systems") don't count."""
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in GENERIC and len(w) > 2]
    if not words:
        return 0
    label = host.split(".")[0].replace("-", "")
    name = "".join(words)
    if label in (name, words[0]) or label == "".join(w[0] for w in words) and len(words) > 2:
        return 3
    if label.startswith(name) or label.startswith(words[0]) and len(label) - len(words[0]) <= 6:
        return 2
    return 1 if all(w in label for w in words) else 0


def company_domain(company: str) -> str:
    """The company's own website domain, from a web search; "" if it can't be told apart.
    The best-matching name wins, not the first result (Rapido is rapido.bike, not
    rapidotrains.com); a domain that only shares a page title is never taken."""
    try:
        results = web_search(f"{company} official website", 8)
    except Exception:
        return ""
    best, best_score = "", 0
    for rank_, r in enumerate(results):
        host = urlparse(r.get("href", "")).netloc.lower().removeprefix("www.")
        if not host or any(bad in host for bad in NOT_COMPANY_SITES):
            continue
        score = domain_score(host, company)
        if score > best_score or (score == best_score and score and not best):
            best, best_score = host, score
    return best if best_score >= 2 else ""


def scrape_site(domain: str, client: httpx.Client, about: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """(email, page) pairs published on the company's own pages. Fills `about["text"]`
    with the site's own one-line description when it has one."""
    out = []
    for path in PAGES:
        url = f"https://{domain}{path}"
        try:
            r = client.get(url)
        except httpx.HTTPError:
            continue
        if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
            continue
        text = r.text.replace("&#64;", "@").replace("[at]", "@").replace("(at)", "@")
        if about is not None and not about.get("text"):
            meta = re.search(r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']{20,400})',
                             text, re.I)
            if meta:
                import html as _html

                about["text"] = _html.unescape(meta.group(1)).strip()
        out += [(e, url) for e in clean_emails(text, domain)]
        if sum(rank(e) <= 1 for e, _ in out) >= 2:
            break  # enough hr@/careers@-type addresses: skip the remaining pages
        time.sleep(0.3)
    return out


def searched_emails(company: str, domain: str) -> list[tuple[str, str]]:
    if not domain:
        return []
    try:
        results = web_search(f'"@{domain}" {company} hr OR careers OR hiring OR recruitment', 10)
    except Exception:
        return []
    return [(e, r.get("href", "")) for r in results for e in clean_emails(f"{r.get('title', '')} {r.get('body', '')}",
                                                                          domain)]


def hr_people(company: str, limit: int = 6) -> list[dict[str, str]]:
    """Public LinkedIn profiles of HR / recruiters / talent acquisition at the company."""
    try:
        results = web_search(f'site:linkedin.com/in "{company}" (recruiter OR "talent acquisition" OR HR OR '
                             f'"human resources" OR "hiring")', 10)
    except Exception:
        return []
    people = []
    for r in results:
        url = r.get("href", "").split("?")[0]
        if "linkedin.com/in/" not in url:
            continue
        parts = [p.strip() for p in r.get("title", "").replace(" | LinkedIn", "").split(" - ")]
        name, headline = parts[0], " - ".join(parts[1:])
        text = f"{headline} {r.get('body', '')}".lower()
        if company.lower().split()[0] not in text:
            continue  # someone at another company with the same words
        if not re.search(r"recruit|talent|\bhr\b|human resource|people (ops|partner)|hiring manager", headline.lower()):
            continue  # the headline has to say it: a body mentioning "hiring" isn't enough
        people.append({"name": name, "title": headline[:120], "url": url})
        if len(people) >= limit:
            break
    return people


def find(company: str, client: httpx.Client | None = None, domain: str = "") -> dict[str, Any]:
    own = client is None
    client = client or httpx.Client(headers=UA, timeout=8.0, follow_redirects=True)
    try:
        domain = domain or company_domain(company)
        about: dict[str, str] = {}
        pairs = (scrape_site(domain, client, about) if domain else []) + searched_emails(company, domain)
    finally:
        if own:
            client.close()
    emails: dict[str, str] = {}
    for email, source in pairs:
        emails.setdefault(email, source)
    ranked = sorted(emails.items(), key=lambda kv: (rank(kv[0]), kv[0]))
    return {"company": company, "domain": domain, "about": about.get("text", ""),
            "emails": [{"email": e, "source": src, "kind": "person" if rank(e) == 3 else "role"} for e, src in ranked],
            "people": hr_people(company)}


def set_domain(hub: Hub, company: str, domain: str) -> dict[str, Any]:
    """You know the website better: use it, and look the company up again on that site."""
    domain = re.sub(r"^https?://|^www\.|/.*$", "", domain.strip().lower())
    domains = json.loads(hub.get("cold:domains", "{}"))
    domains[company.strip().lower()] = domain
    hub.put("cold:domains", json.dumps(domains))
    return lookup(hub, company, refresh=True)


def lookup(hub: Hub, company: str, refresh: bool = False) -> dict[str, Any]:
    """Cached find(): the same company isn't searched again for 30 days."""
    company = company.strip()
    if not company:
        return {"company": "", "domain": "", "emails": [], "people": []}
    key = "contacts:" + company.lower()
    cached = hub.get(key)
    if cached and not refresh:
        data = json.loads(cached)
        if datetime.fromisoformat(data["checked"]) > datetime.now() - timedelta(days=CACHE_DAYS):
            return data
    fixed = json.loads(hub.get("cold:domains", "{}")).get(company.lower(), "")
    data = find(company, domain=fixed) | {"checked": datetime.now().isoformat(timespec="seconds")}
    hub.put(key, json.dumps(data))
    return data


def cached(hub: Hub, company: str) -> dict[str, Any] | None:
    raw = hub.get("contacts:" + company.strip().lower())
    return json.loads(raw) if raw else None


def companies(hub: Hub) -> list[dict[str, Any]]:
    """Every company from your jobs plus the ones you added for cold emailing, with what's
    known about reaching them."""
    by_name: dict[str, dict[str, Any]] = {}
    for j in hub.jobs(limit=100_000, include_description=False):
        name = (j["company"] or "").strip()
        if not name or "client" in name.lower():  # freelance marketplace clients have no company
            continue
        c = by_name.setdefault(name.lower(), {"company": name, "jobs": [], "best_fit": 0, "added": False})
        c["jobs"].append({"id": j["id"], "title": j["title"], "fit": j["fit"], "url": j["url"]})
        c["best_fit"] = max(c["best_fit"], j["fit"])
    for name in json.loads(hub.get("cold:companies", "[]")):
        by_name.setdefault(name.lower(), {"company": name, "jobs": [], "best_fit": 0})["added"] = True
    out = []
    for c in by_name.values():
        info = cached(hub, c["company"]) or {}
        out.append(c | {"domain": info.get("domain", ""), "about": info.get("about", ""),
                        "emails": info.get("emails", []), "people": info.get("people", []),
                        "checked": info.get("checked", "")})
    return sorted(out, key=lambda c: (-c["added"], -c["best_fit"], c["company"].lower()))


def add_company(hub: Hub, name: str) -> list[str]:
    names = json.loads(hub.get("cold:companies", "[]"))
    if name.strip() and name.strip().lower() not in {n.lower() for n in names}:
        names.append(name.strip())
        hub.put("cold:companies", json.dumps(names))
    return names


DISCOVER_PROMPT = """\
List {n} real companies that hire freshers (0-6 months experience) for data analysis, data \
science, ML/AI or Python roles, as interns, part-timers, freelancers or entry-level staff. \
Mix: Indian AI/data/SaaS startups, IT and analytics service companies, \
remote-first startups worldwide that hire from India, and Indian product companies. Only \
companies you are sure exist, by their usual name. Not these: {known}."""
DISCOVER_SCHEMA = {"type": "object", "properties": {"companies": {"type": "array", "items": {"type": "string"}}},
                   "required": ["companies"], "additionalProperties": False}


def discover(hub: Hub, llm: Any, n: int = 40) -> list[str]:
    """New companies for the cold-email list. Only names come from the model: each one is
    then looked up on its own website (lookup), and only addresses published there or in
    search results are ever used, so a name the model got wrong just finds nothing."""
    from .llm import complete

    known = {c["company"].lower() for c in companies(hub)}
    response = complete(llm, model="openai/gpt-oss-120b", reasoning_effort="low", messages=[{"role": "user",
        "content": DISCOVER_PROMPT.format(n=n, known=", ".join(sorted(known))[:3000])}],
        response_format={"type": "json_schema", "json_schema": {"name": "companies", "strict": True,
                                                                "schema": DISCOVER_SCHEMA}})
    names = json.loads(response.choices[0].message.content or "{}").get("companies", [])
    added = list(dict.fromkeys(x.strip() for x in names if x.strip() and x.strip().lower() not in known))[:n]
    for name in added:
        add_company(hub, name)
    return added


def csv_export(hub: Hub) -> str:
    """Cold-email data as CSV: one row per company."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "website", "about", "best_email", "all_emails", "hr_people", "hr_profiles", "open_roles",
                "best_fit"])
    for c in companies(hub):
        emails = [e["email"] for e in c["emails"]]
        w.writerow([c["company"], c["domain"], c["about"], emails[0] if emails else "", "; ".join(emails),
                    "; ".join(f"{p['name']} ({p['title']})" for p in c["people"]),
                    "; ".join(p["url"] for p in c["people"]), "; ".join(j["title"] for j in c["jobs"][:5]),
                    c["best_fit"]])
    return buf.getvalue()


def lookup_batch(hub: Hub, limit: int = 15, min_fit: int = 55, log=print) -> int:
    """Looks up the best-matching companies not checked yet (your list first). Run after each
    night's job search, so the cold-email list fills itself."""
    todo = [c["company"] for c in companies(hub) if not c["checked"] and (c.get("added") or c["best_fit"] >= min_fit)]
    done = 0
    for name in todo[:limit]:
        try:
            lookup(hub, name)
            done += 1
        except Exception as exc:
            log(f"contacts for {name}: {exc!r}")
    log(f"contacts: looked up {done} companies")
    return done


def xlsx_export(hub: Hub, path: Path) -> Path:
    """The cold-email list as an Excel file: one row per company, emails and links clickable."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Companies"
    head = ["Company", "Website", "About", "Best email", "All emails", "HR / recruiters (LinkedIn)", "Open roles",
            "Best fit", "Looked up"]
    ws.append(head)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="5B3DF5")
    for c in companies(hub):
        emails = [e["email"] for e in c["emails"]]
        ws.append([c["company"], c["domain"], c["about"], emails[0] if emails else "", ", ".join(emails),
                   "\n".join(f"{p['name']} ({p['title']}) {p['url']}" for p in c["people"]),
                   "\n".join(j["title"] for j in c["jobs"][:5]), c["best_fit"] or "", c["checked"][:10]])
        row = ws.max_row
        if c["domain"]:
            ws.cell(row, 2).hyperlink = f"https://{c['domain']}"
            ws.cell(row, 2).font = Font(color="2848B8", underline="single")
        if emails:
            ws.cell(row, 4).hyperlink = f"mailto:{emails[0]}"
            ws.cell(row, 4).font = Font(color="2848B8", underline="single")
    for col, width in zip("ABCDEFGHI", (26, 26, 50, 30, 40, 60, 40, 9, 12)):
        ws.column_dimensions[col].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def best_email(job: dict[str, Any], found: dict[str, Any] | None = None) -> str:
    """Where to send an application: an address written in the post first, then the
    company's published hr@/careers@ (not sales@ or support@)."""
    in_post = clean_emails(job.get("contact", "") + " " + job.get("description", ""))
    if in_post:
        return sorted(in_post, key=rank)[0]
    for e in (found or {}).get("emails", []):
        if rank(e["email"]) <= 2:
            return e["email"]
    return ""
