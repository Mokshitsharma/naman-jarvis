"""Live updates: GitHub news, the open-tasks nudge, quiet hours, notification timing."""

from datetime import datetime

from naman import automation, github
from naman.hub import Hub
from naman.overlay import read_time
from naman.store import Store


def test_github_news_is_what_went_up_since_last_time():
    old = {"followers": 3, "repos": {"ToolForge": {"stars": 1, "forks": 0, "issues": 0},
                                     "Sensei": {"stars": 2, "forks": 1, "issues": 1}}}
    new = {"followers": 4, "repos": {"ToolForge": {"stars": 3, "forks": 1, "issues": 0},
                                     "Sensei": {"stars": 1, "forks": 1, "issues": 0},
                                     "Mario": {"stars": 0, "forks": 0, "issues": 0}}}
    assert github.changes(old, new) == ["You have 1 new GitHub follower (4 now).", "ToolForge got 2 new stars.",
                                        "ToolForge got a new fork.", "New repo on your GitHub: Mario."]
    assert github.changes(new, new) == []


def test_github_snapshot_skips_forks():
    pages = {"/users/me/repos?per_page=100&type=owner&sort=updated": [
                 {"name": "Mine", "stargazers_count": 2, "forks_count": 0, "open_issues_count": 1, "fork": False},
                 {"name": "Theirs", "stargazers_count": 9, "forks_count": 9, "open_issues_count": 9, "fork": True}],
             "/users/me": {"followers": 5}}
    snap = github.snapshot("me", fetch=pages.__getitem__)
    assert snap == {"followers": 5, "repos": {"Mine": {"stars": 2, "forks": 0, "issues": 1}}}


def test_task_nudge_counts_open_work_and_names_the_next(tmp_path):
    hub = Hub(Store(tmp_path / "n.db"))
    assert automation.task_nudge(hub) == ""
    hub.store.add_task("Record the ToolForge demo", "ToolForge", "coding", "high", "2026-10-01")
    hub.store.add_task("Update resume", "Career", "job-search", "medium", None)
    text = automation.task_nudge(hub, datetime(2026, 10, 13).date())
    assert text.startswith("You have 2 tasks not done (1 overdue).")
    assert "Next up: Record the ToolForge demo, due Thu 01 Oct." in text


def test_quiet_hours_cross_midnight():
    cfg = {"quiet_hours": ["22:30", "08:00"]}
    assert automation.in_quiet_hours(cfg, datetime(2026, 10, 13, 23, 0))
    assert automation.in_quiet_hours(cfg, datetime(2026, 10, 13, 7, 59))
    assert not automation.in_quiet_hours(cfg, datetime(2026, 10, 13, 12, 0))


def test_messages_stay_long_enough_to_read_and_no_longer():
    assert 4 < read_time("Opened VS Code.") < 5
    assert read_time("x" * 2000) == 12.0 and read_time("x" * 2000, notification=True) == 10.0


def test_feeds_parse_rss_and_atom():
    from naman import technews

    rss = """<rss><channel><item><title>GPT-x is here</title><link>https://openai.com/x</link>
      <pubDate>Mon, 12 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;A new model.&lt;/p&gt;</description></item></channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>JDK 26</title>
      <link rel="alternate" href="https://inside.java/26"/><updated>2026-10-11T09:00:00Z</updated>
      <summary>Compact source files.</summary></entry></feed>"""
    a, = technews.parse_feed(rss, "OpenAI")
    b, = technews.parse_feed(atom, "Java")
    assert a == {"source": "OpenAI", "title": "GPT-x is here", "url": "https://openai.com/x",
                 "published": "2026-10-12T10:00:00+00:00", "snippet": "A new model."}
    assert b["url"] == "https://inside.java/26" and b["published"].startswith("2026-10-11")
    assert technews.parse_feed("not xml", "x") == []


def test_form_helper_answers_never_invent_unknowns():
    from naman import formfill

    text = """<think>looking</think>Here you go:
    {"page": "Apply: Data Analyst at Acme", "fields": [
      {"label": "Full name", "answer": "Alex Doe", "status": "known"},
      {"label": "Why Acme?", "answer": "I build dashboards...", "status": "written"},
      {"label": "Expected salary", "answer": "6 LPA", "status": "unknown"},
      {"label": "Notice period", "answer": "", "status": "known"},
      {"label": "", "answer": "x", "status": "known"}]}"""
    data = formfill.parse(text)
    assert data["page"] == "Apply: Data Analyst at Acme"
    assert [(f["label"], f["answer"], f["status"]) for f in data["fields"]] == [
        ("Full name", "Alex Doe", "known"), ("Why Acme?", "I build dashboards...", "written"),
        ("Expected salary", "", "unknown"), ("Notice period", "", "unknown")]
    assert formfill.parse("no json here") == {"page": "", "fields": []}


def test_remembered_form_answers_feed_the_next_form(tmp_path):
    from naman import formfill

    store = Store(tmp_path / "n.db")
    formfill.remember_answer(store, "Notice period:*", "Immediate")
    ctx = formfill.context(store, {"from_name": "Alex", "gmail_account": "me@example.com"})
    assert "Form answer, Notice period: Immediate" in ctx["facts"]
    assert "me@example.com" in ctx["profile"] and ctx["name"] == "Alex"
