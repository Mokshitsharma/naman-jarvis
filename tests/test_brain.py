"""Planning, progress, follow-ups, app reuse, Spotify window finding and the orb."""

import json

import numpy as np
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from naman import apps
from naman.agent import Naman, Reply, describe_step
from naman.orb import OrbRenderer
from naman.router import GroqRouter, KeywordRouter
from naman.skills import spotify, system
from naman.store import Store
from naman.tools import build_toolsets
from naman.voice import FRAME, VoiceLoop, envelope, loudness
from test_naman import FakeChat, call, say, use
from test_voice import FakeMic, Recorder, ScriptedEars, loud, quiet


@pytest.fixture
def store():
    return Store(":memory:")


def planner_answering(sets, steps):
    """A GroqRouter whose model call returns `sets` and `steps`."""

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    content = json.dumps({"sets": sets, "steps": steps})
                    message = type("M", (), {"content": content})
                    return type("R", (), {"choices": [type("C", (), {"message": message})]})

    router = object.__new__(GroqRouter)
    router.client, router.model, router.fallback, router.skipped = Client, "m", KeywordRouter(), 0
    return router


# --- planning ----------------------------------------------------------------------------


def test_router_returns_a_plan_for_multi_step_commands(store):
    sets = build_toolsets(store)
    router = planner_answering(["media"], ["Open Spotify", "Play old songs on Spotify"])
    assert router.route("open spotify and play old songs", sets, []) == (
        ["media"], ["Open Spotify", "Play old songs on Spotify"])
    assert router.choose("open spotify and play old songs", sets, []) == ["media"]  # old interface still works
    assert router.route("volume 30", sets, []) == (["laptop"], [])  # obvious: no model call, no plan


def test_plan_reaches_the_model_and_progress_reaches_the_front_end(store, monkeypatch):
    from naman import tools as tools_mod

    monkeypatch.setattr(tools_mod, "open_app", lambda name: f"opened {name}")
    monkeypatch.setattr(tools_mod, "_spotify_play", lambda query: f"playing '{query}' on Spotify")
    model = FakeChat(use(call("open_app", {"name": "Spotify"})),
                     use(call("spotify_play", {"query": "old hindi classics"}, "c2")),
                     say("Spotify's playing old Hindi classics."))
    router = planner_answering(["media"], ["Open Spotify", "Play old songs on Spotify"])
    naman = Naman(store, model, InMemorySaver(), router=router)
    events = []
    naman.on_progress = lambda event, data: events.append((event, data))

    reply = naman.send("t", "open spotify and play old songs")
    assert reply.text == "Spotify's playing old Hindi classics."
    assert "1. Open Spotify\n2. Play old songs on Spotify" in model.seen[0][0]["content"]
    assert events == [
        ("plan", {"steps": ["Open Spotify", "Play old songs on Spotify"]}),
        ("step", {"text": "Opening Spotify", "index": 1, "total": 2}),
        ("step", {"text": "Playing old hindi classics on Spotify", "index": 2, "total": 2}),
    ]


def test_one_step_plans_are_dropped_and_a_broken_display_never_breaks_a_command(store):
    model = FakeChat(say("Hi!"))
    naman = Naman(store, model, InMemorySaver(), router=planner_answering([], ["Say hi"]))
    naman.on_progress = lambda event, data: 1 / 0
    assert naman.send("t", "hey there, what's new and how are you").text == "Hi!"
    assert "Plan for this request" not in model.seen[0][0]["content"]


def test_step_descriptions():
    assert describe_step("open_app", {"name": "VS Code"}) == "Opening VS Code"
    assert describe_step("open_app", {}) == "Open app"  # missing argument: fall back to the tool name
    assert describe_step("pokeapi__get_pokemon", {"name": "pikachu"}) == "Pokeapi: get pokemon"


# --- voice: levels and follow-ups ---------------------------------------------------------


def test_loudness_scale_and_voice_envelope():
    assert loudness(0) == 0 and loudness(100) == 0 and loudness(1e6) == 1
    assert 0.3 < loudness(2000) < 0.9  # normal speech
    tone = (np.sin(np.linspace(0, 400, 24_000)) * 8000).astype(np.int16)
    env = envelope(np.concatenate([np.zeros(12_000, np.int16), tone]), 24_000)
    assert len(env) == 90 and env[:25].max() == 0 and env[-25:].min() > 0.5


class AsksBack:
    def __init__(self, *replies):
        self.replies, self.sent = list(replies), []

    def send(self, thread, text):
        self.sent.append(text)
        return Reply(text=self.replies.pop(0))


