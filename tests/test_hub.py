"""Control center data, jobs, schedule, automation, builder, Telegram setup, file finding."""

import json
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from naman import automation, claude_code, jobs, tools
from naman.agent import Naman
from naman.hub import Hub, parse_days
from naman.store import Store
from naman.telegram_bot import set_env
from test_naman import FakeChat, call, say, use

CONFIG = {**jobs.DEFAULT_CONFIG, "home": ["jaipur", "rajasthan"]}


@pytest.fixture
def hub():
    return Hub(Store(":memory:"))


def job(**kw):
    base = {"source": "linkedin", "external_id": "1", "title": "Machine Learning Engineer", "company": "Acme",
            "location": "Jaipur, Rajasthan, India", "url": "https://x", "skills": [], "min_years": -1}
    return {**base, **kw}


# --- jobs ---------------------------------------------------------------------------------------


def test_fit_rewards_skills_role_experience_and_place():
    good, why = jobs.fit(job(skills=["python", "pytorch", "sql"], min_years=0), CONFIG)
    assert good == 100 and "home +10" in why
    senior, why = jobs.fit(job(title="Senior ML Engineer", skills=["python", "pytorch", "sql"]), CONFIG)
    assert senior == 45 and any("too senior" in r for r in why)  # "Senior" caps the score, number or not
    off, _ = jobs.fit(job(title="Sales Executive", skills=["negotiation", "crm"], location="Dubai"), CONFIG)
    assert off < 30


def test_skill_matching_uses_whole_words():
    assert jobs._skill_match("react.js", ["react"]) and jobs._skill_match("python", ["python"])
    assert not jobs._skill_match("go", ["mongodb"]) and not jobs._skill_match("r", ["react"])


def test_remote_jobs_closed_to_india_are_skipped():
    assert not jobs.open_to_india(job(source="remotive", location="USA, Canada"))
    assert jobs.open_to_india(job(source="remotive", location="Worldwide"))
    assert jobs.open_to_india(job(source="jobicy", location="APAC"))
    assert jobs.open_to_india(job(source="linkedin_post", location=""))


def test_hiring_post_takes_role_company_and_contact_from_the_post():
    post = job(source="linkedin_post", title="HIRING | AI/ML FRESHERS", company="", location="",
               author="A2IT InternEdge", author_url="https://in.linkedin.com/company/a2it",
               links=["https://lnkd.in/abc"])
    data = {"role": "AI/ML Intern", "company": "ExampleCo", "location": "Mohali", "summary": "s",
            "experience": "Freshers", "min_years": 0, "skills": ["python"], "salary": "Rs 7,000/month",
            "contact": "hr@example-co.in", "apply": "Send your CV to hr@example-co.in", "work_mode": "onsite"}
    merged = jobs.apply_extraction(post, data)
    assert (merged["title"], merged["company"], merged["location"]) == ("AI/ML Intern", "ExampleCo", "Mohali")
    assert "hr@example-co.in" in merged["contact"] and "Posted by A2IT InternEdge" in merged["contact"]
    assert "https://lnkd.in/abc" in merged["contact"]


def test_rules_fallback_finds_contacts_and_experience():
    data = jobs.extract_with_rules(job(description="We're hiring! 0-1 years. Python, SQL. Mail jobs@acme.in or "
                                                   "call 98765 43210"), CONFIG)
    assert data["min_years"] == 0 and "jobs@acme.in" in data["contact"] and "98765 43210" in data["contact"]
    assert {"python", "sql"} <= set(data["skills"])


def test_jobs_are_stored_once_even_across_boards(hub):
    assert hub.add_job(job(external_id="1", fit=80))
    assert hub.add_job(job(external_id="1", fit=80)) is None  # same posting
    assert hub.add_job(job(source="remotive", external_id="9", title="Machine learning engineer!", fit=80)) is None
    assert hub.add_job(job(external_id="2", title="Data Analyst", fit=60))
    assert [j["title"] for j in hub.jobs()] == ["Machine Learning Engineer", "Data Analyst"]  # best fit first
    first = hub.jobs()[0]["id"]
    hub.set_job_status(first, "hidden")
    assert [j["title"] for j in hub.jobs()] == ["Data Analyst"]  # hidden jobs leave the list


# --- schedule, what next ---------------------------------------------------------------------


