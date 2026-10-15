import numpy as np

from naman.agent import Reply
from naman.apps import App, find_app
from naman.voice import FRAME, Endpointer, VoiceLoop, clean_for_speech, describe_action, parse_yes_no

APPS = (
    App("Visual Studio Code", "Microsoft.VisualStudioCode"),
    App("CodeBlocks", "cb"),
    App("Google Chrome", "Chrome"),
    App("WordPad", "wordpad"),
    App("Microsoft Office Word 2007", "word"),
    App("Settings", "settings"),
    App("WSL Settings", "wsl"),
    App("Calculator", "calc"),
)


def test_apps_are_found_by_spoken_name():
    cases = {
        "VS Code": "Visual Studio Code",
        "open vs code please": "Visual Studio Code",
        "code": "Visual Studio Code",
        "chrome": "Google Chrome",
        "word": "Microsoft Office Word 2007",  # whole word beats "WordPad"
        "wordpad": "WordPad",
        "settings": "Settings",  # exact beats "WSL Settings"
        "calc": "Calculator",
        "calculater": "Calculator",  # typo from speech-to-text
    }
    for said, expected in cases.items():
        assert find_app(said, APPS).name == expected, said
    assert find_app("photoshop", APPS) is None


def loud(n=1):
    return [np.full(FRAME, 3000, np.int16) for _ in range(n)]


def quiet(n=1):
    return [np.zeros(FRAME, np.int16) for _ in range(n)]


def run(ep, frames):
    state = "listening"
    for f in frames:
        state = ep.feed(f)
        if state != "listening":
            break
    return state


def test_endpointer_waits_for_speech_then_silence():
    ep = Endpointer(noise_floor=100)
    assert run(ep, quiet(5) + loud(10) + quiet(20)) == "done"
    assert len(ep.audio()) == FRAME * (5 + 10 + 11)  # stops 11 quiet frames (~0.9 s) after speech


def test_endpointer_times_out_and_ignores_clicks():
    assert run(Endpointer(100, start_timeout_s=1.0), quiet(30)) == "timeout"
    assert run(Endpointer(100), quiet(2) + loud(1) + quiet(20)) == "timeout"  # an 80 ms click isn't speech


def test_yes_no_and_speech_cleanup():
    assert parse_yes_no("Yes, go ahead.") is True
    assert parse_yes_no("haan kar do") is True
    assert parse_yes_no("No, don't.") is False
    assert parse_yes_no("not okay") is False
    assert parse_yes_no("what?") is None
    assert clean_for_speech("**Done!** See https://x.com 🎉\n- item") == "Done! See a link item"
    assert describe_action({"tool": "files__write_file", "args": {"path": "a.txt"}}) == "files write file with path a.txt"


class FakeMic:
    def __init__(self, frames):
        self.frames = list(frames)

    def read(self):
        return self.frames.pop(0) if self.frames else np.zeros(FRAME, np.int16)

    def flush(self):
        pass


class Recorder:
    def __init__(self):
        self.spoken, self.beeps = [], 0

    def speak(self, text):
        self.spoken.append(text)

    def beep(self):
        self.beeps += 1


class ScriptedEars:
    def __init__(self, *texts):
        self.texts = list(texts)

    def transcribe(self, audio, filename="speech.wav"):
        return self.texts.pop(0)


class FakeNaman:
    def __init__(self):
        self.sent, self.resumed = [], []

    def send(self, thread, text):
        self.sent.append(text)
        return Reply(approval={"tool": "files__write_file", "args": {"path": "note.txt"}})

    def resume(self, thread, approved):
        self.resumed.append(approved)
        return Reply(text="Saved it." if approved else "Okay, left it.")


def utterance():
    return loud(8) + quiet(15)


def make_loop(naman, speaker, ears, frames):
    return VoiceLoop(naman, "t", FakeMic(frames), speaker, ears, wake=None, log=lambda s: None,
                     wait_for_enter=lambda: None)


def test_voice_command_with_spoken_confirmation():
    naman, speaker = FakeNaman(), Recorder()
    loop = make_loop(naman, speaker, ScriptedEars("save a note", "yes please"), utterance() + utterance())
    assert loop.run_once() is True
    assert naman.sent == ["[voice] save a note"]  # marked so replies come back short and speakable
    assert naman.resumed == [True]
    assert speaker.spoken == ["Should I files write file with path note.txt? Say yes or no.", "Saved it."]
    assert speaker.beeps == 1


def test_unclear_confirmation_counts_as_no_and_goodbye_stops():
    naman, speaker = FakeNaman(), Recorder()
    loop = make_loop(naman, speaker, ScriptedEars("save a note", "umm what", "Goodbye."), utterance() * 3)
    assert loop.run_once() is True
    assert naman.resumed == [False] and speaker.spoken[-1] == "Okay, left it."
    assert loop.run_once() is False


def test_errors_are_spoken_and_listening_continues():
    class Broken(FakeNaman):
        def send(self, thread, text):
            raise ConnectionError("offline")

    speaker = Recorder()
    loop = make_loop(Broken(), speaker, ScriptedEars("open vs code"), utterance())
    assert loop.run_once() is True
    assert speaker.spoken == ["Something went wrong (ConnectionError). Try again."]


# --- "Hello Naman", the always-on loop and the on-screen widget ------------------------

from datetime import datetime  # noqa: E402

from naman.overlay import greeting  # noqa: E402
from naman.voice import Burst, match_wake  # noqa: E402


