import json
from datetime import datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from naman.agent import Naman
from naman.reminders import Scheduler, resolve_due
from naman.router import KeywordRouter
from naman.skills import info, system
from naman.store import Store
from naman.tools import Tool, Toolset, build_toolsets


@pytest.fixture
def store():
    return Store(":memory:")


# --- router ----------------------------------------------------------------------------


def test_keyword_router_picks_skill_packs(store):
    sets = build_toolsets(store)
    sets["mcp:pokeapi"] = Toolset("mcp:pokeapi", "pokeapi (MCP server): get_pokemon: details of a pokemon")
    route = KeywordRouter().choose
    assert route("turn the volume up", sets, []) == ["laptop"]
    assert route("what's the weather in Jaipur", sets, []) == ["knowledge"]
    assert route("remind me in 10 minutes to stretch", sets, []) == ["reminders"]
    assert set(route("play arijit singh songs", sets, [])) == {"media"}
    assert route("tell me about pikachu the pokemon", sets, []) == ["mcp:pokeapi"]
    assert route("louder", sets, ["laptop"]) == ["laptop"]  # short follow-up keeps the last pack
    assert route("hello there, how are you doing", sets, ["laptop"]) == []


class Script:
    def __init__(self, *msgs):
        self.msgs, self.tools_seen = list(msgs), []

    def chat(self, messages, tools):
        self.tools_seen.append({t["function"]["name"] for t in tools})
        return self.msgs.pop(0)


def say(text):
    return {"role": "assistant", "content": text}