def test_schedule_days_and_times():
    assert parse_days("weekdays") == ["mon", "tue", "wed", "thu", "fri"]
    assert parse_days("fri-mon") == ["fri", "sat", "sun", "mon"]
    assert parse_days("Monday, Wednesday") == ["mon", "wed"]
    with pytest.raises(ValueError):
        parse_days("someday")


def test_whats_next_combines_schedule_and_tasks(hub):
    hub.add_block("weekdays", "10am", "6pm", "Acme")
    hub.add_block("weekdays", "20:00", "22:00", "DSA")
    task = hub.store.add_task("Send invoice", "Acme", "admin", "high", "2026-10-01")
    tuesday_noon = datetime(2026, 9, 29, 12, 0)
    out = tools.whats_next(hub.store, hub, tuesday_noon)
    assert out["schedule_now"]["title"] == "Acme" and out["schedule_next"]["title"] == "DSA"
    assert out["suggested_tasks"][0]["id"] == task
    with pytest.raises(ValueError):
        hub.add_block("mon", "18:00", "09:00", "overnight")  # can't cross midnight


def test_finished_task_through_chat_is_ticked_off(hub):
    task = hub.store.add_task("Update resume", "Job search", "job-search", "high", None)
    model = FakeChat(use(call("complete_task", {"task_id": task})), say("Ticked off. Next: DSA at 8."))
    naman = Naman(hub.store, model, InMemorySaver())
    assert naman.send("t", "I finished updating my resume").text.startswith("Ticked off")
    assert hub.store.list_tasks() == []


# --- automation --------------------------------------------------------------------------------


def test_automation_runs_each_slot_once_and_catches_up(tmp_path, monkeypatch):
    hub = Hub(Store(tmp_path / "n.db"))
    said = []
    auto = automation.Automation(hub, tmp_path, said.append)
    ran = []
    monkeypatch.setattr(auto, "refresh_jobs", lambda: ran.append("jobs") or {"seen": 40})
    monkeypatch.setattr(auto, "refresh_feed", lambda: ran.append("feed") or {"seen": 12})
    monkeypatch.setattr(auto, "refresh_tech", lambda: ran.append("tech") or {"added": 3})
    assert auto.tick(datetime(2026, 10, 1, 1, 0)) == []  # nothing due yet
    # The laptop slept through 02:00; at 07:30 the search runs with the tech news, the 08:00 brief waits.
    assert auto.tick(datetime(2026, 10, 1, 7, 30)) == ["jobs_refresh", "tech_news"]
    assert auto.tick(datetime(2026, 10, 1, 7, 45)) == []  # never twice
    assert auto.tick(datetime(2026, 10, 1, 8, 1)) == ["morning_brief"]
    assert said and said[0].startswith("Good morning")
    # Next day, laptop only on at 23:00: the 14:00 and 18:00 searches are late (one run covers both),
    # the 19:30 tech news still runs, the morning ones are skipped.
    assert auto.tick(datetime(2026, 10, 2, 23, 0)) == ["jobs_refresh", "tech_news", "evening_checkin"]
    assert ran.count("jobs") == 2


def test_a_search_that_reached_nothing_is_retried(tmp_path, monkeypatch):
    auto = automation.Automation(Hub(Store(tmp_path / "n.db")), tmp_path, lambda text: None, log=lambda s: None)
    online = []
    monkeypatch.setattr(auto, "refresh_jobs", lambda: {"seen": 30 if online else 0})
    monkeypatch.setattr(auto, "refresh_feed", lambda: {"seen": 5})
    assert auto.tick(datetime(2026, 10, 1, 2, 0)) == ["jobs_refresh"]  # Wi-Fi asleep: nothing seen
    assert auto.tick(datetime(2026, 10, 1, 2, 10)) == []  # waits 15 minutes
    online.append(True)
    assert auto.tick(datetime(2026, 10, 1, 2, 16)) == ["jobs_refresh"]  # tried again, worked
    assert "jobs_refresh" not in auto.tick(datetime(2026, 10, 1, 2, 40))  # and done for the night


def test_morning_brief_lists_schedule_tasks_and_new_jobs(hub):
    hub.add_block("daily", "09:00", "10:00", "DSA practice")
    hub.store.add_task("Pay rent", "Personal", "personal", "high", "2026-10-01")
    hub.add_job(job(fit=82, title="AI Engineer"))
    text = automation.morning_brief(hub, "Alex", date(2026, 10, 1))
    assert "Good morning, Alex" in text and "DSA practice" in text and "Pay rent" in text
    assert "[82] AI Engineer" in text


