"""Freelance/remote/home-city focus, company contacts, cold-email data and email drafts."""

import json
from types import SimpleNamespace

import pytest

from naman import contacts, jobs, outreach
from naman.hub import Hub
from naman.store import Store

CONFIG = {**jobs.DEFAULT_CONFIG, "home": ["jaipur", "rajasthan"]}


def job(**kw):
    base = {"source": "linkedin", "external_id": "1", "title": "Data Analyst", "company": "Acme", "location": "Pune, India",
            "url": "https://x", "skills": ["sql", "python"], "min_years": 0}
    return {**base, **kw}


def test_home_remote_and_freelance_rank_above_onsite_elsewhere():
    home = jobs.fit(job(location="Jaipur, Rajasthan, India"), CONFIG)[0]
    remote = jobs.fit(job(work_mode="remote"), CONFIG)[0]
    gig = jobs.fit(job(source="freelancer", title="Freelance data analysis project", freelance=True,
                       location="Remote", work_mode="remote"), CONFIG)[0]
    pune = jobs.fit(job(), CONFIG)
    assert min(home, remote, gig) > pune[0] and "on-site away from home" in pune[1]
    assert jobs.is_kind(job(title="Data Analyst Intern"), "internship")
    assert jobs.is_kind(job(title="Contract ML engineer"), "freelance")


def test_published_emails_only_cleaned_and_ranked():
    text = ('Write to \u003ecareers@acme.io, sales@acme.io, ravi.k@acme.io, hr@acme.io or logo@2x.png '
            'and test@example.com; partner@other.com')
    found = contacts.clean_emails(text, "acme.io")
    assert set(found) == {"careers@acme.io", "sales@acme.io", "ravi.k@acme.io", "hr@acme.io"}
    assert sorted(found, key=contacts.rank)[:2] == ["careers@acme.io", "hr@acme.io"]
    assert contacts.rank("sales@acme.io") > contacts.rank("ravi.k@acme.io") > contacts.rank("info@acme.io")


def test_best_email_prefers_the_post_then_hr_addresses():
    post = job(contact="Send your CV to jobs@startup.in", description="")
    assert contacts.best_email(post, {"emails": [{"email": "hr@acme.io"}]}) == "jobs@startup.in"
    found = {"emails": [{"email": "sales@acme.io"}, {"email": "careers@acme.io"}]}
    assert contacts.best_email(job(contact=""), found) == "careers@acme.io"
    assert contacts.best_email(job(contact=""), {"emails": [{"email": "sales@acme.io"}]}) == ""  # not sales@


@pytest.fixture
def hub(tmp_path):
    return Hub(Store(tmp_path / "n.db"))


def test_companies_and_csv_for_cold_emailing(hub):
    hub.add_job(job(company="Acme", fit=80, title="Data Analyst"))
    hub.add_job(job(external_id="2", company="Freelancer.com client", fit=70, title="Gig"))
    contacts.add_company(hub, "Porter")
    hub.put("contacts:acme", json.dumps({"domain": "acme.io", "about": "Analytics for retail.",
                                         "emails": [{"email": "careers@acme.io", "source": "x", "kind": "role"}],
                                         "people": [{"name": "Riya", "title": "Talent Acquisition", "url": "u"}],
                                         "checked": "2026-10-01T10:00:00"}))
    rows = contacts.companies(hub)
    assert [r["company"] for r in rows] == ["Porter", "Acme"]  # added ones first; marketplace clients left out
    csv = contacts.csv_export(hub)
    assert "Acme,acme.io,Analytics for retail.,careers@acme.io" in csv and "Riya (Talent Acquisition)" in csv


class FakeLLM:
    def __init__(self):
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        content = json.dumps({"subject": "Data Analyst application", "body": "Hi, I build dashboards at Acme."})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_drafts_are_written_and_never_repeated(hub, tmp_path):
    hub.store.remember("Private: salary details")
    hub.store.remember("Project Sensei AI: stock analysis with SQL and Python")
    llm = FakeLLM()
    (tmp_path / "outreach.json").write_text(json.dumps({"from_name": "Alex", "gmail_account": "me@example.com"}))
    out = outreach.Outreach(hub, tmp_path, llm=lambda: llm, log=lambda s: None)
    first = hub.add_job(job(contact="hr@acme.io", fit=82))
    second = hub.add_job(job(external_id="2", title="ML Engineer", company="Other", contact="hr@acme.io", fit=75))
    draft = out.draft(first, find_contacts=False)
    assert draft["to_email"] == "hr@acme.io" and draft["status"] == "draft"
    assert "Sensei" in llm.prompts[0] and "Private" not in llm.prompts[0]  # real facts in, private facts out
    assert "mail.google.com" in draft["gmail_url"] and "authuser=me%40example.com" in draft["gmail_url"]
    assert "already" in out.draft(second, find_contacts=False)  # same address: no second email
    out.mark(draft["id"], "sent")
    assert hub.job(first)["status"] == "applied"


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, msg):
        self.sent.append(msg)


