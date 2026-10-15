"""The control center: one window with everything Naman keeps for you.

Jobs (with fit scores and contacts), hiring posts, posts worth reading, tasks and the
weekly schedule, Claude Code builds, memory, and a chat box that talks to the same
Naman (same conversation as voice and Telegram).

A small local web server (standard library only) serves web/index.html and a JSON API.
It listens on 127.0.0.1 only, and every API call must carry a secret key (it's in the
URL the orb opens; made once and kept in data/control.key, so an open window survives
a restart), so other devices and other websites open in the browser can't read or
change anything.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .runtime import THREAD

WEB = Path(__file__).parent / "web"
DEFAULT_PORT = 8765


def copy_file_to_clipboard(path: str) -> None:
    """Puts the file itself on the clipboard (like Ctrl+C in Explorer), so Ctrl+V attaches it in
    LinkedIn messages, Gmail or WhatsApp. The path travels in an environment variable."""
    subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -LiteralPath $env:NAMAN_CLIP"],
                   env={**os.environ, "NAMAN_CLIP": path}, capture_output=True, timeout=20,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _load_key(path: Path | None) -> str:
    """The secret every API call carries. Kept in a file (made once) so a window that's
    open while Naman restarts keeps working; without a file, a new key each start."""
    if path is None:
        return secrets.token_urlsafe(24)
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 24:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(24)
    try:
        path.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


def _edge() -> str | None:
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(path).exists():
            return path
    return shutil.which("msedge")


class ControlCenter:
    def __init__(self, naman: Any, port: int = DEFAULT_PORT, log=print, key_file: Path | None = None) -> None:
        self.naman, self.log = naman, log
        self.token = _load_key(key_file)
        handler = type("Handler", (_Handler,), {"center": self})
        for p in range(port, port + 10):  # another copy or app may hold the port
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", p), handler)
                break
            except OSError:
                continue
        else:
            raise OSError(f"no free port in {port}-{port + 9}")
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?k={self.token}"

    def start(self, shortcut: Path | None = None) -> None:
        threading.Thread(target=self.server.serve_forever, daemon=True, name="control-center").start()
        self.log(f"control center on http://127.0.0.1:{self.port}")
        if shortcut is not None:  # a double-clickable link (rewritten each start: the port can change)
            try:
                shortcut.write_text(f"[InternetShortcut]\nURL={self.url}\n", encoding="utf-8")
            except OSError:
                pass

    def stop(self) -> None:
        self.server.shutdown()

    def open(self) -> str:
        """Opens as a Chrome app window (no tabs, no address bar; your signed-in profile, so
        "Open in Gmail" lands in the right account); Edge or a browser tab if Chrome is missing."""
        from .browser import open_app_window

        if os.environ.get("NAMAN_BROWSER", "chrome").lower() == "chrome" and open_app_window(self.url):
            return "opened the control center"
        edge = _edge()
        if edge:
            subprocess.Popen([edge, f"--app={self.url}", "--window-size=1400,900"])
        else:
            webbrowser.open(self.url)
        return "opened the control center"

    # --- API ---------------------------------------------------------------------------------

    def api(self, method: str, path: str, query: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
        n, hub, store = self.naman, self.naman.hub, self.naman.store
        parts = [p for p in path.split("/") if p][1:]  # after "api"
        what = parts[0] if parts else ""
        ident = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        auto = n.services.automation

        if what == "overview" and method == "GET":
            from .tools import whats_next

            jobs = hub.jobs(limit=1000, include_description=False)
            return 200, {
                "name": os.environ.get("NAMAN_USER_NAME", ""),
                "next": whats_next(store, hub),
                "counts": {"jobs_new": sum(j["status"] == "new" for j in jobs),
                           "jobs_strong": sum(j["fit"] >= 70 and j["status"] != "hidden" for j in jobs),
                           "saved": sum(j["status"] == "saved" for j in jobs),
                           "applied": sum(j["status"] == "applied" for j in jobs),
                           "posts": len(hub.posts(status="new", limit=500)), "tasks": len(store.list_tasks()),
                           "memories": len(store.recent_memories(10_000))},
                "top_jobs": [j for j in jobs if j["status"] in ("new", "saved")][:6],
                "runs": hub.runs(3),
                "drafts": len(n.services.outreach.items("draft")) if n.services.outreach else 0,
                "status": {"telegram": bool(getattr(n, "telegram_on", False)), "automation": auto is not None,
                           "busy": bool(auto and auto._busy.locked()),
                           "progress": getattr(auto, "progress", "") if auto else "",
                           "last_added": (getattr(auto, "last_result", {}) or {}).get("added") if auto else None,
                           "last_jobs": hub.get("last:jobs_refresh"), "last_feed": hub.get("last:feed_refresh")},
            }
        if what == "jobs":
            out_ = n.services.outreach
            sub = parts[2:]  # /jobs/<id>/<sub>
            if ident and sub:
                if out_ is None:
                    return 409, {"error": "résumés and replies aren't available here"}
                if sub == ["resumes"] and method == "GET":
                    return 200, out_.resume_scores(ident)
                if sub == ["tailor"] and method == "POST":  # about a minute: written, then checked twice
                    return 200, out_.tailor(ident)
                if sub == ["reply"] and method == "GET":
                    return 200, (out_.reply(ident) if hub.get(f"reply:{ident}") else {})
                if sub == ["reply"] and method == "POST":
                    return 200, out_.reply(ident, refresh=bool(body.get("refresh")))
                if sub == ["mail"] and method == "POST":  # Open mail: compose link + résumé on the clipboard
                    result = out_.mail(ident, body.get("to_email", ""), body.get("text", ""), body.get("subject", ""))
                    if result["resume"] and out_.known_resume(result["resume"]["file"]):
                        copy_file_to_clipboard(result["resume"]["file"])
                    return 200, result
                return 404, {"error": f"no route {method} {path}"}
            if method == "GET" and ident:
                job = hub.job(ident)
                return (200, job) if job else (404, {"error": "no such job"})
            if method == "GET":
                rows = hub.jobs(status=query.get("status") or None, min_fit=int(query.get("min_fit") or 0),
                                query=query.get("q", ""), include_description=False, where=query.get("where", ""))
                if query.get("source"):
                    rows = [r for r in rows if r["source"] == query["source"]]
                return 200, rows
            if method == "POST" and ident:
                return 200, {"ok": hub.set_job_status(ident, body.get("status", ""))}
        if what == "posts":
            if method == "GET":
                return 200, hub.posts(status=query.get("status") or None, limit=200)
            if method == "POST" and ident:
                return 200, {"ok": hub.set_post_status(ident, body.get("status", ""))}
        if what == "tasks":
            if method == "GET":
                return 200, {"open": store.list_tasks(), "done": store.list_tasks(status="done")[:20]}
            if method == "POST" and ident and parts[2:] == ["done"]:
                return 200, {"ok": store.complete_task(ident)}
            if method == "POST":
                task_id = store.add_task(body["title"], body.get("project") or "Personal",
                                         body.get("category") or "other", body.get("priority") or "medium",
                                         body.get("due") or None)
                return 200, {"id": task_id}
        if what == "schedule":
            if method == "GET":
                return 200, hub.blocks()
            if method == "POST":
                return 200, {"id": hub.add_block(body["days"], body["start"], body["end"], body["title"])}
            if method == "DELETE" and ident:
                return 200, {"ok": hub.delete_block(ident)}
        if what == "runs":
            if method == "GET":
                return 200, hub.runs(20)
            if method == "POST":  # clicking "Start" in the window is the approval
                return 200, {"message": n.services.builder.start(body["folder"], body["instructions"],
                                                                 bool(body.get("new_project")))}
        if what == "memory":
            if method == "GET":
                return 200, {"profile": store.profile(), "memories": store.recent_memories(10_000)}
            if method == "DELETE" and ident:
                return 200, {"ok": store.forget(ident)}
        out = n.services.outreach
        if what == "outreach" and out is not None:
            if method == "GET":
                return 200, out.items()
            if method == "POST" and not ident:  # draft for a job (the model writes it: a few seconds)
                result = out.draft(int(body["job_id"]), body.get("to_email", ""))
                return (409, {"error": result}) if isinstance(result, str) else (200, result)
            if method == "POST" and parts[1:] == ["sendall"]:  # "Send all", confirmed in the page
                return 200, out.send_all()
            if method == "POST" and ident and parts[2:] == ["send"]:  # clicking "Send" is the approval
                try:
                    return 200, out.send(ident)
                except (ValueError, RuntimeError, OSError) as exc:  # a [placeholder], no password, Gmail said no
                    return 409, {"error": str(exc)}
            if method == "POST" and ident and parts[2:] == ["mark"]:
                out.mark(ident, body.get("status", ""))
                return 200, {"ok": True}
            if method == "POST" and ident and parts[2:] == ["resume"]:
                item = out.item(ident)
                if item and item["attachment"] and Path(item["attachment"]).exists():
                    subprocess.Popen(["explorer.exe", "/select,", item["attachment"]])
                    return 200, {"ok": True}
                return 404, {"error": "no résumé file for this email"}
            if method == "POST" and ident:
                out.edit(ident, body)
                return 200, {"ok": True}
        if what == "companies":
            from . import contacts

            if method == "GET":
                return 200, contacts.companies(hub)
            if method == "POST" and body.get("domain"):  # you correct the website: looked up again there
                return 200, contacts.set_domain(hub, body["name"], body["domain"])
            if method == "POST":
                return 200, {"companies": contacts.add_company(hub, body.get("name", ""))}
        if what == "contacts" and method == "GET":  # one company: up to a minute the first time
            from . import contacts

            if query.get("cached") == "1":  # only what's already known: instant
                return 200, contacts.cached(hub, query.get("company", "")) or {}
            return 200, contacts.lookup(hub, query.get("company", ""), refresh=query.get("refresh") == "1")
        if what == "contacts" and method == "POST":  # many companies, in the background
            from . import contacts

            names = [c["company"] for c in contacts.companies(hub) if not c["checked"]][:int(body.get("limit", 15))]

            def run() -> None:
                for name in names:
                    try:
                        contacts.lookup(hub, name)
                    except Exception as exc:
                        self.log(f"contacts for {name}: {exc!r}")
                n.announce(f"Looked up contacts for {len(names)} companies (control center, Companies).")

            threading.Thread(target=run, daemon=True, name="contacts").start()
            return 200, {"started": len(names)}
        if what == "paste" and method == "POST" and out is not None:  # one pasted post: ~20-90 s
            try:
                text = body.get("text", "")
                if body.get("image"):  # a screenshot: read the post off it first
                    from . import jobs as jobs_mod, screen

                    client = jobs_mod.groq_client()
                    if client is None:
                        return 503, {"error": "no AI model available"}
                    seen = screen.job_post(client, image_url=body["image"])
                    if not seen and not text.strip():
                        return 422, {"error": "I couldn't find a job post in that screenshot"}
                    text = f"{seen}\n\n{text}".strip()
                return 200, out.analyse_paste(text, body.get("url", ""))
            except Exception as exc:
                if exc.__class__.__name__ in {"RateLimitError", "APIConnectionError", "APITimeoutError"}:
                    from .llm import friendly_error

                    return 503, {"error": friendly_error(exc)}  # the post stays in its slot to retry
                raise
        if what == "file" and method == "POST" and out is not None:  # a résumé: copy it, or open it
            path = body.get("path", "")
            if not out.known_resume(path):
                return 403, {"error": "only your résumés can be copied or opened"}
            if body.get("action") == "open":
                os.startfile(path)
                return 200, {"ok": True}
            copy_file_to_clipboard(path)
            return 200, {"ok": True}
        if what == "refresh" and method == "POST":
            if auto is None:
                return 409, {"error": "automation isn't running"}
            job = {"jobs": auto.refresh_jobs, "tech": auto.refresh_tech}.get(body.get("what"), auto.refresh_feed)
            started = auto.in_background(job, f"refresh-{body.get('what')}")
            return 200, {"started": started}
        if what == "tech" and method == "GET":
            if auto is None:
                return 409, {"error": "automation isn't running"}
            return 200, {"items": auto.tech.items(query.get("category", "")), "last": hub.get("last:tech_refresh"),
                         "busy": auto.progress}
        if what == "autoapply":
            if out is None:
                return 409, {"error": "outreach isn't available"}
            if method == "POST":  # the switch on the Outreach page
                cfg = out.config | {"auto_send": bool(body.get("on"))}
                out.config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            cfg = out.config
            return 200, {"on": bool(cfg.get("auto_send")), "ready": out.can_send(),
                         "min_fit": cfg["auto_send_min_fit"], "per_day": cfg["auto_send_per_day"]}
        if what == "history" and method == "GET":  # the chat page: the shared conversation so far
            return 200, {"messages": n.history(THREAD), "approval": n.pending(THREAD)}
        if what == "ask" and method == "POST":
            from .llm import friendly_error

            try:
                reply = n.send(THREAD, body.get("text", ""))
            except Exception as exc:  # the chat shows a sentence, not "Couldn't reach Jarvis"
                self.log(f"chat error: {exc!r}")
                return 200, {"text": friendly_error(exc), "approval": None}
            return 200, {"text": reply.text, "approval": reply.approval}
        if what == "resume" and method == "POST":
            if n.pending(THREAD) is None:
                return 200, {"text": "Nothing is waiting for an answer."}
            reply = n.resume(THREAD, bool(body.get("approved")))
            return 200, {"text": reply.text, "approval": reply.approval}
        return 404, {"error": f"no route {method} {path}"}


class _Handler(BaseHTTPRequestHandler):
    center: ControlCenter

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet: the orb logs to a file
        pass

    def _send(self, code: int, payload: Any, content_type: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _allowed_host(self) -> bool:
        # Blocks DNS-rebinding: a page on evil.example resolving to 127.0.0.1 sends its own Host.
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in {"127.0.0.1", "localhost"}

    def _route(self, method: str) -> None:
        if not self._allowed_host():
            return self._send(403, {"error": "bad host"})
        url = urlparse(self.path)
        if url.path == "/api/companies.xlsx" and method == "GET":
            if not secrets.compare_digest(parse_qs(url.query).get("k", [""])[0], self.center.token):
                return self._send(401, {"error": "missing or wrong key"})
            import tempfile

            from . import contacts

            out = contacts.xlsx_export(self.center.naman.hub, Path(tempfile.gettempdir()) / "naman-companies.xlsx")
            data = out.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="Cold Email Companies.xlsx"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if url.path == "/api/companies.csv" and method == "GET":
            if not secrets.compare_digest(parse_qs(url.query).get("k", [""])[0], self.center.token):
                return self._send(401, {"error": "missing or wrong key"})
            from . import contacts

            data = contacts.csv_export(self.center.naman.hub).encode("utf-8-sig")  # BOM: Excel opens it right
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="cold-email-companies.csv"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not url.path.startswith("/api/"):
            if method == "GET" and url.path in {"/", "/index.html"}:
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            return self._send(404, {"error": "not found"})
        if not secrets.compare_digest(self.headers.get("X-Naman-Key", ""), self.center.token):
            return self._send(401, {"error": "missing or wrong key"})
        body: dict[str, Any] = {}
        if method in {"POST", "DELETE"}:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._send(400, {"error": "body must be JSON"})
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            code, payload = self.center.api(method, url.path, query, body)
        except (KeyError, ValueError, TypeError) as exc:
            code, payload = 400, {"error": str(exc)}
        except Exception as exc:  # never kill the server thread
            from .llm import friendly_error

            self.center.log(f"control center error on {method} {url.path}: {exc!r}")
            code, payload = 500, {"error": friendly_error(exc)}
        self._send(code, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")