# --- control center ----------------------------------------------------------------------------


def test_control_center_needs_the_key_and_a_local_host(tmp_path):
    from naman.control import ControlCenter

    store = Store(tmp_path / "n.db")
    naman = Naman(store, FakeChat(say("hi")), InMemorySaver())
    center = ControlCenter(naman, port=18765, log=lambda s: None)
    center.start()
    base = f"http://127.0.0.1:{center.port}"
    try:
        def get(path, key=None, host=None):
            req = urllib.request.Request(base + path, headers={k: v for k, v in
                                                               (("X-Naman-Key", key), ("Host", host)) if v})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        assert get("/api/overview")[0] == 401  # no key
        assert get("/api/overview", "wrong")[0] == 401
        assert get("/api/overview", center.token, host="evil.example")[0] == 403  # DNS rebinding
        code, body = get("/api/overview", center.token)
        assert code == 200 and "counts" in json.loads(body)
        assert get("/")[0] == 200  # the page itself holds no data
    finally:
        center.stop()


# --- Claude Code builder -----------------------------------------------------------------------


def test_builder_refuses_broad_or_system_folders(tmp_path):
    assert "too broad" in claude_code.check_folder(str(Path.home()), False)
    assert "too broad" in claude_code.check_folder("C:/", False)
    assert "system folder" in claude_code.check_folder("C:/Windows/Temp/x", True)
    assert "doesn't exist" in claude_code.check_folder(str(tmp_path / "nope"), False)
    assert claude_code.check_folder(str(tmp_path / "new"), True) == (tmp_path / "new").resolve()
    assert "Bash(git push:*)" in claude_code.DISALLOWED_TOOLS


def test_builder_reads_claude_codes_result():
    out = 'noise\n{"type":"result","subtype":"success","is_error":false,"result":"Built it."}'
    assert claude_code.parse_result(out, 0) == (True, "Built it.")
    assert claude_code.parse_result('{"type":"result","is_error":true,"result":"nope"}', 1)[0] is False
    assert claude_code.parse_result("crash", 1)[0] is False


# --- Telegram setup, files -------------------------------------------------------------------------


def test_set_env_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nGROQ_API_KEY=abc\nTELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    set_env(env, "TELEGRAM_BOT_TOKEN", "123:xyz")
    set_env(env, "TELEGRAM_ALLOWED_USER_IDS", "42")
    assert env.read_text(encoding="utf-8") == ("# comment\nGROQ_API_KEY=abc\nTELEGRAM_BOT_TOKEN=123:xyz\n"
                                               "TELEGRAM_ALLOWED_USER_IDS=42\n")


def test_desktop_paths_fall_back_to_onedrive(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # what ~ expands to on Windows
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "OneDrive" / "Desktop").mkdir(parents=True)
    sheet = tmp_path / "OneDrive" / "Desktop" / "Internship details.xlsx"
    sheet.write_text("x")
    assert tools._resolve("~/Desktop/Internship details.xlsx") == sheet
    assert tools._resolve("~/Desktop/internship details") == sheet  # case and extension slips
    monkeypatch.setattr(tools, "_known_folders", lambda: [tmp_path / "OneDrive" / "Desktop"])
    assert tools.find_files("internship details") == [str(sheet)]


def test_post_dates_come_from_linkedin_ids_and_old_jobs_drop_out(hub):
    from datetime import timedelta

    url = "https://www.linkedin.com/posts/acme_hiring-activity-7472635279254794241-WcOm"
    assert jobs.post_time(url).date().isoformat() == "2026-06-16"
    assert jobs.too_old({"source": "linkedin_post", "url": url, "posted": ""}, 7)  # date from the id alone
    assert jobs.too_old({"source": "truelancer", "url": "", "posted": "2026-03-20T07:00:11.000000Z"}, 7)
    fresh = (datetime.now() - timedelta(days=1)).date().isoformat()
    stale = (datetime.now() - timedelta(days=20)).date().isoformat()
    hub.add_job(job(external_id="a", title="Fresh role", posted=fresh, fit=70))
    old_id = hub.add_job(job(external_id="b", title="Stale role", posted=stale, fit=90))
    assert [j["title"] for j in hub.jobs()] == ["Fresh role"]
    hub.set_job_status(old_id, "saved")  # saved jobs stay, however old
    assert {j["title"] for j in hub.jobs(status="saved")} == {"Stale role"}
    assert len(hub.jobs(max_age_days=None)) == 2
