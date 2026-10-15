"""Plug any MCP server into Naman as extra tools.

Servers are listed in mcp_servers.json, in the same shape Claude Desktop uses:

    {"mcpServers": {
        "pokeapi": {"command": "python", "args": ["path/to/server.py"]},
        "files":   {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "~/notes"],
                    "approval": "writes"}
    }}

Per-server extras:
    approval  "writes" (default): ask before any tool not marked read-only
              "always": ask before every tool;  "never": trust the server fully
    tools     optional allow-list of tool names to expose
    enabled   false to keep an entry without starting it
    description  optional one-liner telling the skill router when to load this server

Each server runs as a subprocess with one long-lived MCP session. Sessions live on a
background event loop, so Naman's synchronous agent can call them without spawning a
process per call. A server that fails to start is reported and skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .tools import Tool, Toolset

START_TIMEOUT_S = 60  # npx may download a package on first run
CALL_TIMEOUT_S = 90
MAX_DESCRIPTION = 1000  # third-party descriptions go into every prompt; keep them bounded
_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass
class ServerStatus:
    name: str
    ok: bool = False
    error: str = ""
    tools: list[str] = field(default_factory=list)


def load_config(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", data)
    return {name: cfg for name, cfg in servers.items() if cfg.get("enabled", True)}


def tool_name(server: str, tool: str) -> str:
    """`server__tool`, limited to the characters and length function-calling APIs accept."""
    return _NAME_OK.sub("_", f"{server}__{tool}")[:64]


def needs_approval(policy: str, tool: dict[str, Any]) -> bool:
    if policy == "never":
        return False
    if policy == "always":
        return True
    return not (tool.get("annotations") or {}).get("readOnlyHint", False)


def _wire(obj: Any) -> dict[str, Any]:
    return obj.model_dump(mode="json", by_alias=True, exclude_none=True)


def _params(cfg: dict[str, Any], base_dir: Path) -> StdioServerParameters:
    command = cfg["command"]
    if command == "python":
        import sys

        command = sys.executable  # same interpreter as Naman
    else:
        command = shutil.which(command) or command  # Windows: npx -> npx.cmd
    args = [str(base_dir / a) if a.endswith(".py") and not os.path.isabs(a) else a for a in cfg.get("args", [])]
    env = {**os.environ, **{k: str(v) for k, v in (cfg.get("env") or {}).items()}}
    return StdioServerParameters(command=command, args=args, env=env, cwd=cfg.get("cwd"))


class MCPManager:
    def __init__(self, config: dict[str, dict[str, Any]], base_dir: Path | str = ".") -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.status: dict[str, ServerStatus] = {}
        self.sessions: dict[str, ClientSession] = {}
        self._tools: dict[str, list[dict[str, Any]]] = {}
        self._loop = asyncio.new_event_loop()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp")
        self._started = False

    # --- lifecycle -----------------------------------------------------------------------

    def start(self) -> list[ServerStatus]:
        """Connect to every configured server; returns one status per server."""
        if not self.config:
            return []
        self._thread.start()
        self._started = True
        self._stop = asyncio.run_coroutine_threadsafe(self._make_event(), self._loop).result()
        pending: dict[str, Future] = {}
        for name, cfg in self.config.items():
            ready: Future = Future()
            self._loop.call_soon_threadsafe(lambda n=name, c=cfg, r=ready: self._loop.create_task(self._serve(n, c, r)))
            pending[name] = ready
        for name, ready in pending.items():
            status = ServerStatus(name)
            try:
                tools = ready.result(timeout=START_TIMEOUT_S)
                allow = self.config[name].get("tools")
                self._tools[name] = [t for t in tools if not allow or t["name"] in allow]
                status.ok, status.tools = True, [t["name"] for t in self._tools[name]]
            except Exception as exc:
                status.error = _root(exc)
            self.status[name] = status
        return list(self.status.values())

    async def _make_event(self) -> asyncio.Event:
        return asyncio.Event()

    async def _serve(self, name: str, cfg: dict[str, Any], ready: Future) -> None:
        """Owns one server's connection for its whole life (anyio contexts must be
        entered and exited in the same task)."""
        try:
            with open(os.devnull, "w") as devnull:
                async with stdio_client(_params(cfg, self.base_dir), errlog=devnull) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = [_wire(t) for t in (await session.list_tools()).tools]
                        self.sessions[name] = session
                        ready.set_result(tools)
                        await self._stop.wait()  # type: ignore[union-attr]
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(repr(exc)))
            elif name in self.status:
                self.status[name].ok, self.status[name].error = False, f"disconnected: {_root(exc)}"
        finally:
            self.sessions.pop(name, None)

    def close(self) -> None:
        if not self._started:
            return
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        pending = asyncio.run_coroutine_threadsafe(self._drain(), self._loop)
        try:
            pending.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._started = False

    async def _drain(self) -> None:
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.wait(tasks, timeout=8) if tasks else None

    # --- tools ---------------------------------------------------------------------------

    def call(self, server: str, tool: str, args: dict[str, Any]) -> str:
        session = self.sessions.get(server)
        if session is None:
            return f"error: MCP server {server!r} is not connected"
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool, args, read_timeout_seconds=CALL_TIMEOUT_S), self._loop
        )
        try:
            result = _wire(future.result(timeout=CALL_TIMEOUT_S + 5))
        except Exception as exc:
            return f"error: {server}/{tool} failed: {_root(exc)}"
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if not text and result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"])
        return f"error: {text}" if result.get("isError") else (text or "(no output)")

    def _runner(self, server: str, tool: str):
        # A closure, not keyword defaults: any argument name the tool uses stays free.
        def run(**args: Any) -> str:
            return self.call(server, tool, args)

        return run

    def toolsets(self) -> list[Toolset]:
        """One toolset per server, described for the skill router."""
        out = []
        for server in self._tools:
            tools = [t for t in self.tools() if t.name.startswith(tool_name(server, ""))]
            summary = self.config[server].get("description") or "; ".join(
                f"{t.name.split('__', 1)[1]}: {t.description.split('] ', 1)[-1][:60]}" for t in tools[:8]
            )
            out.append(Toolset(f"mcp:{server}", f"{server} (MCP server): {summary}"[:400], tools))
        return out

    def tools(self) -> list[Tool]:
        out = []
        for server, tools in self._tools.items():
            policy = self.config[server].get("approval", "writes")
            for t in tools:
                desc = (t.get("description") or "").strip()[:MAX_DESCRIPTION]
                out.append(
                    Tool(
                        name=tool_name(server, t["name"]),
                        description=f"[{server}] {desc}",
                        parameters=t.get("inputSchema") or {"type": "object", "properties": {}},
                        run=self._runner(server, t["name"]),
                        needs_approval=needs_approval(policy, t),
                    )
                )
        return out


def _root(exc: BaseException) -> str:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{exc.__class__.__name__}: {str(exc)[:200]}"
