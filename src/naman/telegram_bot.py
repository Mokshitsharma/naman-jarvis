"""Talk to Naman from your phone through a private Telegram bot.

The bot runs on the laptop, so actions like opening an app happen on the laptop, and
each risky one asks for approval with Approve/Deny buttons. Only the Telegram user ids
in TELEGRAM_ALLOWED_USER_IDS are served; anyone else just gets their own id back.

It's the same Naman and the same conversation as the orb and the control center:
"I've finished the resume" ticks the task off everywhere, "what next?" looks at your
schedule and tasks. Naman also writes first: the morning briefing, new job matches,
finished Claude Code builds, reminders, the evening check-in.

`naman start` runs the bot alongside the orb when TELEGRAM_BOT_TOKEN is set;
`naman telegram` runs it on its own; `naman telegram-setup` connects your account.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .agent import Naman, Reply
from .llm import friendly_error
from .runtime import THREAD, make_naman

MAX_MESSAGE = 4000  # Telegram's limit is 4096
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # Groq's free tier takes files up to 25 MB

HELP = """\
Talk to me like you would out loud: "what next?", "I finished the resume task", "show jobs above 70", \
"remind me at 6 to call mom", "build a todo app in C:/code/todo with Claude Code".

/next - what to do now (schedule + tasks)
/tasks - open tasks
/jobs - best new job matches
/brief - today's briefing
/status - the laptop: battery, CPU, what's open
/screenshot - see the laptop's screen
/help - this message"""


def allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def pc_status() -> str:
    import psutil

    from .skills import system

    s = system.system_status()
    up = int(time.time() - psutil.boot_time())
    lines = [f"Laptop is on (up {up // 3600}h {up % 3600 // 60}m)."]
    if s["battery_percent"] is not None:
        lines.append(f"Battery {s['battery_percent']:.0f}%" + (" charging" if s["charging"] else "")
                     + (f", ~{s['battery_minutes_left']} min left" if s["battery_minutes_left"] else ""))
    lines.append(f"CPU {s['cpu_percent']:.0f}%, memory {s['memory_percent']:.0f}%, {s['disk_free_gb']} GB free")
    try:
        lines.append("In front: " + (system._foreground_title() or "desktop"))
        wins = [w for w in system.list_windows() if w.strip()][:8]
        if wins:
            lines.append("Open: " + "; ".join(w[:50] for w in wins))
    except Exception:
        pass
    return "\n".join(lines)


def screenshot_png() -> bytes:
    from PIL import ImageGrab

    img = ImageGrab.grab(all_screens=True)
    img.thumbnail((1920, 1920))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class Bot:
    def __init__(self, naman: Naman, allowed: set[int]) -> None:
        self.naman = naman
        self.allowed = allowed
        self.transcriber = None  # created on the first voice note

    def _ok(self, update: Update) -> bool:
        return bool(update.effective_user and update.effective_user.id in self.allowed)

    async def _deny(self, update: Update) -> None:
        uid = update.effective_user.id if update.effective_user else "unknown"
        await update.effective_message.reply_text(
            f"This is a private assistant. Your Telegram user id is {uid}. "
            "If this is your bot, add it to TELEGRAM_ALLOWED_USER_IDS in .env and restart."
        )

    async def _send_reply(self, update: Update, reply: Reply) -> None:
        message = update.effective_message
        if reply.approval:
            args = json.dumps(reply.approval["args"], ensure_ascii=False)
            buttons = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Approve", callback_data="approve"),
                  InlineKeyboardButton("Deny", callback_data="deny")]]
            )
            await message.reply_text(f"Allow {reply.approval['tool']} {args[:3000]} on the laptop?", reply_markup=buttons)
            return
        await self._send_text(message, reply.text or "(no reply)")

    @staticmethod
    async def _send_text(message, text: str) -> None:
        for i in range(0, len(text), MAX_MESSAGE):
            await message.reply_text(text[i: i + MAX_MESSAGE], disable_web_page_preview=True)

    async def _handle(self, update: Update, text: str) -> None:
        await update.effective_chat.send_action("typing")
        try:  # Naman.send takes its own lock: voice, the control center and this share a conversation
            reply = await asyncio.to_thread(self.naman.send, THREAD, text)
        except Exception as exc:
            reply = Reply(text=friendly_error(exc))
        await self._send_reply(update, reply)

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await self._handle(update, update.effective_message.text)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        message = update.effective_message
        media = message.voice or message.audio
        if media.file_size and media.file_size > MAX_AUDIO_BYTES:
            return await message.reply_text("That recording is too long for me. Keep it under a few minutes.")
        audio = bytes(await (await media.get_file()).download_as_bytearray())
        name = "voice.ogg" if message.voice else (media.file_name or "audio.mp3")
        text = await asyncio.to_thread(self._transcriber().transcribe, audio, name)
        if not text:
            return await message.reply_text("I couldn't make out any words in that.")
        await message.reply_text(f"Heard: {text}")
        await self._handle(update, text)

    def _transcriber(self):
        if self.transcriber is None:
            from .voice import Transcriber

            self.transcriber = Transcriber()
        return self.transcriber

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._ok(update):
            return await query.answer("Not allowed")
        await query.answer()
        approved = query.data == "approve"
        await query.edit_message_reply_markup(None)  # buttons work once
        await query.message.reply_text("Approved." if approved else "Denied.")
        if self.naman.pending(THREAD) is None:
            return
        try:
            reply = await asyncio.to_thread(self.naman.resume, THREAD, approved)
        except Exception as exc:
            reply = Reply(text=friendly_error(exc))
        await self._send_reply(update, reply)

    # --- commands --------------------------------------------------------------------------

    def _command(self, text: str):
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if not self._ok(update):
                return await self._deny(update)
            await self._handle(update, text)
        return handler

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await update.effective_message.reply_text("Naman here, same brain as on your laptop.\n\n" + HELP)

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await update.effective_message.reply_text(HELP)

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await update.effective_message.reply_text(await asyncio.to_thread(pc_status))

    async def on_screenshot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._ok(update):
            return await self._deny(update)
        await update.effective_chat.send_action("upload_photo")
        png = await asyncio.to_thread(screenshot_png)
        await update.effective_message.reply_photo(png, caption=time.strftime("Screen at %H:%M"))


def build_app(naman: Naman, token: str, allowed: set[int], on_ready: Callable | None = None) -> Application:
    bot = Bot(naman, allowed)
    builder = Application.builder().token(token)
    if on_ready:
        builder = builder.post_init(on_ready)
    app = builder.build()
    app.add_handler(CommandHandler("start", bot.on_start))
    app.add_handler(CommandHandler("help", bot.on_help))
    app.add_handler(CommandHandler("status", bot.on_status))
    app.add_handler(CommandHandler("screenshot", bot.on_screenshot))
    app.add_handler(CommandHandler("brief", bot._command("Give me my briefing for today.")))
    app.add_handler(CommandHandler("next", bot._command("What should I do next?")))
    app.add_handler(CommandHandler("tasks", bot._command("List my open tasks.")))
    app.add_handler(CommandHandler("jobs", bot._command("Show my best new job matches with fit scores and links.")))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, bot.on_voice))
    return app


def _pusher(naman: Naman, allowed: set[int]):
    """on_ready hook: messages Naman starts (announcements, reminders) go to your chat."""
    async def on_ready(application: Application) -> None:
        loop = asyncio.get_running_loop()

        def push(text: str) -> None:  # called from other threads
            for uid in allowed:
                for i in range(0, len(text), MAX_MESSAGE):
                    asyncio.run_coroutine_threadsafe(
                        application.bot.send_message(uid, text[i: i + MAX_MESSAGE], disable_web_page_preview=True), loop)

        naman.listeners.append(push)
        if naman.scheduler is not None:
            naman.scheduler.add_listener(push)
    return on_ready


def start_in_background(naman: Naman, log: Callable[[str], None] = print) -> bool:
    """Runs the bot on its own thread next to the orb. False when it isn't set up."""
    token, allowed = os.environ.get("TELEGRAM_BOT_TOKEN"), allowed_ids()
    if not token or not allowed:
        log("telegram: not set up (run `naman telegram-setup`)")
        return False

    def run() -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            app = build_app(naman, token, allowed, _pusher(naman, allowed))
            naman.telegram_on = True
            app.run_polling(stop_signals=None, close_loop=False)  # signals only work on the main thread
        except Exception as exc:
            log(f"telegram stopped: {exc!r}")
        finally:
            naman.telegram_on = False

    threading.Thread(target=run, daemon=True, name="telegram").start()
    log("telegram: bot running")
    return True


