"""Talk to Naman out loud.

    "Jarvis" / "Hey Jarvis" (spotted locally: short bursts of speech go to a small offline Whisper)
      -> beep, record until you stop talking (energy-based endpointing)
      -> speech to text (Groq Whisper, free tier)
      -> Naman
      -> spoken reply (Microsoft Edge neural voices via edge-tts, free)

"Jarvis, open VS Code" in one breath works too: the command is taken from the
same burst. Confirmations are spoken: Naman asks, you say yes or no.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import os
import queue
import re
import threading
import wave
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from .llm import friendly_error

SAMPLE_RATE = 16_000
FRAME = 1280  # 80 ms
FRAME_S = FRAME / SAMPLE_RATE
DEFAULT_VOICE = "en-GB-RyanNeural"
STT_MODEL = "whisper-large-v3-turbo"
WAKE_MODEL = "tiny.en"  # offline, ~75 MB, downloaded once
# Spelling hints for Whisper: names it would otherwise mishear.
VOCAB_PROMPT = "Jarvis, Mario, Naman, VS Code, GitHub, YouTube, Spotify."


def vocab_prompt() -> str:
    """VOCAB_PROMPT plus your own names from NAMAN_VOCAB (projects, company, friends)."""
    extra = os.environ.get("NAMAN_VOCAB", "").strip().strip(",.")
    return f"{VOCAB_PROMPT[:-1]}, {extra}." if extra else VOCAB_PROMPT
# Whisper tends to invent these from silence or noise.
HALLUCINATIONS = {"", "you", "thank you", "thank you.", "thanks for watching!", "thanks for watching.",
                  ".", "bye.", "bye", "okay.", "so", "hmm"}
EXIT_PHRASES = {"goodbye", "good bye", "stop listening", "exit", "quit", "go to sleep"}
WAKE_WORD = "Jarvis"  # what you call Naman to wake it; NAMAN_WAKE_WORD in .env changes it
GREETINGS = {"oh", "hello", "hallo", "hullo", "helo", "hi", "hii", "hey", "hay", "namaste", "ok", "okay"}


def wake_word() -> str:
    return os.environ.get("NAMAN_WAKE_WORD") or WAKE_WORD


# --- pure helpers (unit-tested) ----------------------------------------------------------


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(frame.astype(np.float64) ** 2))) if len(frame) else 0.0


def loudness(level: float) -> float:
    """RMS of 16-bit audio -> 0..1 on a log scale (room noise ~0, normal speech ~0.5-0.8),
    for animating the orb."""
    import math

    return min(1.0, max(0.0, (math.log10(max(level, 1.0)) - 2.2) / 1.6))


def envelope(samples: np.ndarray, rate: int, fps: int = 60) -> np.ndarray:
    """Loudness per 1/fps s of audio, so the orb can move with Naman's voice."""
    step = max(1, rate // fps)
    n = len(samples) // step
    if n == 0:
        return np.zeros(0)
    frames = samples[: n * step].astype(np.float64).reshape(n, step)
    return np.array([loudness(v) for v in np.sqrt((frames ** 2).mean(axis=1))])


class Endpointer:
    """Decides when an utterance starts and ends from per-frame loudness."""

    MIN_SPEECH_RMS = 350.0

    def __init__(self, noise_floor: float, start_timeout_s: float = 6.0, silence_s: float = 0.9,
                 max_s: float = 20.0) -> None:
        self.threshold = speech_threshold(noise_floor)
        self.start_frames = int(start_timeout_s / FRAME_S)
        self.silence_frames = int(silence_s / FRAME_S)
        self.max_frames = int(max_s / FRAME_S)
        self.frames: list[np.ndarray] = []
        self.speech_frames = 0
        self.quiet_run = 0
        self.started = False

    def feed(self, frame: np.ndarray) -> str:
        """Returns "listening", "done" (got speech) or "timeout" (nobody spoke)."""
        self.frames.append(frame)
        loud = rms(frame) > self.threshold
        if loud:
            self.started = True
            self.speech_frames += 1
            self.quiet_run = 0
        elif self.started:
            self.quiet_run += 1
        if not self.started and len(self.frames) >= self.start_frames:
            return "timeout"
        if self.started and (self.quiet_run >= self.silence_frames or len(self.frames) >= self.max_frames):
            return "done" if self.speech_frames >= 3 else "timeout"  # 3 frames = 240 ms of speech
        return "listening"

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, np.int16)


