"""Résumés for a job: score the ones you have, or write one tailored to it.

Scoring works like an applicant-tracking system: the job's keywords (the skills the
model pulled out of the post, plus known tech/analytics terms found in its text) are
looked for in each résumé's text. You get a match % per résumé and what's missing.

A tailored résumé is written from your own material only (your résumés, profile and
remembered facts): the model may choose, reorder, shorten and reword, never add. Then
it's checked: a skill or a number that doesn't appear in the source is removed. The
header (name, phone, email, links) is copied as it is from your master résumé. It's
printed to PDF with Chrome and scored like the others, so you can see if it's better.

Settings in data/outreach.json: "resume_files" (glob patterns of your résumé PDFs) and
"master_resume" (the most complete one: the source for tailoring).
"""

from __future__ import annotations

import glob
import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .browser import chrome_path
from .llm import complete

TAILOR_MODEL = "openai/gpt-oss-120b"
# Terms an ATS would look for, beyond the skills the model extracts from each post.
VOCAB = [
    "python", "sql", "r", "excel", "power bi", "tableau", "looker", "statistics", "a/b testing", "hypothesis testing",
    "etl", "elt", "airflow", "spark", "pyspark", "hadoop", "kafka", "dbt", "snowflake", "bigquery", "redshift",
    "data warehouse", "data modeling", "data pipeline", "data cleaning", "data visualization", "dashboards",
    "reporting", "kpi", "aws", "gcp", "azure", "docker", "kubernetes", "linux", "git", "ci/cd", "mlops", "mlflow",
    "machine learning", "deep learning", "nlp", "computer vision", "llm", "generative ai", "rag", "langchain",
    "langgraph", "agents", "prompt engineering", "fine-tuning", "transformers", "hugging face", "openai",
    "tensorflow", "pytorch", "keras", "scikit-learn", "xgboost", "lightgbm", "pandas", "numpy", "matplotlib",
    "regression", "classification", "clustering", "time series", "forecasting", "feature engineering",
    "reinforcement learning", "recommendation", "shap", "explainable ai", "fastapi", "flask", "django", "rest api",
    "react", "next.js", "node.js", "express", "typescript", "javascript", "java", "mongodb", "postgresql", "mysql",
    "redis", "vector database", "embeddings", "streamlit", "jupyter",
]
ALIASES = {"powerbi": "power bi", "sklearn": "scikit-learn", "scikit learn": "scikit-learn", "node": "node.js",
           "nodejs": "node.js", "nextjs": "next.js", "postgres": "postgresql", "js": "javascript", "ts": "typescript",
           "genai": "generative ai", "gen ai": "generative ai", "large language models": "llm", "llms": "llm",
           "ml": "machine learning", "dl": "deep learning", "ai agents": "agents", "agentic ai": "agents",
           "ms excel": "excel", "rest apis": "rest api", "apis": "rest api", "hf": "hugging face",
           "natural language processing": "nlp", "statistical analysis": "statistics", "data warehousing": "data warehouse",
           "etl pipelines": "etl", "data pipelines": "data pipeline", "data analysis": "data analytics",
           "dashboarding": "dashboards", "visualization": "data visualization", "a/b tests": "a/b testing",
           "eda": "exploratory data analysis", "exploratory analysis": "exploratory data analysis",
           "retrieval-augmented generation": "rag", "retrieval augmented generation": "rag",
           "preprocessing": "data preprocessing", "data pre-processing": "data preprocessing",
           "restful": "rest api", "restful apis": "rest api", "dockerized": "docker", "llm agents": "agents",
           "precision/recall": "precision recall f1", "fine tuning": "fine-tuning", "ml fundamentals": "machine learning"}


# A keyword is supported when the source names it, or names clear evidence of it: LLM agents
# built with LangGraph are generative AI even if those two words never appear. Kept narrow
# on purpose: a tool (TensorFlow, ChatGPT) needs its own name in the source.
EVIDENCE = {
    "generative ai": ["llm", "langgraph", "langchain", "rag", "gpt", "claude", "gemini", "groq", "agents"],
    "prompt engineering": ["prompt", "llm", "langgraph", "langchain"],
    "agents": ["langgraph", "agentic", "tool use", "tool-use", "mcp"],
    "llm": ["langgraph", "langchain", "gpt", "claude", "gemini", "groq"],
    "data cleaning": ["data preprocessing", "exploratory data analysis", "cleaned", "cleaning"],
    "data preprocessing": ["data cleaning", "feature engineering", "cleaned"],
    "data visualization": ["dashboards", "power bi", "matplotlib", "plotly", "streamlit"],
    "statistics": ["regression", "hypothesis testing", "statistical"],
    "rest api": ["fastapi", "express", "endpoints", "flask", "django"],
}


