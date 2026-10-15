"""Build a ready-to-use Naman from environment settings."""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from .agent import Naman
from .llm import GroqChat
from .mcp_tools import MCPManager, load_config
from .reminders import Scheduler
from .router import GroqRouter
from .store import Store

THREAD = "main"  # one conversation shared by laptop and phone


def data_dir() -> Path:
    path = Path(os.environ.get("NAMAN_DATA_DIR") or "data")
    path.mkdir(parents=True, exist_ok=True)
    return path


def mcp_config_path() -> Path:
    return Path(os.environ.get("NAMAN_MCP_CONFIG") or "mcp_servers.json")


def make_store() -> Store:
    return Store(data_dir() / "naman.db")


def start_mcp() -> MCPManager:
    path = mcp_config_path()
    manager = MCPManager(load_config(path), base_dir=path.resolve().parent)
    manager.start()
    return manager


def make_naman() -> Naman:
    """Remember to call naman.close() when done: it stops MCP servers and reminders.
    Front ends add their own reminder channel with naman.scheduler.add_listener()."""
    if not os.environ.get("GROQ_API_KEY"):
        raise SystemExit("GROQ_API_KEY is not set. Add it to .env (free key: https://console.groq.com/keys).")
    from .browser import use_chrome

    use_chrome()  # links, searches and songs open in Chrome (NAMAN_BROWSER=default to undo)
    conn = sqlite3.connect(str(data_dir() / "conversations.db"), check_same_thread=False)
    store = make_store()
    manager = start_mcp()
    naman = Naman(
        store=store,
        model=GroqChat(),
        checkpointer=SqliteSaver(conn),
        user_name=os.environ.get("NAMAN_USER_NAME") or "the user",
        extra_toolsets=manager.toolsets(),
        confirm_actions=os.environ.get("NAMAN_CONFIRM_ACTIONS", "").lower() in {"1", "true", "yes"},
        router=GroqRouter(),
    )
    naman.mcp = manager
    naman.scheduler = Scheduler(store)
    naman.scheduler.start()
    # Scanning the Start Menu takes ~1.5 s; do it now so the first "open ..." is instant.
    threading.Thread(target=_index_apps, daemon=True, name="apps").start()
    return naman


def _index_apps() -> None:
    try:
        from .apps import installed_apps

        installed_apps()
    except Exception:
        pass  # opening apps will just scan on first use