def use(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_model_only_sees_the_routed_packs(store):
    model = Script(say("Volume is at 50."))
    Naman(store, model, InMemorySaver(), router=KeywordRouter()).send("t", "set volume to 50")
    seen = model.tools_seen[0]
    assert {"volume", "media", "open_app", "remember"} <= seen  # laptop pack + always-on core/memory
    assert not seen & {"weather", "add_task", "set_reminder"}  # other packs stay out of the request


def test_saying_a_routine_name_loads_routines(store):
    store.save_routine("good morning", "Give the weather for Jaipur, then my briefing, then open VS Code.")
    model = Script(use("run_routine", {"name": "good morning"}), say("Morning! Here's your day."))
    reply = Naman(store, model, InMemorySaver(), router=KeywordRouter()).send("t", "Good morning Naman")
    assert "run_routine" in model.tools_seen[0]
    assert reply.text == "Morning! Here's your day."


def test_mcp_toolsets_are_routable(store):
    extra = Toolset("mcp:files", "files (MCP server): read_text_file: read a file",
                    [Tool("files__read_text_file", "[files] read", {"type": "object", "properties": {}}, lambda: "")])
    model = Script(say("ok"))
    Naman(store, model, InMemorySaver(), extra_toolsets=[extra], router=KeywordRouter()).send("t", "read that file")
    assert "files__read_text_file" in model.tools_seen[0]


# --- reminders & routines --------------------------------------------------------------


def test_resolve_due_times():
    now = datetime(2026, 9, 29, 18, 0)
    assert resolve_due(None, 10, now) == now + timedelta(minutes=10)
    assert resolve_due("2026-09-29T19:30", None, now) == datetime(2026, 9, 29, 19, 30)
    with pytest.raises(ValueError):
        resolve_due("2026-09-29T17:00", None, now)  # in the past
    with pytest.raises(ValueError):
        resolve_due(None, None, now)


def test_scheduler_fires_each_reminder_once(store):
    heard = []
    sched = Scheduler(store, notify_desktop=False)
    sched.add_listener(heard.append)
    sched.add_listener(lambda text: 1 / 0)  # a broken channel must not stop the others
    now = datetime.now()
    store.add_reminder("stretch", now - timedelta(minutes=1))
    store.add_reminder("later", now + timedelta(hours=1))
    assert sched.tick(now) == ["Reminder: stretch"]
    assert sched.tick(now) == []
    assert heard == ["Reminder: stretch"]
    assert [r["message"] for r in store.pending_reminders()] == ["later"]


def test_reminder_and_routine_tools(store):
    tools = {t.name: t for s in build_toolsets(store).values() for t in s.tools}
    result = tools["set_reminder"].run(message="call mom", in_minutes=30)
    assert result["reminder_id"] == 1
    assert tools["cancel_reminder"].run(reminder_id=1) == {"cancelled": True}
    tools["save_routine"].run(name="Work Mode", steps="Open VS Code and play lo-fi.")
    assert "Open VS Code" in tools["run_routine"].run(name="work mode")
    assert tools["run_routine"].run(name="party").startswith("error: no routine called 'party'")


# --- skills ----------------------------------------------------------------------------


def test_hotkeys_parse_and_dangerous_ones_are_blocked():
    assert system.parse_hotkey("Ctrl + Shift + T") == ["ctrl", "shift", "t"]
    assert system.parse_hotkey("control+s") == ["ctrl", "s"]
    assert system.parse_hotkey("alt+f4").startswith("error: alt+f4 is blocked")
    assert system.parse_hotkey("ctrl+alt+delete").startswith("error:")
    assert system.parse_hotkey("ctrl+banana").startswith("error: unknown key")


def test_whatsapp_draft_never_sends(monkeypatch):
    opened = []
    monkeypatch.setattr(info.webbrowser, "open", opened.append)
    result = info.whatsapp_draft("Running 10 min late", "98765 43210")
    assert opened == ["whatsapp://send?phone=919876543210&text=Running%2010%20min%20late"]
    assert "NOT sent" in result


def test_keyword_router_understands_turn_it_up(store):
    assert KeywordRouter().choose("turn it up a bit", build_toolsets(store), []) == ["laptop"]


def test_duplicated_replies_are_collapsed():
    from naman.llm import collapse_repeat

    assert collapse_repeat("Set.Set.") == "Set."
    assert collapse_repeat("Added routine 'work mode'.Added routine 'work mode'.") == "Added routine 'work mode'."
    assert collapse_repeat("Battery at 6% and charging. You should plug it in soon."
                           "Battery at 6% and charging. Plug it in soon.") == "Battery at 6% and charging. Plug it in soon."
    normal = "Opened VS Code. Want me to start lo-fi too?"
    assert collapse_repeat(normal) == normal
    assert collapse_repeat("Yes. Yes, I can do that.") == "Yes. Yes, I can do that."


def test_running_a_routine_unlocks_every_pack(store):
    store.save_routine("work mode", "Open VS Code, then play lo-fi on YouTube.")
    model = Script(use("run_routine", {"name": "work mode"}), say("Done."))
    Naman(store, model, InMemorySaver(), router=KeywordRouter()).send("t", "work mode")
    assert "play_youtube" not in model.tools_seen[0]
    assert "play_youtube" in model.tools_seen[1]  # the step after run_routine can use any skill


def test_obvious_commands_skip_the_router_model(store):
    from naman.router import GroqRouter, obvious

    sets = build_toolsets(store)
    route = KeywordRouter().choose
    fast = ["[voice] hello", "thanks", "open VS Code", "close Chrome", "volume 30", "turn it up a bit",
            "play some lo-fi", "remind me in 10 minutes to stretch", "what's the weather in Jaipur tomorrow?",
            "today's headlines", "Good morning Naman", "take a screenshot"]
    slow = ["what are Pikachu's abilities?", "who won yesterday's match?", "open VS Code and play lo-fi",
            "read the README in ToolForge and summarise it", "No thanks, just book an appointment at 5pm then remind me",
            "what time is it?"]
    for said in fast:
        assert obvious(said, route(said, sets, [])), said
    for said in slow:
        assert not obvious(said, route(said, sets, [])), said

    class NoCalls:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    raise AssertionError("the router model should not be called")

    router = object.__new__(GroqRouter)
    router.client, router.model, router.fallback, router.skipped = NoCalls, "m", KeywordRouter(), 0
    assert router.choose("[voice] volume 30", sets, []) == ["laptop"]
    assert router.choose("louder", sets, ["laptop"]) == ["laptop"]
    assert router.skipped == 2


def test_tab_skill_focuses_the_app_then_presses_the_browser_shortcut(monkeypatch):
    pressed, focused = [], []
    monkeypatch.setattr(system, "_need_windows", lambda: None)
    monkeypatch.setattr(system, "_match_windows", lambda name: [(42, "YouTube - Google Chrome")] if "chrome" in name else [])
    monkeypatch.setattr(system, "_focus", focused.append)
    monkeypatch.setattr(system, "_foreground_title", lambda: "YouTube - Google Chrome")
    monkeypatch.setattr(system, "press_keys", lambda keys: pressed.append(keys) or f"pressed {keys}")
    monkeypatch.setattr(system.time, "sleep", lambda s: None)

    assert system.tab("close", "chrome") == "close tab x1 in: YouTube - Google Chrome"
    assert focused == [42] and pressed == ["ctrl+w"]
    system.tab("reopen")  # no app: the window in front
    system.tab("close", None, 50)  # capped
    assert pressed[1:] == ["ctrl+shift+t"] + ["ctrl+w"] * system.MAX_TABS
    assert system.tab("close", "firefox").startswith("error: no open window")
    assert system.tab("delete").startswith("error")
    monkeypatch.setattr(system, "_foreground_title", lambda: "Naman")
    assert "no app window is in front" in system.tab("close")  # never closes its own window


def test_close_chrome_tab_routes_to_the_laptop_pack_without_the_router_model(store):
    from naman.router import obvious

    sets = build_toolsets(store)
    for said in ["close Chrome tab", "[voice] close this tab", "open a new tab", "next tab"]:
        picked = KeywordRouter().choose(said, sets, [])
        assert picked == ["laptop"] and obvious(said, picked), said
    assert "tab" in {t.name for t in sets["laptop"].tools}