def supported(term: str, source_lower: str) -> bool:
    """The source shows this keyword, by name or by clear evidence (see EVIDENCE)."""
    term = _norm(term)
    return _has(term, source_lower) or any(_has(e, source_lower) for e in EVIDENCE.get(term, []))


def _norm(term: str) -> str:
    t = re.sub(r"\s+", " ", term.lower().strip())
    return ALIASES.get(t, t)


def _has(term: str, text: str) -> bool:
    """`term` in `text` as a whole word/phrase, alias-aware."""
    forms = {term} | {a for a, canon in ALIASES.items() if canon == term}
    return any(re.search(rf"(?<![a-z0-9]){re.escape(f)}(?![a-z0-9])", text) for f in forms)


# --- your résumés --------------------------------------------------------------------------


def resume_files(config: dict[str, Any]) -> list[Path]:
    """Every distinct résumé PDF (same content under two names counts once)."""
    patterns = list(config.get("resume_files") or [])
    patterns += [p for p in (config.get("resumes") or {}).values() if p]
    seen: set[str] = set()
    out = []
    for pattern in patterns:
        for name in sorted(glob.glob(os.path.expanduser(pattern))):
            path = Path(name)
            if path.suffix.lower() != ".pdf" or not path.is_file():
                continue
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            if digest not in seen:
                seen.add(digest)
                out.append(path)
    return out


def pdf_text(path: Path, cache_dir: Path | None = None) -> str:
    """The text of a PDF, cached by file and modification time."""
    key = hashlib.sha1(f"{path.resolve()}|{path.stat().st_mtime_ns}".encode()).hexdigest()
    cached = cache_dir / f"{key}.txt" if cache_dir else None
    if cached and cached.exists():
        return cached.read_text(encoding="utf-8")
    from pypdf import PdfReader

    text = "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(text, encoding="utf-8")
    return text


# --- scoring ---------------------------------------------------------------------------------


def job_keywords(job: dict[str, Any]) -> dict[str, int]:
    """{keyword: weight}: the post's own skills count double."""
    text = f"{job.get('title', '')}\n{job.get('description', '')}".lower()
    words = {_norm(s): 2 for s in job.get("skills") or [] if s.strip()}
    for term in VOCAB:
        if term not in words and _has(term, text):
            words[term] = 1
    return words


def score(job: dict[str, Any], resume_text: str) -> dict[str, Any]:
    keywords = job_keywords(job)
    text = resume_text.lower()
    matched = [k for k in keywords if _has(k, text)]
    missing = [k for k in keywords if k not in matched]
    total = sum(keywords.values()) or 1
    pct = round(100 * sum(keywords[k] for k in matched) / total)
    return {"score": pct, "matched": matched, "missing": sorted(missing, key=lambda k: -keywords[k])}


def rank(job: dict[str, Any], config: dict[str, Any], cache_dir: Path | None = None,
         extra: list[Path] | None = None) -> list[dict[str, Any]]:
    """Your résumés (and any tailored ones for this job) scored for the job, best first."""
    rows = []
    for path in [*resume_files(config), *(extra or [])]:
        try:
            result = score(job, pdf_text(path, cache_dir))
        except Exception as exc:  # an unreadable PDF shouldn't hide the others
            result = {"score": 0, "matched": [], "missing": [], "error": str(exc)}
        rows.append({"file": str(path), "name": path.name, "tailored": "tailored" in path.parts, **result})
    # Ties: the résumé named for this kind of role ("Data_Analyst" for a data analyst job), then the newest.
    title = set(re.findall(r"[a-z]+", job.get("title", "").lower())) - {"intern", "senior", "junior", "i", "ii"}
    named = lambda r: len(title & set(re.findall(r"[a-z]+", r["name"].lower().replace("_", " "))))  # noqa: E731
    newest = lambda r: Path(r["file"]).stat().st_mtime if Path(r["file"]).exists() else 0  # noqa: E731
    return sorted(rows, key=lambda r: (-r["score"], -named(r), -newest(r)))


# --- tailoring -------------------------------------------------------------------------------