def test_send_attaches_the_resume_and_refuses_unfinished_drafts(hub, tmp_path, monkeypatch):
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "outreach.json").write_text(json.dumps({"from_name": "Alex", "gmail_account": "me@example.com",
                                                         "signature": "Alex Doe", "resumes": {"": str(resume)}}))
    out = outreach.Outreach(hub, tmp_path, llm=FakeLLM, log=lambda s: None)
    job_id = hub.add_job(job(contact="hr@acme.io", fit=82))
    draft = out.draft(job_id, find_contacts=False)
    smtp = FakeSMTP()

    out.edit(draft["id"], {"body": "Hi. My notice period is [notice period]."})
    with pytest.raises(ValueError, match="notice period"):
        out.send(draft["id"], smtp=lambda: smtp)
    out.edit(draft["id"], {"body": "Hi, I build dashboards."})
    monkeypatch.delenv("NAMAN_GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="App Password"):  # not set up: nothing goes out
        out.send(draft["id"])
    assert not smtp.sent and out.item(draft["id"])["status"] == "draft"

    result = out.send(draft["id"], smtp=lambda: smtp)
    msg = smtp.sent[0]
    assert result["attached"] == "Resume.pdf" and msg["To"] == "hr@acme.io" and "me@example.com" in msg["From"]
    assert "Alex Doe" in msg.get_body(("plain",)).get_content()
    assert [a.get_filename() for a in msg.iter_attachments()] == ["Resume.pdf"]
    assert out.item(draft["id"])["status"] == "sent" and hub.job(job_id)["status"] == "applied"
    with pytest.raises(ValueError, match="not a draft"):  # never twice
        out.send(draft["id"], smtp=lambda: smtp)


def test_web_pages_go_to_chrome_other_links_to_their_apps(monkeypatch):
    import webbrowser

    from naman import browser

    launched, started = [], []
    monkeypatch.setattr(browser, "chrome_path", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(browser, "chrome_profile", lambda: "Profile 1")
    monkeypatch.setattr(browser.subprocess, "Popen", launched.append)
    monkeypatch.setattr(browser.os, "startfile", started.append, raising=False)
    monkeypatch.delenv("NAMAN_BROWSER", raising=False)
    monkeypatch.setattr(webbrowser, "_tryorder", list(webbrowser._tryorder or []))  # leave the real registry alone
    monkeypatch.setattr(webbrowser, "_browsers", dict(webbrowser._browsers))
    assert browser.use_chrome()
    webbrowser.open("https://www.youtube.com/watch?v=x")
    webbrowser.open("whatsapp://send?text=hi")
    assert launched == [[r"C:\Chrome\chrome.exe", "--profile-directory=Profile 1", "https://www.youtube.com/watch?v=x"]]
    assert started == ["whatsapp://send?text=hi"]
    monkeypatch.setenv("NAMAN_BROWSER", "default")
    assert not browser.use_chrome()


def test_low_paying_gigs_are_capped():
    gig = job(source="truelancer", title="Data analysis project", freelance=True, location="Remote", work_mode="remote")
    cheap = jobs.fit({**gig, "salary": "USD 3 /hour"}, CONFIG)
    fair = jobs.fit({**gig, "salary": "USD 15-25 /hour"}, CONFIG)
    assert cheap[0] == 40 and any("pay too low" in r for r in cheap[1])
    assert fair[0] > 40 and not any("pay too low" in r for r in fair[1])
    assert jobs.fit({**gig, "salary": "INR 600-1500 fixed"}, CONFIG)[0] == 40  # ~$18 total
    assert jobs.pay_in_usd("INR 12500-37500 fixed") == (37500 * 0.012, "fixed")


def test_company_websites_must_match_the_name(hub):
    assert contacts.domain_score("rapido.bike", "Rapido") > contacts.domain_score("rapidotrains.com", "Rapido")
    assert contacts.domain_score("tarento.com", "Tarento Group") == 3
    assert contacts.domain_score("studentinternships.org", "UM IT PRIVATE LIMITED") == 0  # no shared name
    assert contacts.domain_score("devsunite.com", "Devryxa") == 0


def test_cold_email_list_exports_to_excel(hub, tmp_path):
    from openpyxl import load_workbook

    hub.put("contacts:acme", json.dumps({"domain": "acme.io", "about": "Retail analytics.", "checked": "2026-10-01",
                                         "emails": [{"email": "careers@acme.io", "source": "x", "kind": "role"}],
                                         "people": [{"name": "Riya", "title": "Talent Acquisition", "url": "https://l/riya"}]}))
    hub.add_job(job(company="Acme", fit=80))
    sheet = load_workbook(contacts.xlsx_export(hub, tmp_path / "c.xlsx")).active
    assert [c.value for c in sheet[1]][:4] == ["Company", "Website", "About", "Best email"]
    assert sheet["A2"].value == "Acme" and sheet["D2"].value == "careers@acme.io"
    assert sheet["D2"].hyperlink.target == "mailto:careers@acme.io" and "Riya" in sheet["F2"].value


def test_auto_apply_sends_the_best_drafts_with_the_resume_up_to_the_daily_cap(hub, tmp_path):
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    cfg = {"from_name": "Alex", "gmail_account": "me@example.com", "resumes": {"": str(resume)},
           "auto_send": False, "auto_send_min_fit": 70, "auto_send_per_day": 2}
    (tmp_path / "outreach.json").write_text(json.dumps(cfg))
    said = []
    out = outreach.Outreach(hub, tmp_path, said.append, llm=FakeLLM, log=lambda s: None)
    ids = [hub.add_job(job(external_id=str(i), title=f"Role {i}", company=f"Co{i}", contact=f"hr@co{i}.io", fit=fit))
           for i, fit in enumerate((90, 80, 75, 60))]
    drafts = [out.draft(i, find_contacts=False) for i in ids]
    smtp = FakeSMTP()
    assert out.auto_send(smtp=lambda: smtp) == []  # off: nothing goes
    (tmp_path / "outreach.json").write_text(json.dumps(cfg | {"auto_send": True}))
    out.edit(drafts[0]["id"], {"body": "Hi, notice period [notice period]."})  # needs you: skipped
    sent = out.auto_send(smtp=lambda: smtp)
    assert [s["fit"] for s in sent] == [80, 75]  # best first, the 60 never, capped at 2 a day
    assert all([a.get_filename() for a in m.iter_attachments()] == ["Resume.pdf"] for m in smtp.sent)
    assert out.auto_send(smtp=lambda: smtp) == []  # today's cap reached
    assert said[-1].startswith("Applied to 2 jobs by email, résumé attached")


def test_send_all_sends_every_ready_draft_and_keeps_back_the_rest(hub, tmp_path):
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "outreach.json").write_text(json.dumps({"from_name": "Alex", "gmail_account": "me@example.com",
                                                         "resumes": {"": str(resume)}}))
    out = outreach.Outreach(hub, tmp_path, llm=FakeLLM, log=lambda s: None)
    ids = [hub.add_job(job(external_id=str(i), company=f"Co{i}", contact=f"hr@co{i}.io", fit=70)) for i in range(3)]
    drafts = [out.draft(i, find_contacts=False) for i in ids]
    out.edit(drafts[1]["id"], {"body": "Available from [start date]."})
    smtp = FakeSMTP()
    r = out.send_all(pause_s=0, smtp=lambda: smtp)
    assert len(r["sent"]) == 2 and len(smtp.sent) == 2 and r["stopped"] == ""
    assert r["skipped"] == [{"id": drafts[1]["id"], "company": "Co1", "why": "fill in [start date] before sending"}]
    assert [d["status"] for d in out.items()] .count("draft") == 1


def test_two_clicks_at_once_send_one_email(hub, tmp_path):
    import threading
    import time

    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "outreach.json").write_text(json.dumps({"gmail_account": "me@example.com", "resumes": {"": str(resume)}}))
    out = outreach.Outreach(hub, tmp_path, llm=FakeLLM, log=lambda s: None)
    draft = out.draft(hub.add_job(job(contact="hr@acme.io", fit=80)), find_contacts=False)

    class SlowSMTP(FakeSMTP):
        def send_message(self, msg):
            time.sleep(0.2)  # Gmail takes a moment: the second click lands meanwhile
            super().send_message(msg)

    smtp, errors = SlowSMTP(), []

    def click():
        try:
            out.send(draft["id"], smtp=lambda: smtp)
        except ValueError as exc:
            errors.append(str(exc))

    clicks = [threading.Thread(target=click) for _ in range(2)]
    for c in clicks:
        c.start()
    for c in clicks:
        c.join()
    assert len(smtp.sent) == 1 and errors == [f"email #{draft['id']} is sent, not a draft"]
