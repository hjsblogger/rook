"""travel-planner, from the command line.

    python3 -m travel_planner execute --session-id S      (the request on stdin)
    python3 -m travel_planner execute --session-id S --task "..."
    python3 -m travel_planner logs    --session-id S
    python3 -m travel_planner close   --session-id S
    python3 -m travel_planner serve   [--port 8765]          (the web UI)

A session is a conversation: a later `execute` with the same id continues it,
so "make it cheaper" after a first plan works. Each command prints one JSON
object on stdout. Diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from . import mcp, planner
from .llm import ModelError

SESSION_ROOT = Path(os.environ.get("PLANNER_STATE_DIR", ".planner-sessions"))


def _load_dotenv() -> None:
    """Read `.env` beside the package, without a dependency. The real environment wins."""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        return


_load_dotenv()


class Session:
    """One planning conversation: its log, its state, its artifacts."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.dir = SESSION_ROOT / session_id
        self.log_path = self.dir / "session.jsonl"
        self.artifacts_dir = self.dir / "artifacts"
        # Specialists run in parallel and all log here.
        self._log_lock = threading.Lock()

    def exists(self) -> bool:
        return self.log_path.exists()

    def is_closed(self) -> bool:
        return any(e.get("event") == "session_closed" for e in self.entries())

    def log(self, event: str, **fields) -> None:
        """One JSON line per event, written as it happens so a crashed session still says how far it got."""
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
        with self._log_lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def entries(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                out.append({"event": "unparseable_log_line", "raw": line[:200]})
        return out

    def write_artifact(self, name: str, content: str) -> Path:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = (self.artifacts_dir / Path(name).name).resolve()
        if path.parent != self.artifacts_dir.resolve():
            raise ValueError("artifact path escapes the session")
        self._atomic_write(path, content)
        return path

    def read_json(self, name: str, default):
        path = self.dir / name
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.log("state_recovery", path=str(path), reason="invalid_json")
            return default

    def write_json(self, name: str, value) -> Path:
        path = self.dir / Path(name).name
        self._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2))
        return path

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)


def _emit(obj: dict, status: int = 0) -> int:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return status


def _fail(code: str, message: str, status: int = 1) -> int:
    return _emit({"ok": False, "error": code, "message": message}, status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="travel-planner")
    parser.add_argument("command", choices=["execute", "logs", "close", "serve"])
    parser.add_argument("--port", type=int, default=8765, help="serve: the local port")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--task", default=None, help="the request; read from stdin when omitted")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .web import serve

        return serve(args.port)

    session_id = args.session_id or time.strftime("trip-%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", session_id):
        return _fail("bad_session_id", "session ids are letters, digits, '.', '_' and '-'", 2)
    session = Session(session_id)

    if args.command == "logs":
        if not session.exists():
            return _fail("no_session", f"no session {session_id}")
        return _emit({"ok": True, "session_id": session_id, "entries": session.entries()})

    if args.command == "close":
        if not session.exists():
            return _fail("no_session", f"no session {session_id}")
        session.log("session_closed")
        return _emit({"ok": True, "session_id": session_id, "closed": True})

    if session.is_closed():
        return _fail("session_closed", f"session {session_id} is closed; start a new one")
    task = args.task if args.task is not None else sys.stdin.read()
    if not task.strip():
        return _fail("empty_task", "give the request with --task or on stdin", 2)

    if not session.exists():
        session.log("session_opened")
    session.log("execute", task=task[:2000])
    try:
        result = planner.run_turn(session, task.strip())
    except ModelError as err:
        session.log("model_error", message=str(err))
        return _fail("model_error", str(err))
    finally:
        mcp.close_all()
    return _emit({"ok": True, "session_id": session_id, **result})


if __name__ == "__main__":
    raise SystemExit(main())