TAILOR_PROMPT = """\
Write a one-page résumé for the job below, using ONLY the SOURCE material.

Rules:
- Every skill, tool, number, company, title, date and project must come from SOURCE. You \
may choose, reorder, shorten and reword; you must not add anything SOURCE doesn't say.
- Experience and project bullets: take SOURCE's own bullets nearly word for word \
(choose the most relevant, trim them); don't re-angle a role toward this job by giving \
it tools or methods SOURCE doesn't give it. Every job in SOURCE's experience that \
matters (especially the current one) keeps at least two of its real bullets.
- Lead with what this job asks for: put the most relevant experience bullets and \
projects first. Use every keyword in "Keywords to use", worded the job's way (SOURCE \
backs each one, by name or by clear evidence: LLM agents built with LangGraph are \
generative AI); never use a keyword that isn't on that list or in SOURCE.
- Summary: 2-3 lines aimed at this role. Experience: the most relevant roles, 2-4 bullets \
each. Projects: the 3-4 most relevant, 1-3 bullets each. Skills: grouped, most relevant \
first. Keep the whole thing to one page.
- Plain text in each field (no markdown). Keep each bullet under 30 words.

JOB:
{job}

SOURCE:
{source}"""

TAILOR_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "skills": {"type": "array", "items": {"type": "object", "properties": {
            "group": {"type": "string"}, "items": {"type": "array", "items": {"type": "string"}}},
            "required": ["group", "items"], "additionalProperties": False}},
        "experience": {"type": "array", "items": {"type": "object", "properties": {
            "role": {"type": "string"}, "org": {"type": "string"}, "dates": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}}},
            "required": ["role", "org", "dates", "bullets"], "additionalProperties": False}},
        "projects": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "stack": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}}},
            "required": ["name", "stack", "bullets"], "additionalProperties": False}},
        "education": {"type": "array", "items": {"type": "string"}},
        "certifications": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "summary", "skills", "experience", "projects", "education", "certifications"],
    "additionalProperties": False,
}


def master_path(config: dict[str, Any]) -> Path | None:
    if config.get("master_resume"):
        path = Path(os.path.expanduser(config["master_resume"]))
        if path.exists():
            return path
    files = resume_files(config)
    return max(files, key=lambda p: p.stat().st_size) if files else None


def header_lines(master_text: str) -> list[str]:
    """Name, contact and links: the master résumé's lines before its first section heading."""
    out = []
    for line in master_text.splitlines():
        if re.fullmatch(r"\s*(PROFESSIONAL )?SUMMARY|PROFILE|OBJECTIVE|SKILLS|EXPERIENCE\s*", line.strip(), re.I):
            break
        if line.strip():
            out.append(line.strip())
    return out[:4]


NUMBER = re.compile(r"(?<![\w.])\d[\d,.]*(?:\s?%|\s?(?:k|lakh|lpa|x|\+))?", re.I)


def _number_forms(token: str) -> set[str]:
    """'136k' -> {'136k', '136000'}; '1,36,000' -> {'136000'}: how a number may be written in the source."""
    t = token.strip().rstrip(".,").lower().replace(" ", "")
    core = re.sub(r"[%+x]|lakh|lpa", "", t).replace(",", "")
    forms = {t, core}
    if core.endswith("k"):
        try:
            forms.add(str(int(float(core[:-1]) * 1000)))
        except ValueError:
            pass
    return {f for f in forms if f}


def _supported_numbers(text: str, source: str) -> bool:
    """Every number in `text` appears in the source as a number of its own ("2" doesn't count
    as found because of "R2"; "136k" counts for "136,000")."""
    flat = source.replace(",", "")

    def found(form: str) -> bool:
        pattern = rf"(?<![\w.]){re.escape(form)}(?!\d)"
        return bool(re.search(pattern, source) or re.search(pattern, flat))

    return all(any(found(f) for f in _number_forms(n)) for n in NUMBER.findall(text))


def verify(data: dict[str, Any], source: str) -> tuple[dict[str, Any], list[str]]:
    """Removes what the source doesn't support. Returns the cleaned résumé and what was removed."""
    src = source.lower()
    removed: list[str] = []
    for group in data["skills"]:
        keep = [s for s in group["items"] if supported(s, src) or s.lower() in src]
        removed += [f"skill: {s}" for s in group["items"] if s not in keep]
        group["items"] = keep
    data["skills"] = [g for g in data["skills"] if g["items"]]
    for section in ("experience", "projects"):
        for entry in data[section]:
            keep = [b for b in entry["bullets"] if _supported_numbers(b, src)]
            removed += [f"bullet with an unsourced number: {b}" for b in entry["bullets"] if b not in keep]
            entry["bullets"] = keep
    if not _supported_numbers(data["summary"], src):
        removed.append(f"summary had an unsourced number: {data['summary']}")
        data["summary"] = NUMBER.sub("", data["summary"]).replace("  ", " ")
    return data, removed


