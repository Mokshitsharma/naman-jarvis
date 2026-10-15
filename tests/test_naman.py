import json
import sqlite3
from datetime import date

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from naman import tools as tools_mod
from naman.agent import Naman, _history
from naman.store import Store
from naman.telegram_bot import allowed_ids


class FakeChat:
    """Plays back scripted assistant messages and records what it was sent."""

    def __init__(self, *script):
        self.script = list(script)
        self.seen = []

    def chat(self, messages, tools):
        self.seen.append(messages)
        return self.script.pop(0)


def call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def say(text):
    return {"role": "assistant", "content": text}


def use(*calls):
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


@pytest.fixture
def store():
    return Store(":memory:")


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(tools_mod.webbrowser, "open", urls.append)
    return urls


def make(store, model, saver=None, confirm_actions=True):
    # confirm_actions=True so opening a URL exercises the approval path
    return Naman(store, model, saver or InMemorySaver(), user_name="Alex", confirm_actions=confirm_actions)


def test_by_default_commands_just_run(store, opened, monkeypatch):
    launched = []
    monkeypatch.setattr(tools_mod, "open_app", lambda name: launched.append(name) or f"opened {name}")
    model = FakeChat(use(call("open_app", {"name": "VS Code"}), call("open_url", {"url": "https://x.com"}, "c2")),
                     say("VS Code and X are open."))
    reply = make(store, model, confirm_actions=False).send("t", "open vs code and x")
    assert reply.approval is None and reply.text == "VS Code and X are open."
    assert launched == ["VS Code"] and opened == ["https://x.com"]


def test_open_path_refuses_runnable_files(tmp_path):
    script = tmp_path / "evil.bat"
    script.write_text("echo hi")
    assert tools_mod._open_path(str(script)).startswith("error: .bat files run code")
    assert tools_mod._open_path(str(tmp_path / "missing.txt")).startswith("error:")


# --- store -----------------------------------------------------------------------------


def test_tasks_sort_by_priority_then_due(store):
    store.add_task("later", "A", "coding", "low", None)
    store.add_task("urgent", "A", "coding", "high", "2026-10-02")
    store.add_task("soon", "B", "admin", "high", "2026-10-01")
    assert [t["title"] for t in store.list_tasks()] == ["soon", "urgent", "later"]
    assert [t["title"] for t in store.list_tasks("b")] == ["soon"]


def test_briefing_and_bad_dates(store):
    old = store.add_task("overdue", "A", "coding", "medium", "2026-09-01")
    store.add_task("today", "A", "coding", "high", "2026-09-29")
    brief = store.briefing(date(2026, 9, 29))
    assert [t["title"] for t in brief["overdue"]] == ["overdue"]
    assert [t["title"] for t in brief["due_today"]] == ["today"]
    assert brief["open_by_project"] == {"A": 2}
    assert store.complete_task(old) and not store.complete_task(old)
    with pytest.raises(ValueError):
        store.add_task("x", "A", "coding", "low", "next friday")


def test_memories_dedupe_and_search(store):
    a = store.remember("Prefers Python over Java")
    assert store.remember("prefers python over java") == a
    store.remember("Works at Acme")
    assert [m["fact"] for m in store.search_memories("which language, python?")] == ["Prefers Python over Java"]
    assert store.forget(a) and not store.forget(a)


# --- agent -----------------------------------------------------------------------------


def test_plain_reply_and_persona(store):
    store.remember("Lives in Jaipur")
    model = FakeChat(say("Hey!"))
    assert make(store, model).send("t", "hi").text == "Hey!"
    system = model.seen[0][0]["content"]
    assert "Alex" in system and "Lives in Jaipur" in system


def test_task_is_added_through_tools(store):
    model = FakeChat(
        use(call("add_task", {"title": "Finish ToolForge README", "project": "ToolForge",
                              "category": "coding", "priority": "high", "due": None})),
        say("Filed under ToolForge, high priority."),
    )
    reply = make(store, model).send("t", "I need to finish the ToolForge readme")
    assert reply.text.startswith("Filed")
    assert store.list_tasks()[0]["project"] == "ToolForge"
    tool_msg = model.seen[1][-1]
    assert tool_msg["role"] == "tool" and '"task_id": 1' in tool_msg["content"]


def test_risky_action_waits_for_approval(store, opened):
    model = FakeChat(use(call("open_url", {"url": "https://github.com"})), say("Opened GitHub."))
    naman = make(store, model)
    reply = naman.send("t", "open github")
    assert reply.approval == {"tool": "open_url", "args": {"url": "https://github.com"}}
    assert opened == []  # nothing happens before the yes
    assert naman.send("t", "hello?").approval is not None  # still waiting
    assert naman.resume("t", True).text == "Opened GitHub."
    assert opened == ["https://github.com"]