def test_wake_word_is_spotted_with_or_without_a_command():
    for said in ["Jarvis", "Jarvis.", "Hey Jarvis", "Hello, Jarvis!", "Hi Jarvis", "Oh hey Jarvis", "Hey Jervis",
                 "Hey Javis"]:
        assert match_wake(said, "Jarvis") == "", said
    assert match_wake("Jarvis, open VS Code.", "Jarvis") == "open VS Code"
    assert match_wake("Hey Jarvis open YouTube", "Jarvis") == "open YouTube"
    for other in ["Hello Naman", "Hey Travis", "Davis is here", "I told Jarvis about it", "Hello everyone",
                  "The service is down", "Open the door", ""]:
        assert match_wake(other, "Jarvis") is None, other


def test_wake_word_is_configurable(monkeypatch):
    monkeypatch.setenv("NAMAN_WAKE_WORD", "Naman")
    assert match_wake("Hello Naman, open Chrome") == "open Chrome"
    assert match_wake("Hey Jarvis") is None


def test_bursts_are_short_speech_between_pauses():
    burst, floor = Burst(), 100.0
    out = [burst.feed(f, floor) for f in quiet(5) + loud(10) + quiet(8)]
    audio = [a for a in out if a is not None]
    assert len(audio) == 1 and len(audio[0]) >= FRAME * 10  # includes a little lead-in
    assert all(burst.feed(f, floor) is None for f in loud(2) + quiet(10))  # a click is not speech
    long_talk = [burst.feed(f, floor) for f in loud(80) + quiet(10)]  # 6.4 s: TV, not a wake phrase
    assert all(a is None for a in long_talk)


class ScriptedWake:
    """Fires once per scripted entry: (rest of the sentence, command audio or None)."""

    def __init__(self, *hits):
        self.hits, self.rest, self.command_audio = list(hits), "", None

    def heard(self, frame, noise_floor):
        if not self.hits:
            raise AssertionError("no more wakes scripted")
        self.rest, self.command_audio = self.hits.pop(0)
        return True

    def reset(self):
        pass


class Answering(FakeNaman):
    def send(self, thread, text):
        self.sent.append(text)
        return Reply(text=f"done: {text}")


def test_command_in_the_same_breath_skips_the_beep():
    naman, speaker, states = Answering(), Recorder(), []
    wake = ScriptedWake(("open vs code", np.zeros(FRAME, np.int16)))
    loop = VoiceLoop(naman, "t", FakeMic(quiet(3)), speaker, ScriptedEars("Jarvis, open VS Code."), wake,
                     log=lambda s: None, on_state=lambda s, t: states.append(s))
    assert loop.run_once() is True
    assert naman.sent == ["[voice] open VS Code"]  # the accurate transcript, wake phrase removed
    assert speaker.beeps == 0
    assert states[0] == "sleeping" and "speaking" in states


def test_always_on_loop_stays_up_after_goodbye_and_takes_typed_commands():
    naman, speaker, states = Answering(), Recorder(), []
    loop = VoiceLoop(naman, "t", FakeMic(utterance()), speaker, ScriptedEars("Goodbye."), ScriptedWake(("", None)),
                     log=lambda s: None, on_state=lambda s, t: states.append((s, t)), stay_awake=True)
    assert loop.run_once() is True  # goodbye does not quit
    assert speaker.beeps == 1 and naman.sent == []
    loop.inbox.put(("type", "what's on today?"))
    loop.inbox.put(("say", "Reminder: stretch"))
    loop.run_once()
    loop.run_once()
    assert naman.sent == ["what's on today?"]  # typed: no [voice] marker, so replies can be longer
    assert ("reply", "done: what's on today?") in states
    assert speaker.spoken[-1] == "Reminder: stretch"


def test_greeting_mentions_todays_deadlines(monkeypatch):
    monkeypatch.setenv("NAMAN_WAKE_WORD", "Mario")  # it introduces itself by the name you call it
    morning = datetime(2026, 9, 30, 9, 0)
    assert greeting("Alex", {"due_today": [], "overdue": []}, morning) == "Good morning, Alex. Mario is online."
    assert greeting("Alex", {"due_today": [1, 2], "overdue": [1]}, datetime(2026, 9, 30, 19, 0)) == (
        "Good evening, Alex. Mario is online. You have 2 tasks due today and 1 task overdue.")


def test_startup_shortcut_points_at_the_project():
    from naman import startup

    assert (startup.project_dir() / "pyproject.toml").exists()
    assert startup.windowless_python().name.lower() in {"pythonw.exe", "python.exe", "python"}


def test_replies_are_spoken_sentence_by_sentence():
    from naman.voice import split_sentences

    assert split_sentences("VS Code opened. Anything else?") == ["VS Code opened. Anything else?"]
    long = ("Jaipur is clear and about 33 degrees today. It should stay dry through the evening. "
            "Take water if you head out.")
    assert split_sentences(long) == ["Jaipur is clear and about 33 degrees today.",
                                     "It should stay dry through the evening. Take water if you head out."]


def test_whisper_echoing_its_spelling_hints_is_ignored():
    from naman.voice import echoes_prompt

    assert echoes_prompt("Kool, GitHub, YouTube, Sĩ, Spotify, GitHub, YouTube, Sventy, Spotify, GitHub, YouTube.")
    assert echoes_prompt("VS Code, Jarvis, Naman.")
    assert not echoes_prompt("open GitHub and YouTube")
    assert not echoes_prompt("can you open VS Code for me")
    assert not echoes_prompt("Jarvis")