CHECK_MODEL = "openai/gpt-oss-120b"
CHECK_PROMPT = """\
You check a résumé against the facts it was written from. For each numbered claim, \
decide whether SOURCE directly supports it: the same work, at the same place, with the \
same tools, methods and results. A claim is UNSUPPORTED if it adds a tool, method, \
technology, result or scope SOURCE doesn't state for that work (for example calling a \
manually-set pricing system "machine-learning-based", or naming a library SOURCE never \
connects to that job), even if each word appears somewhere in SOURCE.

Return the numbers of the unsupported claims.

CLAIMS:
{claims}

SOURCE:
{source}"""
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"unsupported": {"type": "array", "items": {"type": "integer"}}},
    "required": ["unsupported"],
    "additionalProperties": False,
}


def fact_check(data: dict[str, Any], source: str, llm: Any) -> list[str]:
    """Second pass: a model reads every bullet (and the summary) against the source and the
    ones it can't support are removed. Catches true facts combined into false claims, which
    the word/number check can't see."""
    claims: list[tuple[str, int, int]] = [("summary", 0, 0)]
    texts = [data["summary"]]
    for section in ("experience", "projects"):
        for i, entry in enumerate(data[section]):
            for j, bullet in enumerate(entry["bullets"]):
                claims.append((section, i, j))
                where = entry.get("org") or entry.get("name") or ""
                texts.append(f"[{where}] {bullet}")
    numbered = "\n".join(f"{n}. {t}" for n, t in enumerate(texts))
    response = complete(llm, 
        model=CHECK_MODEL, reasoning_effort="medium",
        messages=[{"role": "user", "content": CHECK_PROMPT.format(claims=numbered, source=source[:12000])}],
        response_format={"type": "json_schema", "json_schema": {"name": "check", "strict": True,
                                                                "schema": CHECK_SCHEMA}},
    )
    bad = set(json.loads(response.choices[0].message.content or "{}").get("unsupported", []))
    removed = [f"claim not in your material: {texts[n]}" for n in sorted(bad) if 0 <= n < len(texts)]
    drop: dict[tuple[str, int], set[int]] = {}
    for n in bad:
        if 0 < n < len(claims):
            section, i, j = claims[n]
            drop.setdefault((section, i), set()).add(j)
    for (section, i), js in drop.items():
        entry = data[section][i]
        entry["bullets"] = [b for j, b in enumerate(entry["bullets"]) if j not in js]
    for section in ("experience", "projects"):
        data[section] = [e for e in data[section] if e["bullets"]]
    if 0 in bad:
        data["summary"] = ""
    return removed


CSS = """
@page { size: A4; margin: 12mm 13mm; }
body { font: 10pt/1.35 "Segoe UI", Calibri, Arial, sans-serif; color: #1a1d29; margin: 0; }
h1 { font-size: 19pt; margin: 0; letter-spacing: .3px; }
.head { color: #3f4660; font-size: 9pt; margin: 2px 0 6px; }
.headline { font-weight: 600; color: #4b32c3; margin: 2px 0 0; }
h2 { font-size: 10pt; text-transform: uppercase; letter-spacing: 1.2px; color: #4b32c3; border-bottom: 1px solid #d9dcf0;
     padding-bottom: 2px; margin: 10px 0 5px; }
p { margin: 0 0 4px; } ul { margin: 2px 0 6px 16px; padding: 0; } li { margin: 1px 0; }
.row { display: flex; justify-content: space-between; font-weight: 600; } .row span { font-weight: 400; color: #525a75; }
.skills div { margin: 1px 0; } .skills b { color: #2b2f45; }
"""


