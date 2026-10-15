"""Naman on screen: a small always-on-top orb that listens for "Jarvis".

The orb's colour shows what Naman is doing (teal: waiting for "Jarvis", blue:
listening, amber: thinking, violet: carrying out steps, green: speaking, grey: mic
off). It moves with your voice and Naman's, and a multi-step command splits it into
one dot per step (see orb.py). What you said, the plan and the reply slide out
beside it and slide back in a few seconds after it's done, like a phone notification.
News from the background (new jobs, GitHub, open tasks) comes the same way, with a
little hop.

    click the orb        talk without saying the wake phrase
    click the message    close it now
    right-click > Help me fill this form
                         reads the form on screen; a panel lists each field with
                         an answer and a Copy button (see formfill.py)
    drag                 move it (the spot is remembered)
    double-click         type a command
    right-click          menu: talk, type, control center, mic on/off, start with Windows, quit

The voice loop runs on a worker thread; it reports state through a queue that the
Tk main loop drains, so the UI never blocks on the network or the microphone.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from .voice import wake_word

STATES = {  # state -> (orb colour, caption)
    "starting": ("#94a3b8", "waking up..."),
    "sleeping": ("#2dd4bf", "say {wake}"),
    "listening": ("#38bdf8", "listening..."),
    "heard": ("#fbbf24", "thinking..."),
    "thinking": ("#fbbf24", "thinking..."),
    "working": ("#a78bfa", "on it"),
    "speaking": ("#34d399", "speaking"),
    "reply": ("#34d399", "reply"),
    "muted": ("#64748b", "mic off"),
    "error": ("#f87171", "something's wrong"),
}
KEY = "#010203"  # painted pixels in this colour are see-through
PANEL, TEXT, DIM = "#0f172a", "#e2e8f0", "#94a3b8"
NEWS = "#f472b6"  # the caption colour of a notification
SIZE = 100  # collapsed: just the orb
WIDTH = 410  # with text
MAX_REPLY_CHARS = 260
ACTIVE_MS, IDLE_MS = 33, 66  # frame interval: ~30 fps while busy, ~15 fps while waiting
BUSY = {"heard", "thinking", "working", "speaking"}  # the message stays out while these last
SLIDE_S = 0.28  # how long the message takes to slide out (and a bit less to go back)


def read_time(text: str, notification: bool = False) -> float:
    """How long a message stays out once it's finished: enough to read it, never long."""
    return min(10.0 if notification else 12.0, (4.5 if notification else 3.5) + len(text) / 22)


def greeting(name: str, briefing: dict[str, Any], now: datetime | None = None) -> str:
    """What Naman says when it starts: a greeting plus today's deadlines."""
    hour = (now or datetime.now()).hour
    part = "morning" if hour < 12 else "afternoon" if hour < 17 else "evening"
    who = f", {name}" if name and name != "the user" else ""
    text = f"Good {part}{who}. {wake_word()} is online."
    due, late = len(briefing.get("due_today", [])), len(briefing.get("overdue", []))
    bits = [f"{n} task{'s' * (n != 1)} {what}" for n, what in ((due, "due today"), (late, "overdue")) if n]
    if bits:
        text += " You have " + " and ".join(bits) + "."
    return text


def _work_area() -> tuple[int, int] | None:
    """Right and bottom edge of the screen minus the taskbar (Windows)."""
    try:
        class Rect(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]

        rect = Rect()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.r, rect.b
    except Exception:
        return None