def run_bot() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Run `naman telegram-setup`.")
    allowed = allowed_ids()
    if not allowed:
        print("No TELEGRAM_ALLOWED_USER_IDS set: the bot will only tell people their user id.")
    naman = make_naman()
    for s in naman.mcp.status.values():
        print(f"mcp {s.name}: " + (f"{len(s.tools)} tools" if s.ok else f"failed to start ({s.error})"))
    app = build_app(naman, token, allowed, _pusher(naman, allowed))
    print("Naman is on Telegram. Ctrl+C to stop.")
    try:
        app.run_polling()
    finally:
        naman.close()


# --- setup -----------------------------------------------------------------------------------


def set_env(path: Path, key: str, value: str) -> None:
    """Sets KEY=value in a .env file, keeping every other line as it was."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup(token: str, env_path: Path, wait_s: int = 180, say: Callable[[str], None] = print) -> bool:
    """Checks the token, waits for you to message the bot, saves your user id."""
    import httpx

    api = f"https://api.telegram.org/bot{token}"
    me = httpx.get(f"{api}/getMe", timeout=20).json()
    if not me.get("ok"):
        say("That token was rejected by Telegram. Copy it again from @BotFather.")
        return False
    name = me["result"]["username"]
    set_env(env_path, "TELEGRAM_BOT_TOKEN", token)
    say(f"Token OK: your bot is @{name}. Now open Telegram and send it any message (e.g. hi)...")
    offset, deadline = None, time.time() + wait_s
    while time.time() < deadline:
        params = {"timeout": 25, **({"offset": offset} if offset else {})}
        updates = httpx.get(f"{api}/getUpdates", params=params, timeout=35).json().get("result", [])
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            user = msg.get("from") or {}
            if user.get("id") and not user.get("is_bot"):
                set_env(env_path, "TELEGRAM_ALLOWED_USER_IDS", str(user["id"]))
                httpx.get(f"{api}/getUpdates", params={"offset": offset}, timeout=20)  # don't replay "hi"
                httpx.post(f"{api}/sendMessage", timeout=20, json={
                    "chat_id": user["id"],
                    "text": f"Connected, {user.get('first_name', '')}. Restart Naman on the laptop and I'll be here.\n\n"
                            + HELP})
                say(f"Connected {user.get('first_name', '')} (id {user['id']}). Saved to {env_path}. "
                    "Restart Naman (`naman start`) to use it.")
                return True
    say("No message arrived. Run this again and message the bot within 3 minutes.")
    return False