def render_html(data: dict[str, Any], header: list[str], tight: int = 0) -> str:
    """`tight` 1, 2...: smaller type and spacing, to get it onto one page."""
    e = html.escape
    name, rest = (header[0].split("|")[0].strip(), " | ".join(header)[len(header[0].split("|")[0]):].strip(" |")) \
        if header else ("", "")
    parts = [f"<h1>{e(name)}</h1>", f'<div class="head">{e(rest)}</div>' if rest else "",
             f'<div class="headline">{e(data["headline"])}</div>' if data["headline"] else "",
             f"<h2>Summary</h2><p>{e(data['summary'])}</p>" if data["summary"].strip() else ""]
    if data["skills"]:
        parts.append('<h2>Skills</h2><div class="skills">' + "".join(
            f"<div><b>{e(g['group'])}:</b> {e(', '.join(g['items']))}</div>" for g in data["skills"]) + "</div>")
    if data["experience"]:
        parts.append("<h2>Experience</h2>")
        for x in data["experience"]:
            parts.append(f'<div class="row">{e(x["role"])}, {e(x["org"])}<span>{e(x["dates"])}</span></div>'
                         "<ul>" + "".join(f"<li>{e(b)}</li>" for b in x["bullets"]) + "</ul>")
    if data["projects"]:
        parts.append("<h2>Projects</h2>")
        for x in data["projects"]:
            parts.append(f'<div class="row">{e(x["name"])}<span>{e(x["stack"])}</span></div>'
                         "<ul>" + "".join(f"<li>{e(b)}</li>" for b in x["bullets"]) + "</ul>")
    if data["education"]:
        parts.append("<h2>Education</h2>" + "".join(f"<p>{e(x)}</p>" for x in data["education"]))
    if data["certifications"]:
        parts.append("<h2>Certifications</h2><p>" + e(" · ".join(data["certifications"])) + "</p>")
    squeeze = (f"body{{font-size:{10 - 0.6 * tight:.1f}pt;line-height:{1.35 - 0.05 * tight:.2f}}}"
               f"h2{{margin:{10 - 2 * tight}px 0 4px}}ul{{margin-bottom:{6 - 2 * tight}px}}"
               f"@page{{margin:{12 - 2 * tight}mm {13 - 2 * tight}mm}}") if tight else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}{squeeze}</style></head>'
            f'<body>{"".join(parts)}</body></html>')


def print_pdf(page: str, out: Path) -> Path:
    """HTML to PDF with headless Chrome (the same engine that shows it on screen)."""
    exe = chrome_path()
    if exe is None:
        raise RuntimeError("Chrome is needed to make the PDF")
    out = out.resolve()  # Chrome resolves a relative path from its own folder
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "resume.html"
        src.write_text(page, encoding="utf-8")
        run = subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                              f"--user-data-dir={Path(tmp) / 'profile'}", f"--print-to-pdf={out}", src.as_uri()],
                             capture_output=True, text=True, timeout=90,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not out.exists():
        raise RuntimeError(f"Chrome didn't produce the PDF: {(run.stderr or run.stdout).strip()[-300:]}")
    return out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "job"


def tailor(job: dict[str, Any], config: dict[str, Any], llm: Any, about: str, out_dir: Path,
           cache_dir: Path | None = None) -> dict[str, Any]:
    """Writes, checks and prints a résumé for this job. Returns the file, its score and what the
    check removed."""
    master = master_path(config)
    if master is None:
        raise RuntimeError("no résumé found to work from; set resume_files in data/outreach.json")
    master_text = pdf_text(master, cache_dir)
    # Source: the master plus the two résumés that match this job best, plus remembered facts.
    others = [r for r in rank(job, config, cache_dir) if Path(r["file"]).resolve() != master.resolve()
              and not r.get("tailored")][:2]
    extra = "\n\n".join(pdf_text(Path(r["file"]), cache_dir) for r in others)
    source = f"{master_text}\n\n{extra}\n\nMore facts:\n{about}"
    # The job's keywords your own material backs up (by name or clear evidence): use each of them.
    backed = [k for k in job_keywords(job) if supported(k, source.lower())]
    posting = (f"{job['title']} at {job.get('company') or 'a company'} ({job.get('location', '')})\n"
               f"Skills asked: {', '.join(job.get('skills') or [])}\n"
               f"Keywords to use (SOURCE supports each one, so include every one where it fits): "
               f"{', '.join(backed) or 'none'}\n\n{(job.get('description') or '')[:2500]}")
    content = ""
    for attempt, effort in enumerate(("low", "low")):  # a long source can run out of room once: try again
        try:
            response = complete(llm, 
                model=TAILOR_MODEL, reasoning_effort=effort, max_completion_tokens=8000,
                messages=[{"role": "user", "content": TAILOR_PROMPT.format(job=posting, source=source[:16000])}],
                response_format={"type": "json_schema", "json_schema": {"name": "resume", "strict": True,
                                                                        "schema": TAILOR_SCHEMA}},
            )
            content = response.choices[0].message.content or ""
            break
        except Exception:
            if attempt == 1:
                raise
    data, removed = verify(json.loads(content or "{}"), source)
    removed += fact_check(data, source, llm)
    from .outreach import fix_links

    header = [fix_links(line, config) for line in header_lines(master_text)]
    out = out_dir / f"{slug(job.get('company') or 'company')}-{slug(job['title'])}-{job.get('id', 0)}.pdf"
    from pypdf import PdfReader

    for tight in range(3):  # one page: re-print a little tighter while it spills over
        print_pdf(render_html(data, header, tight), out)
        if len(PdfReader(str(out)).pages) == 1:
            break
    return {"file": str(out), "name": out.name, **score(job, pdf_text(out, cache_dir)), "removed": removed}
