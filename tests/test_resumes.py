"""Résumé scoring, tailoring safeguards and replies."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from naman import resumes
from naman.hub import Hub
from naman.outreach import Outreach
from naman.store import Store

JOB = {"id": 7, "title": "Data Analyst Intern", "company": "Acme", "source": "linkedin_post",
       "skills": ["sql", "power bi", "python"], "description": "We use Excel, SQL and Tableau dashboards.",
       "contact": "hr@acme.io", "location": "Remote", "work_mode": "remote", "experience": "Freshers"}


def test_score_counts_post_skills_double_and_lists_whats_missing():
    result = resumes.score(JOB, "Skills: SQL, PowerBI, Python, Excel. Built dashboards.")
    assert result["missing"] == ["tableau"]  # powerbi = power bi; dashboards found too
    assert result["score"] == round(100 * 8 / 9)  # sql, power bi, python (x2) + excel, dashboards (x1); tableau missing


def test_numbers_must_come_from_the_source_in_any_form():
    src = "sole engineer ~136,000 loc and 372 api endpoints, r2 0.84, +21% accuracy"
    assert resumes._supported_numbers("~136k LOC, 372 endpoints", src)
    assert resumes._supported_numbers("0.84 R2, 21% better", src)
    assert not resumes._supported_numbers("served 500 users", src)


def test_verify_drops_unsourced_skills_and_numbers():
    data = {"headline": "", "summary": "Cut costs by 40%.", "education": [], "certifications": [],
            "skills": [{"group": "Data", "items": ["SQL", "Snowflake"]}],
            "experience": [{"role": "Intern", "org": "Cognifyz", "dates": "2024",
                            "bullets": ["Regression with R2 0.84", "Served 2 million users"]}], "projects": []}
    cleaned, removed = resumes.verify(data, "sql, regression, r2 0.84 at cognifyz")
    assert cleaned["skills"][0]["items"] == ["SQL"]
    assert cleaned["experience"][0]["bullets"] == ["Regression with R2 0.84"]
    assert "40" not in cleaned["summary"] and len(removed) == 3


def test_fact_check_removes_claims_the_model_flags():
    data = {"summary": "ML engineer.", "experience": [{"role": "Engineer", "org": "Acme", "dates": "",
            "bullets": ["Built the dispatch engine", "Built an ML surge-pricing model"]}],
            "projects": [{"name": "CricXAI", "stack": "", "bullets": ["Made dashboards"]}]}
    flagged = json.dumps({"unsupported": [2, 3]})  # 0 = summary, 1-2 = Acme bullets, 3 = CricXAI
    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=flagged))]))))
    removed = resumes.fact_check(data, "source", llm)
    assert data["experience"][0]["bullets"] == ["Built the dispatch engine"]
    assert data["projects"] == []  # no bullets left: the project goes
    assert len(removed) == 2 and "ML surge-pricing" in removed[0]


def test_header_is_taken_from_the_master_resume():
    text = "JANE DOE | Engineer\n+91 90000 00000 | jane@x.io\nSUMMARY\nBuilds things."
    assert resumes.header_lines(text) == ["JANE DOE | Engineer", "+91 90000 00000 | jane@x.io"]
    page = resumes.render_html({"headline": "", "summary": "", "skills": [], "experience": [], "projects": [],
                                "education": [], "certifications": []}, resumes.header_lines(text), tight=2)
    assert "<h1>JANE DOE</h1>" in page and "Summary" not in page and "font-size:8.8pt" in page


def test_only_your_resumes_may_be_copied_or_opened(tmp_path):
    folder = tmp_path / "cv"
    folder.mkdir()
    mine = folder / "Jane_Data_Analyst.pdf"
    mine.write_bytes(b"%PDF-1.4 a")
    (tmp_path / "outreach.json").write_text(json.dumps({"resume_files": [str(folder / "*.pdf")]}))
    out = Outreach(Hub(Store(tmp_path / "n.db")), tmp_path, log=lambda s: None)
    assert out.known_resume(str(mine))
    assert not out.known_resume(str(tmp_path / "n.db"))
    tailored = out.tailored_dir / "acme-x-1.pdf"
    tailored.parent.mkdir(parents=True)
    tailored.write_bytes(b"%PDF")
    assert out.known_resume(str(tailored))


def test_job_sub_routes_are_not_swallowed_by_the_job_route(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from naman.agent import Naman
    from naman.control import ControlCenter
    from test_naman import FakeChat

    store = Store(tmp_path / "n.db")
    naman = Naman(store, FakeChat(), InMemorySaver())
    job_id = naman.hub.add_job({**JOB, "external_id": "1", "fit": 80, "url": "https://x"})
    monkeypatch.setattr(naman.services.outreach, "resume_scores", lambda jid: [{"name": "cv.pdf", "score": 90}])
    center = ControlCenter(naman, log=lambda s: None)
    assert center.api("GET", f"/api/jobs/{job_id}/resumes", {}, {}) == (200, [{"name": "cv.pdf", "score": 90}])
    assert center.api("GET", f"/api/jobs/{job_id}", {}, {})[1]["title"] == "Data Analyst Intern"
    center.server.server_close()


def test_low_scores_get_a_tailored_resume_but_only_if_it_scores_higher(tmp_path, monkeypatch):
    (tmp_path / "outreach.json").write_text(json.dumps({"resume_files": ["x"], "tailor_below": 70}))
    out = Outreach(Hub(Store(tmp_path / "n.db")), tmp_path, llm=lambda: object(), log=lambda s: None)
    made = []
    monkeypatch.setattr(out, "tailor", lambda jid: made.append(jid) or {"file": "t.pdf", "name": "t.pdf", "score": 82})
    monkeypatch.setattr(out, "resume_scores", lambda jid: [{"file": "mine.pdf", "name": "mine.pdf", "score": 60}])
    assert out.best_resume(1)["name"] == "t.pdf" and made == [1]  # 60% < 70%: tailored, and better
    monkeypatch.setattr(out, "resume_scores", lambda jid: [{"file": "mine.pdf", "name": "mine.pdf", "score": 85}])
    assert out.best_resume(2)["name"] == "mine.pdf" and made == [1]  # good enough: nothing made
    monkeypatch.setattr(out, "resume_scores", lambda jid: [{"file": "mine.pdf", "name": "mine.pdf", "score": 65}])
    monkeypatch.setattr(out, "tailor", lambda jid: {"file": "t.pdf", "name": "t.pdf", "score": 50})
    assert out.best_resume(3)["name"] == "mine.pdf"  # tailored one scored worse: keep yours


def test_keywords_count_as_supported_by_name_or_clear_evidence_only():
    src = "built llm agents with langgraph and groq; data preprocessing and eda in pandas; fastapi services"
    assert resumes.supported("Generative AI", src) and resumes.supported("prompt engineering", src)
    assert resumes.supported("data cleaning", src) and resumes.supported("REST APIs", src)
    assert not resumes.supported("TensorFlow", src) and not resumes.supported("ChatGPT", src)  # tools need their name


def test_open_mail_fills_gmail_and_keeps_one_outreach_item(tmp_path, monkeypatch):
    (tmp_path / "outreach.json").write_text(json.dumps({"from_name": "Jane", "gmail_account": "me@x.io",
                                                         "signature": "Jane | jane.dev"}))
    hub = Hub(Store(tmp_path / "n.db"))
    out = Outreach(hub, tmp_path, log=lambda s: None)
    job_id = hub.add_job({**JOB, "external_id": "9", "fit": 80, "url": "https://x"})
    monkeypatch.setattr(out, "reply", lambda jid: {"text": "Hi, applying.", "subject": "AI intern", "to_email": "hr@acme.io",
                                                   "resume": {"file": "cv.pdf", "name": "cv.pdf", "score": 85}})
    first = out.mail(job_id)
    assert first["to_email"] == "hr@acme.io" and "to=hr%40acme.io" in first["gmail_url"]
    assert "su=AI+intern" in first["gmail_url"] and "Jane+%7C+jane.dev" in first["gmail_url"]
    edited = out.mail(job_id, text="Edited text", to_email="jobs@acme.io")  # edits update the same item
    assert edited["id"] == first["id"] and "Edited+text" in edited["gmail_url"] and "to=jobs%40acme.io" in edited["gmail_url"]
    out.mark(first["id"], "sent")
    assert hub.job(job_id)["status"] == "applied"
    monkeypatch.setattr(out, "reply", lambda jid: {"text": "x", "subject": "", "to_email": "", "resume": None})
    with pytest.raises(ValueError):
        out.mail(job_id)  # no address anywhere: say so


def test_pasted_post_becomes_a_saved_job_with_reply_and_resume(tmp_path, monkeypatch):
    from naman import jobs as jobs_mod

    hub = Hub(Store(tmp_path / "n.db"))
    out = Outreach(hub, tmp_path, log=lambda s: None)
    post = ("We're hiring a Data Analyst Intern (remote). Skills: SQL, Python, Power BI. "
            "Send your CV to hr@acme.io")
    calls = []
    monkeypatch.setattr(out, "best_resume", lambda jid, threshold=None: calls.append(threshold) or
                        {"file": "cv.pdf", "name": "cv.pdf", "score": 88})
    monkeypatch.setattr(out, "reply", lambda jid: {"text": "Hi", "subject": "DA intern", "to_email": "hr@acme.io",
                                                   "resume": None})
    first = out.analyse_paste(post, "linkedin.com/posts/acme_x-activity-7472635279254794241-a")
    assert first["to_email"] == "hr@acme.io" and first["resume"]["score"] == 88 and calls == [85]
    job = hub.job(first["job_id"])
    assert job["source"] == "pasted" and job["status"] == "saved" and "hr@acme.io" in job["contact"]
    assert job["url"].startswith("https://") and job["posted"].startswith("2026-06-16")  # date from the post id
    again = out.analyse_paste(post)
    assert again["job_id"] == first["job_id"]  # the same post pasted twice is one job
    with pytest.raises(ValueError):
        jobs_mod.from_paste("too short", "", jobs_mod.DEFAULT_CONFIG, None)


def test_main_model_out_of_quota_falls_back_to_the_small_one():
    from naman.llm import complete

    class RateLimitError(Exception):
        pass

    used = []

    def create(model, **kw):
        used.append(model)
        if model == "big":
            raise RateLimitError("tokens per day")
        return "ok"

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert complete(client, model="big", messages=[]) == "ok" and used == ["big", "openai/gpt-oss-20b"]