def test_denied_action_never_runs(store, opened):
    model = FakeChat(use(call("open_url", {"url": "https://x.com"})), say("Okay, I won't."))
    naman = make(store, model)
    naman.send("t", "open x")
    assert naman.resume("t", False).text == "Okay, I won't."
    assert opened == []
    assert "declined" in model.seen[1][-1]["content"]


def test_two_approvals_each_run_exactly_once(store, opened):
    model = FakeChat(
        use(
            call("open_url", {"url": "https://a.com"}, "c1"),
            call("add_task", {"title": "t", "project": "P", "category": "other", "priority": "low", "due": None}, "c2"),
            call("open_url", {"url": "https://b.com"}, "c3"),
        ),
        say("Done."),
    )
    naman = make(store, model)
    assert naman.send("t", "go").approval["args"]["url"] == "https://a.com"
    assert naman.resume("t", True).approval["args"]["url"] == "https://b.com"
    assert naman.resume("t", False).text == "Done."
    assert opened == ["https://a.com"]
    assert len(store.list_tasks()) == 1  # the unrisky tool ran once, not once per resume


def test_bad_tool_arguments_are_reported_not_crashed(store):
    bad = {"id": "c1", "type": "function", "function": {"name": "remember", "arguments": "{not json"}}
    model = FakeChat(use(bad, call("no_such_tool", {}, "c2")), say("Sorry, retrying later."))
    make(store, model).send("t", "remember this")
    outputs = [m["content"] for m in model.seen[1] if m["role"] == "tool"]
    assert outputs == ["error: arguments were not a JSON object", "error: no tool named no_such_tool"]


def test_conversation_survives_restart(store, tmp_path):
    path = tmp_path / "conv.db"
    first = FakeChat(say("Noted."))
    make(store, first, SqliteSaver(sqlite3.connect(path, check_same_thread=False))).send("main", "my cat is Momo")
    second = FakeChat(say("Momo!"))
    make(store, second, SqliteSaver(sqlite3.connect(path, check_same_thread=False))).send("main", "cat name?")
    sent = [m["content"] for m in second.seen[0] if m["role"] == "user"]
    assert sent == ["my cat is Momo", "cat name?"]


def test_history_window_starts_on_a_user_turn():
    msgs = [{"role": "tool", "content": "x"}, {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert _history(msgs)[0]["role"] == "user"


def test_telegram_allowlist_parsing(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", " 123, 456 ,")
    assert allowed_ids() == {123, 456}
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    assert allowed_ids() == set()


def test_rate_limit_falls_back_to_smaller_model(monkeypatch):
    import groq
    import httpx

    from naman.llm import GroqChat, friendly_error

    monkeypatch.setenv("GROQ_API_KEY", "test")
    chat = GroqChat(model="big", fallback="small")
    used = []

    def fake(model, messages, tools):
        used.append(model)
        if model == "big":
            response = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
            raise groq.RateLimitError("Limit reached on tokens per day (TPD)", response=response, body=None)
        return say("hi from small")

    monkeypatch.setattr(chat, "_chat", fake)
    assert chat.chat([], [])["content"] == "hi from small" and used == ["big", "small"]
    err = groq.RateLimitError("tokens per day (TPD)", response=httpx.Response(
        429, request=httpx.Request("POST", "https://x")), body=None)
    assert "free AI quota is used up" in friendly_error(err)


def test_a_crashed_turn_does_not_block_the_next_one(store):
    class Flaky(FakeChat):
        def chat(self, messages, tools):
            if not self.seen:
                self.seen.append(messages)
                raise RuntimeError("service down")
            return super().chat(messages, tools)

    naman = make(store, Flaky(say("Back now.")))
    with pytest.raises(RuntimeError):
        naman.send("t", "hello")
    assert naman.send("t", "hello again").text == "Back now."


def test_malformed_tool_calls_are_retried(monkeypatch):
    import groq
    import httpx

    from naman.llm import GroqChat

    monkeypatch.setenv("GROQ_API_KEY", "test")
    chat = GroqChat(model="m", fallback="m")
    calls = []

    def fake(model, messages, tools):
        calls.append(model)
        if len(calls) < 3:
            raise groq.BadRequestError("tool_use_failed: 'system_status<|channel|>commentary'",
                                       response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                                       body=None)
        return say("fine now")

    monkeypatch.setattr(chat, "_chat", fake)
    assert chat.chat([], [])["content"] == "fine now" and len(calls) == 3
