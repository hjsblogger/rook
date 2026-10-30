"""A local web UI for the planner.

    python3 -m travel_planner serve [--port 8765]

Standard library only, bound to 127.0.0.1. The page posts a request, the
turn runs in a background thread, and the page follows it by streaming the
session's own event log (`session.jsonl`) over server-sent events. The log is
the source of the live view on purpose: it is what the CLI's `logs` command
shows too, so the UI cannot display progress the log does not record.

    GET  /                          the page
    POST /api/execute               {"session_id", "task"} -> starts a turn
    GET  /api/events?session_id=S&from=N   SSE: log entries from line N, then `result`
    GET  /api/sessions              recent sessions
    GET  /api/artifact?session_id=S&name=F the itinerary or trip file
    GET  /api/session?session_id=S  everything the page draws: state, latest trip, booking, itinerary
    GET  /api/mcp/tools             every declared MCP server and its tools (tools/list)
    POST /api/mcp/call              {"server", "tool", "arguments"} -> one tools/call, run by hand

The two /api/mcp routes are a playground for calling the same servers the
agents use. Calls made there are not part of any session, so they never show
up in an agent's trace or in what Rook grades.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import mcp, planner
from .llm import ModelError

UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"
SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")

# session id -> {"state": "running" | "done", "result": dict | None}
_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()


def _run(session, task: str) -> None:
    try:
        result = {"ok": True, "session_id": session.session_id, **planner.run_turn(session, task)}
    except ModelError as err:
        session.log("model_error", message=str(err))
        result = {"ok": False, "error": "model_error", "message": str(err)}
    except Exception as err:  # the UI must hear about a crash, not wait forever
        session.log("crash", message=str(err), trace=traceback.format_exc()[-1500:])
        result = {"ok": False, "error": "crash", "message": str(err)}
    with _runs_lock:
        _runs[session.session_id] = {"state": "done", "result": result}


class Handler(BaseHTTPRequestHandler):
    server_version = "travel-planner"

    def log_message(self, fmt, *args) -> None:  # keep the terminal quiet
        pass

    # --- helpers ---------------------------------------------------------
    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _session(self, session_id: str | None):
        from .__main__ import Session

        if not session_id or not SESSION_ID.fullmatch(session_id):
            return None
        return Session(session_id)

    # --- routes ----------------------------------------------------------
    def do_GET(self) -> None:
        route = urlparse(self.path).path
        if route == "/":
            body = UI_FILE.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == "/api/sessions":
            self._sessions()
        elif route == "/api/events":
            self._events()
        elif route == "/api/artifact":
            self._artifact()
        elif route == "/api/session":
            self._session_view()
        elif route == "/api/mcp/tools":
            self._mcp_tools()
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route not in {"/api/execute", "/api/mcp/call"}:
            return self._json({"error": "not_found"}, 404)
        try:
            length = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(min(length, 100_000)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad_json"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "bad_json"}, 400)
        if route == "/api/mcp/call":
            return self._mcp_call(payload)
        task = str(payload.get("task") or "").strip()
        session_id = str(payload.get("session_id") or "").strip() or time.strftime("trip-%Y%m%d-%H%M%S")
        session = self._session(session_id)
        if session is None:
            return self._json({"error": "bad_session_id", "message": "letters, digits, '.', '_' and '-' only"}, 400)
        if not task:
            return self._json({"error": "empty_task", "message": "describe the trip"}, 400)
        if session.is_closed():
            return self._json({"error": "session_closed", "message": f"session {session_id} is closed"}, 409)
        with _runs_lock:
            if _runs.get(session_id, {}).get("state") == "running":
                return self._json({"error": "busy", "message": "this session is already planning"}, 409)
            _runs[session_id] = {"state": "running", "result": None}
        start = len(session.entries())
        if not session.exists():
            session.log("session_opened")
        session.log("execute", task=task[:2000])
        threading.Thread(target=_run, args=(session, task), daemon=True).start()
        self._json({"ok": True, "session_id": session_id, "from": start})

    def _sessions(self) -> None:
        from .__main__ import SESSION_ROOT

        out = []
        if SESSION_ROOT.is_dir():
            for path in sorted(SESSION_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
                state_file = path / "state.json"
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
                except json.JSONDecodeError:
                    state = {}
                out.append({
                    "session_id": path.name,
                    "turns": state.get("turn", 0),
                    "status": state.get("status"),
                    "last_goal": state.get("last_goal"),
                    "total_usd": state.get("last_total_usd"),
                    "running": _runs.get(path.name, {}).get("state") == "running",
                })
        self._json({"sessions": out})

    def _events(self) -> None:
        q = self._query()
        session = self._session(q.get("session_id"))
        if session is None:
            return self._json({"error": "bad_session_id"}, 400)
        try:
            sent = max(0, int(q.get("from", "0")))
        except ValueError:
            sent = 0
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def emit(event: str, data) -> None:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        try:
            idle = 0.0
            while True:
                # State first, log second: a turn that finishes between the
                # two reads has already written its last lines, so none are missed.
                with _runs_lock:
                    run = _runs.get(session.session_id)
                entries = session.entries()
                for entry in entries[sent:]:
                    emit("log", entry)
                sent = max(sent, len(entries))
                if not run or run["state"] == "done":
                    emit("result", run["result"] if run else self._replay(session))
                    return
                time.sleep(0.3)
                idle += 0.3
                if idle >= 15:  # a comment line keeps proxies and browsers from timing out
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    idle = 0.0
        except (BrokenPipeError, ConnectionResetError):
            return

    def _mcp_tools(self) -> None:
        servers = []
        for name in mcp.declared():
            try:
                started = time.monotonic()
                tools = mcp.server(name).tools()
                servers.append({"name": name, "ms": round((time.monotonic() - started) * 1000), "tools": tools})
            except mcp.McpError as err:
                servers.append({"name": name, "error": str(err), "tools": []})
        self._json({"servers": servers})

    def _mcp_call(self, payload: dict) -> None:
        name, tool, arguments = payload.get("server"), payload.get("tool"), payload.get("arguments", {})
        if name not in mcp.declared():
            return self._json({"error": "unknown_server", "message": f"no MCP server named {name!r} in .mcp.json"}, 400)
        if not isinstance(arguments, dict):
            return self._json({"error": "bad_arguments", "message": "arguments must be a JSON object"}, 400)
        try:
            known = {t["name"] for t in mcp.server(name).tools()}
        except mcp.McpError as err:
            return self._json({"error": "mcp_error", "message": str(err)}, 502)
        if tool not in known:
            return self._json({"error": "unknown_tool", "message": f"{name} has no tool {tool!r}"}, 400)
        started = time.monotonic()
        result = mcp.server(name).call(tool, arguments)
        self._json({"ok": "error" not in result, "server": name, "tool": tool, "arguments": arguments,
                    "ms": round((time.monotonic() - started) * 1000), "result": result})

    def _session_view(self) -> None:
        """One trip, as the page draws it. Read from the session's own files, never from memory."""
        session = self._session(self._query().get("session_id"))
        if session is None or not session.exists():
            return self._json({"error": "no_session"}, 404)
        state = session.read_json("state.json", {})
        state = state if isinstance(state, dict) else {}

        def latest(pattern: str):
            files = sorted(session.artifacts_dir.glob(pattern), key=lambda f: f.stat().st_mtime) if session.artifacts_dir.is_dir() else []
            return files[-1] if files else None

        def load(path):
            try:
                return json.loads(path.read_text(encoding="utf-8")) if path else None
            except json.JSONDecodeError:
                return None

        trip, itinerary, booking, cancellation = latest("trip-*.json"), latest("itinerary-*.md"), latest("booking-*.json"), latest("cancellation-*.json")
        with _runs_lock:
            running = _runs.get(session.session_id, {}).get("state") == "running"
        self._json({
            "session_id": session.session_id,
            "running": running,
            "closed": session.is_closed(),
            "turns": state.get("turn", 0),
            "status": state.get("status"),
            "last_goal": state.get("last_goal"),
            "last_answer": state.get("last_answer"),
            "metrics": state.get("metrics"),
            "plan_problems": (state.get("plan") or {}).get("problems", []),
            "booking": planner._booking_summary(state),
            "trip": load(trip),
            "booking_record": load(booking),
            "cancellation_record": load(cancellation),
            "itinerary_md": itinerary.read_text(encoding="utf-8") if itinerary else None,
            "files": [f.name for f in (trip, itinerary, booking, cancellation) if f],
        })

    @staticmethod
    def _replay(session) -> dict | None:
        """For a session this server did not run: the saved state, in the shape of a result."""
        state = session.read_json("state.json", None)
        if not isinstance(state, dict):
            return None
        plan = state.get("plan") or {}
        return {"ok": True, "replay": True, "status": state.get("status"), "answer": "",
                "problems": plan.get("problems", []), "booking": planner._booking_summary(state)}

    def _artifact(self) -> None:
        q = self._query()
        session = self._session(q.get("session_id"))
        name = Path(q.get("name") or "").name
        if session is None or not name:
            return self._json({"error": "bad_request"}, 400)
        path = (session.artifacts_dir / name).resolve()
        if path.parent != session.artifacts_dir.resolve() or not path.is_file():
            return self._json({"error": "not_found"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("content-type", "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8765) -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    print(f"travel-planner UI on http://127.0.0.1:{port}  (Ctrl+C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        mcp.close_all()
    return 0