def speech_threshold(noise_floor: float) -> float:
    return max(noise_floor * 3.0, Endpointer.MIN_SPEECH_RMS)


class Burst:
    """Cuts the always-on mic stream into short bursts of speech, for wake-phrase spotting.

    Bursts longer than MAX_S (TV, a long conversation) are dropped: a wake phrase is short."""

    MIN_LOUD = 4  # 320 ms of speech
    GAP_S, MAX_S, PRE_S = 0.5, 5.0, 0.25

    def __init__(self) -> None:
        self.pre: deque[np.ndarray] = deque(maxlen=int(self.PRE_S / FRAME_S))
        self.frames: list[np.ndarray] = []
        self.loud = self.quiet = 0
        self.overlong = False

    def feed(self, frame: np.ndarray, noise_floor: float) -> np.ndarray | None:
        """Returns a finished burst's audio, else None."""
        loud = rms(frame) > speech_threshold(noise_floor)
        if not self.frames:
            if not loud:
                self.pre.append(frame)
                return None
            self.frames = [*self.pre, frame]
            self.pre.clear()
            self.loud, self.quiet = 1, 0
            return None
        self.frames.append(frame)
        self.loud += loud
        self.quiet = 0 if loud else self.quiet + 1
        if len(self.frames) * FRAME_S > self.MAX_S:
            self.overlong = True
            self.frames = self.frames[-2:]  # keep going until a pause, then drop it all
        if self.quiet * FRAME_S < self.GAP_S:
            return None
        frames, enough, overlong = self.frames, self.loud >= self.MIN_LOUD, self.overlong
        self.frames, self.loud, self.quiet, self.overlong = [], 0, 0, False
        return np.concatenate(frames) if enough and not overlong else None


def _is_name(word: str, name: str) -> bool:
    """`word` is the wake word, allowing for how Whisper spells it ("Jervis", "Javis")."""
    word = word.removesuffix("'s")
    return word[:1] == name[:1] and difflib.SequenceMatcher(None, word, name).ratio() >= 0.8


def match_wake(text: str, name: str | None = None) -> str | None:
    """None unless `text` opens with the wake word ("Jarvis", "Hey Jarvis", "Hello Jarvis");
    otherwise what came after it."""
    name = (name or wake_word()).lower()
    tokens = list(re.finditer(r"[a-z']+", text.lower()))
    words = [t.group() for t in tokens]
    for i, word in enumerate(words[:3]):  # "jarvis", "hey jarvis", "oh hello jarvis"
        greeted = all(w in GREETINGS or (w.startswith("h") and len(w) <= 5) for w in words[:i])
        if greeted and _is_name(word, name):
            return text[tokens[i].end():].strip(" ,.!?;:-")  # the rest, as it was said
    return None


def parse_yes_no(text: str) -> bool | None:
    t = f" {re.sub(r'[^a-z ]', ' ', text.lower())} "
    no = re.search(r" (no|not|nope|nah|don t|do not|cancel|stop|nahi|mat) ", t)
    yes = re.search(r" (yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|please do|haan|han|ha|theek hai) ", t)
    if no:
        return False
    return True if yes else None