class Widget:
    def __init__(self, events: queue.Queue, controller: Any, pos_file: Path) -> None:
        import tkinter as tk

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
        except Exception:
            pass
        self.tk, self.events, self.ctl, self.pos_file = tk, events, controller, pos_file
        self.root = root = tk.Tk()
        root.title("Naman")
        root.overrideredirect(True)  # no title bar, no taskbar button
        root.attributes("-topmost", True)
        root.attributes("-alpha", 1.0)  # fully opaque: at 0.95 whatever is behind shows through
        try:
            root.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.s = root.winfo_fpixels("1i") / 96
        root.configure(bg=KEY)  # anything not painted yet is see-through, never a white strip
        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.caption_font = ("Segoe UI Semibold", 9)
        self.you_font = ("Segoe UI", 9, "italic")
        self.body_font = ("Segoe UI", 10)
        self.step_font = ("Segoe UI", 9)

        self.state, self.you, self.said = "starting", "", ""
        self.plan: list[str] = []  # steps of the current multi-step command
        self.plan_done = 0  # how many of them have finished
        self.hover = False
        self.show_until = 0.0
        self.news = False  # the message showing is a notification, not a reply
        self.pending: list[str] = []  # notifications waiting for a conversation to finish
        self.reveal = 0.0  # 0: just the orb, 1: the message fully out
        self.full = (0, 0)  # window size with the message out
        from PIL import Image, ImageTk

        from .orb import OrbRenderer

        orb_px = int(SIZE * self.s)
        self.renderer = OrbRenderer(orb_px)
        self.photo = ImageTk.PhotoImage(Image.new("RGBA", (orb_px, orb_px)))  # reused every frame
        self.size = (0, 0)
        self.is_expanded = False
        self.anchor = self._load_anchor()  # bottom-right corner, so text grows left and up
        self._drag: tuple[int, int, int, int] | None = None
        self._moved = False

        self.muted = tk.BooleanVar(value=False)
        self.on_startup = tk.BooleanVar(value=controller.startup_enabled())
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Talk now", command=controller.talk)
        self.menu.add_command(label="Type a command...", command=self.ask_text)
        self.menu.add_command(label="Control center", command=controller.open_center)
        self.menu.add_command(label="Help me fill this form", command=controller.fill_form)
        self.menu.add_checkbutton(label="Mic off", variable=self.muted, command=self._toggle_mute)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="Start with Windows", variable=self.on_startup,
                                  command=lambda: controller.set_startup(self.on_startup.get()))
        self.menu.add_separator()
        self.menu.add_command(label="Quit Naman", command=self.quit)

        c = self.canvas
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._motion)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<Double-Button-1>", lambda e: self.ask_text())
        c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        c.bind("<Enter>", lambda e: setattr(self, "hover", True))
        c.bind("<Leave>", lambda e: setattr(self, "hover", False))
        self._layout()
        self._poll()
        self._animate()
        self._stay_on_top()

    # --- geometry -----------------------------------------------------------------------

    def _load_anchor(self) -> tuple[int, int]:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        right, bottom = _work_area() or (sw, sh - int(48 * self.s))
        default = (right - int(24 * self.s), bottom - int(24 * self.s))
        try:
            x, y = json.loads(self.pos_file.read_text())["anchor"]
        except Exception:
            return default
        size = int(SIZE * self.s)
        if size <= x <= sw and size <= y <= sh:  # still on screen (monitors change)
            return x, y
        return default

    def _save_anchor(self) -> None:
        try:
            self.pos_file.write_text(json.dumps({"anchor": list(self.anchor)}))
        except OSError:
            pass

    def _expanded(self) -> bool:
        return bool(self.you or self.said) and (time.time() < self.show_until or self.state in BUSY)

    def _layout(self) -> None:
        """Lay out the message (if any) at full size and redraw the static parts. The window
        itself is sized by _place, which slides the message out from behind the orb."""
        s, c = self.s, self.canvas
        c.delete("all")
        size = int(SIZE * s)
        expanded = self.is_expanded = self._expanded()
        width = int(WIDTH * s) if expanded else size
        pad, text_w = int(16 * s), int(WIDTH * s) - size - int(20 * s)
        height = size
        if expanded:  # measure the text first, then draw the panel behind it
            y = int(12 * s)
            colour, caption = (NEWS, "new update") if self.news else STATES.get(self.state, STATES["sleeping"])
            caption = caption.format(wake=f'"{wake_word()}"')
            items = [c.create_text(pad, y, anchor="nw", text=wake_word().upper(), fill=TEXT, font=self.caption_font)]
            x = c.bbox(items[-1])[2] + int(8 * s)
            items.append(c.create_text(x, y, anchor="nw", text=f"●  {caption}", fill=colour, font=self.caption_font))
            y = c.bbox(items[-1])[3] + int(6 * s)
            if self.you:
                items.append(c.create_text(pad, y, anchor="nw", width=text_w, fill=DIM, font=self.you_font,
                                           text=f"“{self.you}”"))
                y = c.bbox(items[-1])[3] + int(3 * s)
            for i, step in enumerate(self.plan):  # the plan as a checklist
                mark, fill = ("✓", DIM) if i < self.plan_done else ("›", colour) if i == self.plan_done else ("·", DIM)
                items.append(c.create_text(pad, y, anchor="nw", width=text_w, fill=fill, font=self.step_font,
                                           text=f"{mark}  {step}"))
                y = c.bbox(items[-1])[3] + int(1 * s)
            if self.said:
                said = self.said if len(self.said) <= MAX_REPLY_CHARS else self.said[:MAX_REPLY_CHARS] + "..."
                items.append(c.create_text(pad, y, anchor="nw", width=text_w, fill=TEXT, font=self.body_font,
                                           text=said))
                y = c.bbox(items[-1])[3]
            height = max(size, y + int(14 * s))
            self._panel(width, height, int(20 * s), colour)
            for item in items:
                c.tag_raise(item)
        self.orb_center = (width - size // 2, height - size // 2)
        c.create_image(*self.orb_center, image=self.photo, tags="orb")
        c.configure(scrollregion=(0, 0, width, height))
        self.full = (width, height)
        self._place()

    def _place(self) -> None:
        """Size the window for how far the message is out. The canvas scrolls with it, so
        the orb stays put and the panel looks like it slides out from behind it."""
        size = int(SIZE * self.s)
        full_w, full_h = self.full
        k = 1 - (1 - self.reveal) ** 3  # ease out
        width = max(size, round(size + (full_w - size) * k)) if self.is_expanded else size
        height = full_h if self.is_expanded else size
        if (width, height) != self.size:
            self.size = (width, height)
            x, y = self.anchor
            self.canvas.configure(width=width, height=height)  # resized and scrolled in one go: no strips
            self.canvas.xview_moveto((full_w - width) / full_w if full_w else 0)
            self.root.geometry(f"{width}x{height}+{x - width}+{y - height}")

    def _panel(self, w: int, h: int, radius: int, colour: str) -> None:
        from PIL import ImageTk

        from .orb import panel_image

        self.panel_photo = ImageTk.PhotoImage(panel_image(w, h, min(radius, h // 2), colour))  # keep a reference
        self.canvas.create_image(0, 0, anchor="nw", image=self.panel_photo, tags="panel")

    # --- drawing ------------------------------------------------------------------------

    def _animate(self) -> None:
        colour = STATES.get(self.state, STATES["sleeping"])[0]
        total = len(self.plan)
        frame = self.renderer.render(time.monotonic(), self.state, colour, self.ctl.level(self.state), self.hover,
                                     total, self.plan_done)
        self.photo.paste(frame)
        want = self._expanded()
        if want and not self.is_expanded:
            self.reveal = 0.0
            self._layout()
        step = (ACTIVE_MS / 1000) / SLIDE_S
        sliding = (want and self.reveal < 1) or (not want and self.is_expanded)
        if want and self.reveal < 1:
            self.reveal = min(1.0, self.reveal + step)
            self._place()
        elif not want and self.is_expanded:
            self.reveal = max(0.0, self.reveal - step * 1.4)
            if self.reveal == 0:  # fully back in: the message is over
                self.you = self.said = ""
                self.plan, self.plan_done = [], 0
                self.news = False
                self._layout()
            else:
                self._place()
        if not want and not self.is_expanded and self.pending and self.state not in BUSY:
            self._notify(self.pending.pop(0))
        idle = self.state in {"sleeping", "muted"} and not self.hover and not self.plan and not sliding
        self.root.after(IDLE_MS if idle else ACTIVE_MS, self._animate)

    def _notify(self, text: str) -> None:
        """News from the background: slides out with a hop, goes away on its own."""
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        self.you, self.said, self.news = "", "\n".join(lines[:4]), True
        self.show_until = time.time() + read_time(self.said, notification=True)
        self.renderer.cheer(time.monotonic())
        self._layout()

    # --- events from the voice thread ---------------------------------------------------

    def _poll(self) -> None:
        changed = False
        was_busy = self.state in BUSY
        try:
            while True:
                state, text = self.events.get_nowait()
                if state == "form":  # "help me fill this form": scan, then the panel
                    self.form_scan()
                    continue
                if state == "form_result":
                    self.form_show(json.loads(text))
                    continue
                if state == "notify":  # waits its turn if you're talking to it
                    if self.state in BUSY or (self.is_expanded and not self.news):
                        self.pending.append(text)
                    else:
                        self._notify(text)
                    continue
                changed = True
                self.news = False
                if state == "plan":
                    self.plan, self.plan_done = json.loads(text), 0
                    continue
                if state == "step":
                    step = json.loads(text)
                    if self.plan:
                        self.plan_done = max(self.plan_done, min(step["index"], len(self.plan)) - 1)
                    self.said = step["text"] + "..."
                    self.state = "working"
                    continue
                if state == "heard":
                    self.you, self.said = text, ""
                    self.plan, self.plan_done = [], 0
                if state in {"speaking", "reply"} and self.plan:
                    self.plan_done = len(self.plan)  # the reply means every step has run
                if state in {"speaking", "reply", "error"} and text:
                    self.said = text
                    if state == "error":
                        self.you = ""
                if text:
                    self.show_until = time.time() + read_time(text)
                if state == "muted" and not self.muted.get():
                    continue  # stale: the mic was switched back on
                self.state = state
        except queue.Empty:
            pass
        if was_busy and self.state not in BUSY and self.said:  # finished: time to read it, then away
            self.show_until = max(self.show_until, time.time() + read_time(self.said))
        if changed and (self._expanded() or self.is_expanded):
            self._layout()
        self.root.after(60, self._poll)

    def _stay_on_top(self) -> None:
        self.root.attributes("-topmost", True)  # other always-on-top windows can bury it
        self.root.lift()
        self.root.after(2000, self._stay_on_top)

    # --- mouse --------------------------------------------------------------------------

    def _press(self, e: Any) -> None:
        self._drag = (e.x_root, e.y_root, *self.anchor)
        self._moved = False

    def _motion(self, e: Any) -> None:
        if not self._drag:
            return
        x0, y0, ax, ay = self._drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        if self._moved:
            self.anchor = (ax + dx, ay + dy)
            w, h = self.size
            self.root.geometry(f"+{self.anchor[0] - w}+{self.anchor[1] - h}")

    def _release(self, e: Any) -> None:
        if self._moved:
            self._save_anchor()
        else:
            cx, cy = self.orb_center
            if math.hypot(self.canvas.canvasx(e.x) - cx, e.y - cy) <= SIZE * self.s / 2:
                self.ctl.talk()
            elif self.is_expanded:  # a click on the message closes it, like a notification
                self.show_until = 0.0
                if self.state not in BUSY:
                    self.state = "muted" if self.muted.get() else "sleeping"
        self._drag = None

    def _toggle_mute(self) -> None:
        self.ctl.set_muted(self.muted.get())
        self.state = "muted" if self.muted.get() else "sleeping"
        self._layout()

    def ask_text(self) -> None:
        tk = self.tk
        top = tk.Toplevel(self.root)
        top.title("Tell Naman")
        top.attributes("-topmost", True)
        top.attributes("-toolwindow", True)
        top.configure(bg=PANEL)
        entry = tk.Entry(top, width=44, font=self.body_font, bg="#1e293b", fg=TEXT, insertbackground=TEXT,
                         relief="flat")
        entry.pack(padx=10, pady=10, ipady=4)
        w, h = self.size
        top.geometry(f"+{max(0, self.anchor[0] - int(WIDTH * self.s))}+{max(0, self.anchor[1] - h - int(80 * self.s))}")

        def submit(_: Any = None) -> None:
            text = entry.get().strip()
            top.destroy()
            if text:
                self.ctl.type(text)

        entry.bind("<Return>", submit)
        entry.bind("<Escape>", lambda _: top.destroy())
        top.after(50, entry.focus_force)

    # --- form helper ----------------------------------------------------------------------

    def form_scan(self) -> None:
        """Hide the panel (so it isn't in the screenshot), then read the form off-thread."""
        if getattr(self, "form_win", None) is not None and self.form_win.winfo_exists():
            self.form_win.withdraw()
        self.state, self.you, self.said, self.news = "thinking", "", "Reading the form on your screen...", False
        self.show_until = time.time() + 30
        self._layout()
        self.root.after(350, lambda: threading.Thread(target=self.ctl.scan_form, daemon=True).start())

    def form_show(self, result: dict[str, Any]) -> None:
        tk = self.tk
        self.state, self.show_until = "sleeping", 0.0
        if result.get("error"):
            self.events.put(("error", result["error"]))
            return
        fields = result.get("fields") or []
        if not fields:
            self.events.put(("reply", "I couldn't find a form on your screen. Open it and try again."))
            return
        win = getattr(self, "form_win", None)
        if win is None or not win.winfo_exists():
            win = self.form_win = tk.Toplevel(self.root)
            win.title("Form helper")
            win.attributes("-topmost", True)
            win.configure(bg=PANEL)
            w, h = int(440 * self.s), int(640 * self.s)
            x = max(0, self.anchor[0] - w - int(20 * self.s))
            win.geometry(f"{w}x{h}+{x}+{max(0, self.anchor[1] - h)}")
        for child in win.winfo_children():
            child.destroy()
        win.deiconify()
        known = sum(f["status"] != "unknown" for f in fields)

        head = tk.Frame(win, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(head, text="FORM HELPER", fg=NEWS, bg=PANEL, font=self.caption_font).pack(side="left")
        tk.Button(head, text="Scan again", command=self.form_scan, relief="flat", bg="#1e293b", fg=TEXT,
                  activebackground="#334155", activeforeground=TEXT, font=self.step_font, padx=10).pack(side="right")
        tk.Label(win, text=result.get("page") or "Your form", fg=TEXT, bg=PANEL, font=self.body_font, anchor="w",
                 justify="left", wraplength=int(410 * self.s)).pack(fill="x", padx=12)
        tk.Label(win, text=f"{known} of {len(fields)} answered from your profile and résumé. Copy, paste, check, submit.",
                 fg=DIM, bg=PANEL, font=self.step_font, anchor="w").pack(fill="x", padx=12, pady=(2, 8))

        outer = tk.Frame(win, bg=PANEL)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=PANEL, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=PANEL)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw", width=int(415 * self.s))
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        win.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for f in fields:
            self._form_row(body, f)

    def _form_row(self, parent: Any, field: dict[str, Any]) -> None:
        tk = self.tk
        row = tk.Frame(parent, bg="#111a2e", highlightthickness=1, highlightbackground="#22304d")
        row.pack(fill="x", padx=(12, 6), pady=4)
        top = tk.Frame(row, bg="#111a2e")
        top.pack(fill="x", padx=8, pady=(6, 2))
        tag = {"known": ("", DIM), "written": ("  written for this job", "#a78bfa"),
               "unknown": ("  not known: fill it yourself", "#fbbf24")}[field["status"]]
        tk.Label(top, text=field["label"] + tag[0], fg=tag[1] if field["status"] != "known" else DIM, bg="#111a2e",
                 font=self.step_font, anchor="w").pack(side="left")
        line = tk.Frame(row, bg="#111a2e")
        line.pack(fill="x", padx=8, pady=(0, 8))

        def copy(value: str, button: Any) -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            button.configure(text="Copied ✓")
            self.root.after(1400, lambda: button.winfo_exists() and button.configure(text="Copy"))

        style = dict(relief="flat", bg="#334155", fg=TEXT, activebackground="#475569", activeforeground=TEXT,
                     font=self.step_font, padx=10)
        if field["status"] == "unknown":
            entry = tk.Entry(line, bg="#1e293b", fg=TEXT, insertbackground=TEXT, relief="flat", font=self.body_font)
            entry.pack(side="left", fill="x", expand=True, ipady=3)
            copy_btn = tk.Button(line, text="Copy", **style)
            copy_btn.configure(command=lambda: copy(entry.get(), copy_btn))
            copy_btn.pack(side="right", padx=(6, 0))

            def keep() -> None:
                if entry.get().strip():
                    self.ctl.remember_form_answer(field["label"], entry.get())
                    keep_btn.configure(text="Saved ✓", state="disabled")

            keep_btn = tk.Button(line, text="Remember", command=keep, **style)
            keep_btn.pack(side="right", padx=(6, 0))
            return
        answer = field["answer"]
        lines = max(1, min(8, len(answer) // 46 + answer.count("\n") + 1))
        text = tk.Text(line, height=lines, wrap="word", bg="#1e293b", fg=TEXT, relief="flat", font=self.body_font,
                       padx=6, pady=4, insertbackground=TEXT)
        text.insert("1.0", answer)  # editable: change it, then Copy takes your version
        text.pack(side="left", fill="x", expand=True)
        copy_btn = tk.Button(line, text="Copy", **style)
        copy_btn.configure(command=lambda: copy(text.get("1.0", "end-1c"), copy_btn))
        copy_btn.pack(side="right", padx=(6, 0), anchor="n")

    def quit(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# --- wiring: widget + voice loop -------------------------------------------------------


class Controller:
    """What the widget can ask the voice thread to do."""

    def __init__(self, events: queue.Queue) -> None:
        self.events = events
        self.loop: Any = None  # VoiceLoop, once it has started
        self.center_open: Any = None  # opens the control center, once it has started
        self.naman: Any = None  # once it has started: the form helper reads your profile through it

    def open_center(self) -> None:
        if self.center_open:
            threading.Thread(target=self.center_open, daemon=True).start()
        else:
            self.events.put(("error", "The control center is still starting. Try again in a moment."))

    def fill_form(self) -> None:
        self.events.put(("form", ""))

    def scan_form(self) -> None:
        """Runs on a worker thread: screenshot, read the form, hand the fields to the widget."""
        from . import formfill
        from .jobs import groq_client

        n, client = self.naman, groq_client()
        if n is None or client is None or n.services.outreach is None:
            self.events.put(("form_result", json.dumps({"error": "Still starting up. Try again in a moment."})))
            return
        out = n.services.outreach
        try:
            result = formfill.scan(client, n.store, out.config, out.cache_dir)
        except Exception as exc:
            from .llm import friendly_error

            result = {"error": friendly_error(exc)}
        self.events.put(("form_result", json.dumps(result)))

    def remember_form_answer(self, label: str, value: str) -> None:
        from . import formfill

        if self.naman is not None:
            formfill.remember_answer(self.naman.store, label, value)

    def level(self, state: str) -> float:
        """How loud things are right now (0..1), for the orb: you while it listens,
        Naman while it speaks."""
        loop = self.loop
        if loop is None:
            return 0.0
        if state == "listening":
            return loop.mic_level
        if state == "speaking":
            return loop.speaker.level()
        return 0.0

    def talk(self) -> None:
        if self.loop:
            self.loop.inbox.put(("talk", ""))

    def type(self, text: str) -> None:
        if self.loop:
            self.loop.inbox.put(("type", text))
        else:
            self.events.put(("error", "Still starting up. Try again in a moment."))

    def set_muted(self, muted: bool) -> None:
        if self.loop:
            (self.loop.muted.set if muted else self.loop.muted.clear)()

    def startup_enabled(self) -> bool:
        from .startup import is_enabled

        return is_enabled()

    def set_startup(self, on: bool) -> None:
        from .startup import disable, enable

        try:
            enable() if on else disable()
        except Exception as exc:
            self.events.put(("error", f"Couldn't change startup: {exc}"))


def start_services(naman: Any, ctl: Controller, emit: Any, log: Any) -> None:
    """Everything that runs next to the voice loop: the control center, the overnight
    automation and the Telegram bot. Each one failing is logged, never fatal."""
    from .automation import Automation
    from .control import ControlCenter
    from .runtime import data_dir
    from .telegram_bot import start_in_background

    # Announcements (a finished build, new jobs, the morning briefing) slide out of the
    # orb as notifications, never spoken; Telegram adds its own listener.
    naman.listeners.append(lambda text: emit("notify", text))
    ctl.naman = naman
    naman.services.show_form_helper = ctl.fill_form
    try:
        auto = naman.services.automation = Automation(naman.hub, data_dir(), naman.announce,
                                                      os.environ.get("NAMAN_USER_NAME") or "", log=log,
                                                      outreach=naman.services.outreach)
        auto.start()
    except Exception:
        log("automation failed to start:\n" + traceback.format_exc())
    try:
        center = ControlCenter(naman, log=log, key_file=data_dir() / "control.key")
        center.start(shortcut=data_dir() / "Control Center.url")
        naman.services.open_control_center = ctl.center_open = center.open
    except Exception:
        log("control center failed to start:\n" + traceback.format_exc())
    try:
        start_in_background(naman, log)
    except Exception:
        log("telegram failed to start:\n" + traceback.format_exc())


def _single_instance() -> bool:
    """False if Naman is already running (e.g. started with Windows, then started again)."""
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.windll.kernel32
    _single_instance.handle = kernel32.CreateMutexW(None, False, "Local\\NamanAssistant")  # held until exit
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run(voice_name: str = "", device: int | None = None, greet: bool = True) -> None:
    from .runtime import data_dir

    if not _single_instance():
        print("Naman is already running (look for the orb on your screen).")
        return
    if sys.stdout is None:  # started by pythonw (e.g. with Windows): no console, so log to a file
        log_file = open(data_dir() / "naman.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log_file
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    events: queue.Queue = queue.Queue()
    ctl = Controller(events)
    holder: dict[str, Any] = {}

    def log(text: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}", flush=True)

    def worker() -> None:
        from .llm import friendly_error
        from .runtime import THREAD, make_naman
        from .voice import DEFAULT_VOICE, Microphone, PhraseWake, Speaker, Transcriber, VoiceLoop

        emit = lambda state, text="": events.put((state, text))  # noqa: E731
        try:
            naman = holder["naman"] = make_naman()
        except SystemExit as exc:  # e.g. no GROQ_API_KEY
            log(f"can't start: {exc}")
            emit("error", str(exc))
            return
        except Exception as exc:
            log("can't start:\n" + traceback.format_exc())
            emit("error", f"Couldn't start: {exc.__class__.__name__}. See data/naman.log.")
            return
        start_services(naman, ctl, emit, log)  # before the mic: they work even if the mic doesn't
        try:
            speaker = Speaker(voice_name or os.environ.get("NAMAN_VOICE") or DEFAULT_VOICE)
            wake, transcriber = PhraseWake(), Transcriber()
            mic = Microphone(device)
        except Exception as exc:  # no mic, audio driver trouble: the rest keeps running
            log("voice can't start:\n" + traceback.format_exc())
            emit("error", f"Voice couldn't start ({exc.__class__.__name__}); the control center and Telegram still work.")
            return
        for st in naman.mcp.status.values():
            log(f"mcp {st.name}: {'%d tools' % len(st.tools) if st.ok else 'failed: %s' % st.error}")
        with mic:
            loop = ctl.loop = VoiceLoop(naman, THREAD, mic, speaker, transcriber, wake, log=log,
                                        on_state=emit, stay_awake=True, follow_up_s=6)

            def progress(event: str, data: dict[str, Any]) -> None:
                if event == "plan":
                    log("plan: " + " -> ".join(data["steps"]))
                    emit("plan", json.dumps(data["steps"]))
                elif event == "step":
                    log(f"step: {data['text']}")
                    emit("step", json.dumps(data))

            naman.on_progress = progress
            naman.scheduler.add_listener(lambda text: loop.inbox.put(("say", text)))
            if greet:
                name = os.environ.get("NAMAN_USER_NAME") or ""
                loop.inbox.put(("say", greeting(name, naman.store.briefing())))
            log(f"listening for '{wake_word()}'")
            while True:
                try:
                    loop.run_once()
                except Exception as exc:  # network down, audio device lost...: report, keep going
                    log("error:\n" + traceback.format_exc())
                    emit("error", friendly_error(exc))
                    time.sleep(2)

    threading.Thread(target=worker, daemon=True, name="voice").start()
    widget = Widget(events, ctl, data_dir() / "widget.json")
    try:
        widget.run()
    finally:
        if "naman" in holder:
            holder["naman"].close()  # stop MCP servers and reminders
        os._exit(0)  # the voice thread may be blocked on the microphone
