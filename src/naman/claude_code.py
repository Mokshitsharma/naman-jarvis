"""Hand a coding job to Claude Code and let it work on its own in the background.

"Build a expense tracker in C:/code/expense-tracker" -> Naman asks you to confirm,
then runs `claude -p` (headless) in that folder. Claude Code makes the decisions itself
and reports what it did; Naman tells you when it's done (orb, Telegram) and keeps the
run in the control center.

What the run may do: read, create and edit files in the folder, and run the usual
build/test commands (npm, pip, python, pytest, git init/add/commit...). Anything else is
refused automatically, because nobody is there to approve it: no `git push`, no
deleting outside the tools, no commands outside the list below. Only folders under
the allowed roots can be used (not C:\\Windows, Program Files, a drive root, ...).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .hub import Hub

TIMEOUT_S = 60 * 60
ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite",
    *(f"Bash({cmd}:*)" for cmd in (
        "npm", "npx", "pnpm", "yarn", "node", "python", "py", "pip", "pytest", "uv", "flutter", "dart", "java",
        "javac", "mvn", "gradle", "go", "cargo", "mkdir", "ls", "dir", "cat", "echo", "cd", "pwd", "type",
        "git init", "git status", "git diff", "git add", "git commit", "git log", "git branch", "git checkout -b")),
]
DISALLOWED_TOOLS = ["Bash(git push:*)", "Bash(rm:*)", "Bash(del:*)", "Bash(rmdir:*)", "Bash(curl:*)",
                    "Bash(powershell:*)", "Bash(shutdown:*)"]
FORBIDDEN = [Path(p).resolve() for p in (os.environ.get("SystemRoot", r"C:\Windows"), r"C:\Program Files",
                                          r"C:\Program Files (x86)", r"C:\ProgramData")]

BUILD_PROMPT = """\
You are building software on your own for {who}. Nobody will answer questions during \
this run, so make sensible decisions yourself and note them in the README.

Task:
{instructions}

How to work:
- Work only inside the current folder.
- Prefer simple, well-known stacks; keep secrets out of code (use .env and write a .env.example).
- Settings that may change (prices, limits, thresholds) go in config, not in code.
- On an existing project, make additive changes with safe defaults; don't break what works.
- Write or update a README: what it is, how to run it, decisions you made.
- Run the build/tests you add and fix failures.
- Commit your work with git (init a repo if there is none). Never push.

Finish with a short report: what you built, how to run it, what's left or needs the user's input."""


def claude_path() -> str | None:
    return shutil.which("claude") or next(
        (str(p) for p in (Path.home() / ".local/bin/claude.exe", Path.home() / ".local/bin/claude") if p.exists()), None)


def check_folder(folder: str, create: bool) -> Path | str:
    path = Path(os.path.expandvars(os.path.expanduser(folder))).resolve()
    if path == Path(path.anchor) or path == Path.home().resolve():
        return f"error: {path} is too broad; use a project folder inside it"
    if any(path == f or f in path.parents for f in FORBIDDEN):
        return f"error: {path} is a system folder"
    if not path.exists():
        if not create:
            return f"error: {path} doesn't exist (say it's a new project to create it)"
        path.mkdir(parents=True)
    if not path.is_dir():
        return f"error: {path} is not a folder"
    return path


class Builder:
    def __init__(self, hub: Hub, log_dir: Path, announce: Callable[[str], None]) -> None:
        self.hub, self.log_dir, self.announce = hub, log_dir, announce
        self.running: dict[int, subprocess.Popen] = {}

    def start(self, folder: str, instructions: str, new_project: bool = False) -> str:
        exe = claude_path()
        if exe is None:
            return "error: Claude Code isn't installed (no `claude` command)"
        if self.running:
            busy = next(iter(self.running))
            return f"error: build #{busy} is still running; wait for it or check its status"
        path = check_folder(folder, new_project)
        if isinstance(path, str):
            return path
        run_id = self.hub.add_run(str(path), instructions.strip())
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"run-{run_id}.json"
        self.hub.set_run_log(run_id, str(log_path))
        who = os.environ.get("NAMAN_USER_NAME") or "the user"
        cmd = [exe, "-p", BUILD_PROMPT.format(who=who, instructions=instructions.strip()), "--output-format", "json",
               "--permission-mode", "acceptEdits", "--allowedTools", *ALLOWED_TOOLS,
               "--disallowedTools", *DISALLOWED_TOOLS]
        log = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=path, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.running[run_id] = proc
        threading.Thread(target=self._wait, args=(run_id, proc, log, log_path), daemon=True,
                         name=f"claude-{run_id}").start()
        return f"started build #{run_id} in {path}; Claude Code is working on it and I'll tell you when it's done"

    def _wait(self, run_id: int, proc: subprocess.Popen, log, log_path: Path) -> None:
        try:
            proc.wait(timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        finally:
            log.close()
            self.running.pop(run_id, None)
        ok, summary = parse_result(log_path.read_text(encoding="utf-8", errors="replace"), proc.returncode)
        self.hub.finish_run(run_id, ok, summary)
        headline = summary.strip().split("\n")[0][:300]
        self.announce(f"Claude Code build #{run_id} {'finished' if ok else 'failed'}: {headline}")

    def status(self) -> list[dict]:
        return [{k: r[k] for k in ("id", "project_dir", "status", "started", "finished")} |
                {"instructions": r["instructions"][:120], "summary": r["summary"][:600]} for r in self.hub.runs(5)]

    def stop_all(self) -> None:
        for proc in list(self.running.values()):
            proc.kill()


def parse_result(output: str, returncode: int | None) -> tuple[bool, str]:
    """`claude -p --output-format json` prints one JSON object with `result` and `is_error`."""
    start = output.rfind('{"type":"result"')
    candidates = [output[start:]] if start >= 0 else []
    candidates.append(output.strip().splitlines()[-1] if output.strip() else "")
    for text in candidates:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and "result" in data:
            return (not data.get("is_error") and returncode == 0), str(data.get("result") or "")
    tail = output.strip()[-600:]
    return False, f"no result (exit code {returncode}). Last output: {tail}" if tail else f"exit code {returncode}"
