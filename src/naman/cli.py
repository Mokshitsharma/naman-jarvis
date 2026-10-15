"""`naman chat` on the laptop, `naman telegram` for the phone, plus quick views."""

from __future__ import annotations

import json

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .runtime import THREAD, make_naman, make_store

app = typer.Typer(add_completion=False, help="Naman, your personal AI assistant.")
console = Console()


@app.callback()
def main() -> None:
    load_dotenv()


@app.command()
def chat() -> None:
    """Talk to Naman in the terminal. Type 'exit' to leave."""
    with console.status("starting..."):
        naman = make_naman()
    try:
        _print_mcp_status(naman.mcp.status.values())
        naman.scheduler.add_listener(lambda text: console.print(f"\n[bold yellow]{text}[/]"))
        console.print("[bold]Naman[/] is listening. Type [dim]exit[/] to leave.\n")
        _chat_loop(naman)
    finally:
        naman.close()


def _print_mcp_status(statuses) -> None:
    for s in statuses:
        if s.ok:
            console.print(f"[green]mcp[/] {s.name}: {len(s.tools)} tools")
        else:
            console.print(f"[red]mcp[/] {s.name}: failed to start ({s.error})")


def _chat_loop(naman) -> None:
    from .llm import friendly_error

    pending = naman.pending(THREAD)
    while True:
        try:
            if pending:
                args = json.dumps(pending["args"], ensure_ascii=False)
                ok = typer.confirm(f"Allow {pending['tool']} {args}?", default=False)
                reply = naman.resume(THREAD, ok)
            else:
                try:
                    text = console.input("[bold cyan]you>[/] ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if text.lower() in {"exit", "quit", "bye"}:
                    break
                if not text:
                    continue
                with console.status("thinking..."):
                    reply = naman.send(THREAD, text)
        except Exception as exc:  # keep the session alive through API hiccups
            console.print(f"[red]naman>[/] {friendly_error(exc)}\n")
            pending = None
            continue
        pending = reply.approval
        if reply.text:
            console.print(f"[bold magenta]naman>[/] {reply.text}\n")


@app.command()
def telegram() -> None:
    """Run the Telegram bot so you can talk to Naman from your phone."""
    from .telegram_bot import run_bot

    run_bot()


@app.command("open")
def open_center() -> None:
    """Open the control center; starts Naman first if it isn't running (what the desktop shortcut runs)."""
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    from .browser import open_app_window
    from .runtime import data_dir

    link = data_dir() / "Control Center.url"

    def url() -> str:
        try:
            line = next(l for l in link.read_text(encoding="utf-8").splitlines() if l.startswith("URL="))
            return line[4:].strip()
        except (OSError, StopIteration):
            return ""

    def running() -> bool:
        address = url()
        if not address:
            return False
        try:
            urllib.request.urlopen(address.split("?")[0], timeout=2)
            return True
        except Exception:
            return False

    if not running():
        before = link.stat().st_mtime if link.exists() else 0
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-m", "naman", "start"],
                         cwd=str(Path.cwd()), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        for _ in range(90):  # the brain and the voice models take ~20-40 s to load
            time.sleep(1)
            if link.exists() and link.stat().st_mtime > before and running():
                break
    address = url()
    if address and not open_app_window(address + "#overview"):
        import webbrowser

        webbrowser.open(address + "#overview")


@app.command()
def shortcut() -> None:
    """Put a "Jarvis Control Center" shortcut on your desktop."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    from PIL import Image

    from .orb import OrbRenderer
    from .runtime import data_dir

    icon = (data_dir() / "naman.ico").resolve()
    frames = [OrbRenderer(256).render(t / 10, "sleeping", "#8b6cff") for t in range(12)]
    frames[-1].save(icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    desktop = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                             capture_output=True, text=True).stdout.strip() or str(Path.home() / "Desktop")
    lnk = Path(desktop) / "Jarvis Control Center.lnk"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK); $s.TargetPath = $env:TARGET; "
              "$s.Arguments = '-m naman open'; $s.WorkingDirectory = $env:WORKDIR; $s.IconLocation = $env:ICON; "
              "$s.Description = 'Open the Jarvis control center (starts Jarvis if needed)'; $s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env={
        **os.environ, "LNK": str(lnk), "TARGET": str(pythonw if pythonw.exists() else sys.executable),
        "WORKDIR": str(Path.cwd().resolve()), "ICON": str(icon)})
    console.print(f"Shortcut created: {lnk}")


@app.command("telegram-setup")
def telegram_setup(token: str = typer.Option("", help="The token @BotFather gave you (asked for if left out)")) -> None:
    """Connect your Telegram: checks the bot token, then saves your user id when you message the bot."""
    from pathlib import Path

    from .telegram_bot import setup

    token = token or typer.prompt("Paste the bot token from @BotFather", hide_input=True)
    setup(token.strip(), Path(".env"), say=console.print)


@app.command()
def control() -> None:
    """Open the control center (jobs, posts, tasks, schedule, builds, memory) without the orb."""
    import time

    from .automation import Automation
    from .control import ControlCenter
    from .runtime import data_dir

    naman = make_naman()
    naman.services.automation = Automation(naman.hub, data_dir(), naman.announce, log=console.print,
                                           outreach=naman.services.outreach)
    center = ControlCenter(naman, log=console.print, key_file=data_dir() / "control.key")
    naman.services.open_control_center = center.open
    center.start()
    center.open()
    console.print(f"Control center at {center.url}\nCtrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        naman.close()


@app.command("jobs-refresh")
def jobs_refresh() -> None:
    """Search for new jobs and hiring posts now, and print the best matches."""
    from .automation import Automation
    from .hub import Hub
    from .runtime import data_dir

    auto = Automation(Hub(make_store()), data_dir(), lambda text: None, log=console.print)
    result = auto.refresh_jobs(notify=False)
    for j in result["best"]:
        console.print(f"[bold]{j['fit']:>3}[/]  {j['title']} at {j['company']}  [dim]{j['url']}[/]")


@app.command()
def voice(
    ptt: bool = typer.Option(False, "--ptt", help="Push-to-talk: press Enter instead of saying the wake word"),
    voice_name: str = typer.Option("", "--voice", help="Edge TTS voice, e.g. en-IN-PrabhatNeural"),
    wake_model: str = typer.Option("tiny.en", help="Offline Whisper model for the wake phrase (tiny.en, base.en)"),
    device: int = typer.Option(-1, help="Microphone device index (see `naman mic-test`)"),
) -> None:
    """Talk to Naman in the terminal, out loud: say 'Jarvis', then your command."""
    import os

    from .voice import DEFAULT_VOICE, Microphone, PhraseWake, Speaker, Transcriber, VoiceLoop, wake_word

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    with console.status("starting..."):
        naman = make_naman()
        speaker = Speaker(voice_name or os.environ.get("NAMAN_VOICE") or DEFAULT_VOICE)
        wake = None if ptt else PhraseWake(wake_model)
        transcriber = Transcriber()
    try:
        _print_mcp_status(naman.mcp.status.values())
        with Microphone(None if device < 0 else device) as mic:
            loop = VoiceLoop(naman, THREAD, mic, speaker, transcriber, wake, log=console.print)
            naman.scheduler.add_listener(lambda text: (console.print(f"[bold yellow]{text}[/]"),
                                                        loop.inbox.put(("say", text))))
            console.print("Press Enter, then speak." if ptt else f'Listening. Say [bold]"{wake_word()}"[/], then your '
                          'command (or both in one breath). Say "goodbye" to stop.')
            speaker.speak("Naman online.")
            loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        naman.close()


@app.command()
def start(
    voice_name: str = typer.Option("", "--voice", help="Edge TTS voice, e.g. en-IN-PrabhatNeural"),
    device: int = typer.Option(-1, help="Microphone device index (see `naman mic-test`)"),
    greet: bool = typer.Option(True, help="Say hello and today's deadlines on start"),
) -> None:
    """Run Naman in the background with an always-on-top orb. Say 'Jarvis' any time."""
    from .overlay import run

    run(voice_name, None if device < 0 else device, greet)


@app.command()
def startup(action: str = typer.Argument("status", help="on, off or status")) -> None:
    """Start Naman automatically when you log in to Windows."""
    from .startup import disable, enable, is_enabled, shortcut_path

    if action == "on":
        console.print(f"Naman will start when you log in. Shortcut: {enable()}")
    elif action == "off":
        console.print("Removed from startup." if disable() else "Wasn't set to start with Windows.")
    else:
        console.print(f"Starts with Windows: {'yes' if is_enabled() else 'no'} ({shortcut_path()})")


@app.command("mic-test")
def mic_test(
    seconds: float = typer.Option(4.0, help="How long to record"),
    device: int = typer.Option(-1, help="Microphone device index"),
) -> None:
    """Check the microphone, speech-to-text and the voice, without the assistant."""
    import sounddevice as sd

    from .voice import SAMPLE_RATE, Speaker, Transcriber, rms, to_wav

    console.print("Input devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["hostapi"] == 0:
            console.print(f"  {i}: {d['name']}")
    console.print(f"\nRecording {seconds:.0f}s. Say something...")
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                   device=None if device < 0 else device)
    sd.wait()
    audio = audio[:, 0]
    level = rms(audio)
    console.print(f"Level: {level:.0f} (speech is usually above 500; below 100 means the mic isn't picking you up)")
    text = Transcriber().transcribe(to_wav(audio))
    console.print(f"Heard: [bold]{text or '(nothing)'}[/]")
    Speaker().speak(f"I heard: {text}" if text else "I didn't catch anything.")


@app.command()
def mcp() -> None:
    """Start every configured MCP server and show its tools and approval rules."""
    from .runtime import mcp_config_path, start_mcp

    path = mcp_config_path()
    if not path.exists():
        console.print(f"No {path} yet. Copy mcp_servers.example.json to {path} to get started.")
        return
    with console.status("connecting to MCP servers..."):
        manager = start_mcp()
    try:
        _print_mcp_status(manager.status.values())
        tools = manager.tools()
        table = Table(title=f"{len(tools)} MCP tools")
        table.add_column("tool")
        table.add_column("asks first")
        table.add_column("description")
        for t in tools:
            table.add_row(t.name, "yes" if t.needs_approval else "no", t.description[:90])
        console.print(table)
        if len(tools) > 40:
            console.print("[yellow]Many tools make every request bigger. Use a server's \"tools\" "
                          "allow-list to keep only the ones you need.[/]")
    finally:
        manager.close()


@app.command()
def tasks(project: str = typer.Option("", help="Only this project")) -> None:
    """Show open tasks."""
    rows = make_store().list_tasks(project or None)
    table = Table(title=f"Open tasks ({len(rows)})")
    for col in ("id", "priority", "project", "category", "due", "title"):
        table.add_column(col)
    for t in rows:
        table.add_row(str(t["id"]), t["priority"], t["project"], t["category"], t["due"] or "-", t["title"])
    console.print(table)


@app.command()
def memories() -> None:
    """Show what Naman remembers about you."""
    for m in make_store().recent_memories(100):
        console.print(f"[dim]{m['id']:>3}[/]  {m['fact']}")


if __name__ == "__main__":
    app()