def clean_for_speech(text: str) -> str:
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"[*_`#>|]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)  # emoji
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str, min_chars: int = 40) -> list[str]:
    """Speakable pieces: whole sentences, with short ones joined so the voice doesn't sound choppy."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        current = f"{current} {sentence}".strip()
        if len(current) >= min_chars:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces


def echoes_prompt(text: str) -> bool:
    """Whisper sometimes "hears" its own spelling hints in silence: "GitHub, YouTube, Spotify, GitHub..."."""
    hints = {w.lower() for w in re.findall(r"[A-Za-z]+", vocab_prompt())}
    words = [w.lower() for w in re.findall(r"[A-Za-z]+", text)]
    from_hints = [w for w in words if w in hints]
    repeated = len(from_hints) != len(set(from_hints))
    return len(words) >= 3 and (repeated or len(from_hints) / len(words) >= 0.6)


def to_wav(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def describe_action(approval: dict[str, Any]) -> str:
    args = ", ".join(f"{k} {v}" for k, v in approval.get("args", {}).items())
    return f"{approval['tool'].replace('__', ' ').replace('_', ' ')}{' with ' + args if args else ''}"


# --- speech services -------------------------------------------------------------------


class Transcriber:
    def __init__(self) -> None:
        from groq import Groq

        self.client = Groq(max_retries=4)

    def transcribe(self, audio: bytes, filename: str = "speech.wav") -> str:
        # Left to guess, Whisper hears short phrases as Portuguese, Spanish or Hindi script.
        # English (the default) also writes Hinglish in Latin letters, which the model reads fine.
        # NAMAN_STT_LANGUAGE=auto lets it guess again; =hi for Hindi.
        language = os.environ.get("NAMAN_STT_LANGUAGE", "en").strip().lower()
        extra = {} if language in {"", "auto"} else {"language": language}
        result = self.client.audio.transcriptions.create(
            file=(filename, audio), model=STT_MODEL, prompt=vocab_prompt(), temperature=0.0, **extra
        )
        text = (result.text or "").strip()
        return "" if text.lower() in HALLUCINATIONS or echoes_prompt(text) else text


class Speaker:
    def __init__(self, voice: str = DEFAULT_VOICE) -> None:
        self.voice = voice
        self._env: np.ndarray = np.zeros(0)
        self._env_start = 0.0

    def level(self) -> float:
        """How loud the voice is right now, 0..1 (0 when silent)."""
        import time

        i = int((time.monotonic() - self._env_start) * 60)
        return float(self._env[i]) if 0 <= i < len(self._env) else 0.0

    def synth(self, text: str, rate: int = 24_000) -> np.ndarray:
        import edge_tts
        import miniaudio

        async def fetch() -> bytes:
            data = b""
            async for chunk in edge_tts.Communicate(text, self.voice).stream():
                if chunk["type"] == "audio":
                    data += chunk["data"]
            return data

        mp3 = asyncio.run(fetch())
        decoded = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate)
        return np.array(decoded.samples, dtype=np.int16)

    def speak(self, text: str) -> None:
        """Starts talking as soon as the first sentence is ready; the rest is fetched meanwhile."""
        import sounddevice as sd

        text = clean_for_speech(text)
        if not text:
            return
        ready: queue.Queue[np.ndarray | None] = queue.Queue()

        def fetch() -> None:
            for piece in split_sentences(text):
                try:
                    ready.put(self.synth(piece))
                except Exception as exc:  # offline or service hiccup: the text was shown anyway
                    print(f"(couldn't speak: {exc.__class__.__name__})")
                    break
            ready.put(None)

        threading.Thread(target=fetch, daemon=True, name="tts").start()
        import time

        while (audio := ready.get()) is not None:
            self._env, self._env_start = envelope(audio, 24_000), time.monotonic()
            sd.play(audio, 24_000)
            sd.wait()
        self._env = np.zeros(0)

    def beep(self, freq: float = 880.0, seconds: float = 0.12) -> None:
        import sounddevice as sd

        t = np.linspace(0, seconds, int(24_000 * seconds), endpoint=False)
        tone = (np.sin(2 * np.pi * freq * t) * np.hanning(len(t)) * 9000).astype(np.int16)
        sd.play(tone, 24_000)
        sd.wait()


# --- audio in --------------------------------------------------------------------------


class Microphone:
    def __init__(self, device: int | None = None) -> None:
        import sounddevice as sd

        self.frames: queue.Queue[np.ndarray] = queue.Queue()
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME, device=device,
            callback=lambda data, n, t, status: self.frames.put(data[:, 0].copy()),
        )

    def __enter__(self) -> Microphone:
        self.stream.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stream.stop()
        self.stream.close()

    def read(self) -> np.ndarray:
        return self.frames.get()

    def flush(self) -> None:
        """Drop audio captured while Naman was talking or thinking."""
        while not self.frames.empty():
            self.frames.get_nowait()


class PhraseWake:
    """Spots the wake word offline: each short burst of speech goes to a small local Whisper."""

    def __init__(self, model: str = WAKE_MODEL, name: str | None = None) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device="cpu", compute_type="int8")
        self.name = name or wake_word()
        self.burst = Burst()
        self.heard_text = ""  # what the wake model heard last time it fired
        self.rest = ""  # words said after the wake word in the same breath
        self.command_audio: np.ndarray | None = None  # that burst, for accurate transcription

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio.astype(np.float32) / 32768, language="en", beam_size=1, without_timestamps=True,
            condition_on_previous_text=False, hotwords=self.name,
        )
        return " ".join(s.text for s in segments).strip()

    def heard(self, frame: np.ndarray, noise_floor: float) -> bool:
        audio = self.burst.feed(frame, noise_floor)
        if audio is None:
            return False
        text = self.transcribe(audio)
        rest = match_wake(text, self.name)
        if rest is None:
            return False
        self.heard_text, self.rest = text, rest
        self.command_audio = audio if rest else None
        return True

    def reset(self) -> None:
        self.burst = Burst()


# --- the loop --------------------------------------------------------------------------


class VoiceLoop:
    """Wake -> command -> reply, over and over.

    Other threads (the on-screen widget, reminders) talk to the loop through `inbox`:
    ("talk", "") listens now without the wake phrase, ("type", text) runs a typed
    command, ("say", text) speaks an announcement. `muted` stops wake-phrase listening.
    `on_state(state, text)` reports sleeping / listening / thinking / heard / speaking / reply."""

    def __init__(self, naman: Any, thread: str, mic: Any, speaker: Any, transcriber: Any,
                 wake: Any | None = None, log: Callable[[str], None] = print,
                 wait_for_enter: Callable[[], None] | None = None,
                 on_state: Callable[[str, str], None] | None = None, stay_awake: bool = False,
                 follow_up_s: float = 0.0) -> None:
        self.naman, self.thread = naman, thread
        self.mic, self.speaker, self.transcriber, self.wake = mic, speaker, transcriber, wake
        self.log = log
        self.wait_for_enter = wait_for_enter or (lambda: input("\n[press Enter and speak] "))
        self.on_state = on_state or (lambda state, text: None)
        self.stay_awake = stay_awake  # "goodbye" goes back to sleep instead of quitting
        # After a spoken reply that asks something, listen this long for the answer
        # without the wake phrase. Only after questions: after "playing old songs" the
        # mic would hear the music.
        self.follow_up_s = follow_up_s
        self.mic_level = 0.0  # 0..1, for the orb
        self.inbox: queue.Queue[tuple[str, str]] = queue.Queue()
        self.muted = threading.Event()
        self.noise_floor = 150.0

    def _track_noise(self, frame: np.ndarray) -> None:
        # Speech must barely move the floor, or a long sentence would raise it above itself.
        level = rms(frame)
        rate = 0.05 if level < speech_threshold(self.noise_floor) else 0.002
        self.noise_floor = (1 - rate) * self.noise_floor + rate * level

    def _next_event(self) -> tuple[str, str]:
        """Blocks until the wake phrase, a push-to-talk, or something in the inbox."""
        if self.wake is None:
            try:
                return self.inbox.get_nowait()
            except queue.Empty:
                self.wait_for_enter()
                return ("talk", "")
        while True:
            try:
                return self.inbox.get_nowait()
            except queue.Empty:
                pass
            frame = self.mic.read()
            if self.muted.is_set():
                self.mic_level = 0.0
                continue
            self.mic_level = loudness(rms(frame))
            self._track_noise(frame)
            if self.wake.heard(frame, self.noise_floor):
                self.log(f"(woke on: {getattr(self.wake, 'heard_text', '') or 'wake phrase'})")
                return ("wake", self.wake.rest)

    def listen(self, start_timeout_s: float = 6.0) -> str:
        """Record one utterance and return its text ("" when nothing was said)."""
        self.mic.flush()
        ep = Endpointer(self.noise_floor, start_timeout_s=start_timeout_s)
        while True:
            frame = self.mic.read()
            self.mic_level = loudness(rms(frame))
            if (state := ep.feed(frame)) != "listening":
                break
        self.mic_level = 0.0
        if state == "timeout":
            return ""
        self.on_state("thinking", "")
        return self.transcriber.transcribe(to_wav(ep.audio()))

    def _say(self, text: str) -> None:
        self.log(f"naman> {text}")
        self.on_state("speaking", text)
        self.speaker.speak(text)
        self.mic.flush()
        if self.wake:
            self.wake.reset()

    def handle(self, text: str, spoken: bool = True) -> str:
        """Runs one command; returns the reply that was given ("" if none)."""
        self.on_state("thinking", "")
        try:
            reply = self.naman.send(self.thread, f"[voice] {text}" if spoken else text)
            while reply.approval:
                self._say(f"Should I {describe_action(reply.approval)}? Say yes or no.")
                self.on_state("listening", "")
                answer = parse_yes_no(self.listen())
                self.log(f"   -> {'yes' if answer else 'no'}")
                self.on_state("thinking", "")
                reply = self.naman.resume(self.thread, bool(answer))
        except Exception as exc:  # say what went wrong and keep listening
            self._say(friendly_error(exc))
            return ""
        if not reply.text:
            return ""
        if spoken:
            self._say(reply.text)
        else:  # typed: long answers are for reading, not for listening to
            self.log(f"naman> {reply.text}")
            self.on_state("reply", reply.text)
        return reply.text

    def follow_up(self, reply: str) -> None:
        """Naman asked something: take the answer without the wake phrase, as long as
        the conversation keeps going."""
        while self.follow_up_s and reply.rstrip().endswith("?"):
            self.on_state("listening", "")
            text = self.listen(start_timeout_s=self.follow_up_s)
            if not text:
                return
            self.log(f"you> {text}")
            self.on_state("heard", text)
            reply = self.handle(text)

    def _command_text(self, rest: str) -> str:
        """The command said together with the wake phrase, transcribed properly."""
        audio = getattr(self.wake, "command_audio", None)
        if audio is None:
            return rest
        self.on_state("thinking", "")
        try:
            return match_wake(self.transcriber.transcribe(to_wav(audio)), getattr(self.wake, "name", None)) or rest
        except Exception:
            return rest  # the offline transcript will do

    def run_once(self) -> bool:
        """One wake -> command -> reply cycle. Returns False when the user says goodbye."""
        self.on_state("muted" if self.muted.is_set() else "sleeping", "")
        kind, text = self._next_event()
        if kind == "say":
            self._say(text)
            return True
        if kind == "type":
            self.on_state("heard", text)
            self.handle(text, spoken=False)
            return True
        if kind == "wake" and text:
            text = self._command_text(text)
        else:
            self.speaker.beep()
            self.on_state("listening", "")
            text = self.listen()
        if not text:
            self.log("(didn't catch anything)")
            return True
        self.log(f"you> {text}")
        self.on_state("heard", text)
        if re.sub(r"[^a-z ]", "", text.lower()).strip() in EXIT_PHRASES:
            if self.stay_awake:
                self._say(f"Okay. Say {wake_word()} when you need me.")
                return True
            self._say("Going quiet. Talk soon.")
            return False
        self.follow_up(self.handle(text))
        return True

    def run(self) -> None:
        while self.run_once():
            pass