def test_a_question_back_is_answered_without_the_wake_word():
    naman = AsksBack("Which playlist, old Hindi or old English?", "Playing old Hindi classics.")
    utter = loud(8) + quiet(15)
    loop = VoiceLoop(naman, "t", FakeMic(utter + utter), Recorder(), ScriptedEars("play old songs", "hindi"),
                     wake=None, log=lambda s: None, wait_for_enter=lambda: None, follow_up_s=3)
    loop.run_once()
    assert naman.sent == ["[voice] play old songs", "[voice] hindi"]


def test_no_follow_up_after_a_statement_so_music_isnt_mistaken_for_a_command():
    naman = AsksBack("Playing old Hindi classics.")
    loop = VoiceLoop(naman, "t", FakeMic(loud(8) + quiet(15) + loud(40)), Recorder(), ScriptedEars("play old songs"),
                     wake=None, log=lambda s: None, wait_for_enter=lambda: None, follow_up_s=3)
    loop.run_once()
    assert naman.sent == ["[voice] play old songs"]


# --- apps and Spotify ---------------------------------------------------------------------


@pytest.fixture
def fake_windows(monkeypatch):
    shown = {}
    monkeypatch.setattr(system, "WINDOWS", True)
    monkeypatch.setattr(system, "_windows", lambda: list(shown.items()))
    monkeypatch.setattr(system, "process_name", lambda hwnd: "chrome.exe")
    monkeypatch.setattr(spotify, "_windows", lambda: list(shown.items()))
    monkeypatch.setattr(spotify, "process_name", lambda hwnd: "chrome.exe")
    return shown


def test_app_window_skips_browser_tabs_that_mention_the_app(fake_windows):
    fake_windows[1] = "spotify - Google Search - Google Chrome"
    assert system.app_window("Spotify") is None
    assert spotify.spotify_windows() == []
    fake_windows[2] = "Spotify - Old is Gold"  # the web app
    assert system.app_window("Spotify") == 2 and spotify.spotify_window() == 2
    assert system.app_window("Google Chrome") == 1  # the browser itself does count


def test_open_app_brings_an_open_app_to_the_front_instead_of_opening_it_twice(fake_windows, monkeypatch):
    launched, focused = [], []
    monkeypatch.setattr(apps.platform, "system", lambda: "Windows")
    monkeypatch.setattr(apps, "find_app", lambda name: apps.App("Spotify", "spotify-id"))
    monkeypatch.setattr(apps.subprocess, "Popen", launched.append)
    monkeypatch.setattr(system, "_focus", focused.append)
    monkeypatch.setattr(system.time, "sleep", lambda s: None)

    fake_windows[7] = "Spotify - Web Player"
    assert apps.open_app("spotify") == "Spotify was already open; brought it to the front"
    assert focused == [7] and launched == []

    del fake_windows[7]
    real_popen = launched.append
    monkeypatch.setattr(apps.subprocess, "Popen", lambda cmd: real_popen(cmd) or fake_windows.update({8: "Spotify"}))
    assert apps.open_app("spotify") == "opened Spotify"  # returns once the window is up
    assert launched == [["explorer.exe", "shell:AppsFolder\\spotify-id"]]


# --- the orb --------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["starting", "sleeping", "listening", "thinking", "working", "speaking", "muted",
                                  "error"])
def test_orb_draws_every_state(mode):
    orb = OrbRenderer(80)
    for i in range(5):
        frame = orb.render(i / 30, mode, "#38bdf8", level=0.6, hover=i % 2 == 0, plan_total=3, plan_done=1)
    assert frame.size == (80, 80) and frame.mode == "RGBA"
    alpha = np.asarray(frame)[..., 3]
    assert alpha[0, 0] == 0 and alpha[40, 40] == 255  # see-through corners, solid middle


# --- memory: profile and relevant facts -----------------------------------------------------


def test_profile_and_relevant_memories_reach_the_model(tmp_path):
    (tmp_path / "profile.md").write_text("- Alex, AI & Data Science student in Pune", encoding="utf-8")
    store = Store(tmp_path / "naman.db")
    store.remember("Internship: Cognifyz Technologies, Data Scientist, Sep-Oct 2024")
    for i in range(30):  # plenty of newer, unrelated facts
        store.remember(f"Unrelated fact number {i}")
    assert store.relevant_memories("which internships did I do")[0]["fact"].startswith("Internship: Cognifyz")

    model = FakeChat(say("Cognifyz, as a Data Scientist."))
    Naman(store, model, InMemorySaver()).send("t", "which internships did I do?")
    system = model.seen[0][0]["content"]
    assert "AI & Data Science student in Pune" in system
    assert "Internship: Cognifyz" in system  # found by relevance, though 30 facts are newer
    assert "Unrelated fact number 0" not in system  # old and irrelevant: left out
