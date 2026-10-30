"""Reaching the travel MCP servers.

The servers the agents use are the ones `.mcp.json` declares, read from that
file rather than repeated here. Anything grading the planner opens the SAME
declarations and can ask `get_fare` or `get_stay` itself, which makes the check
mean something: the planner's answer and the grader's question come from one
source of truth.

JSON-RPC 2.0 over each server's stdin/stdout, no SDK: a dependency here would
put `pip install` between the planner and its first answer.

Replies are read by a background thread rather than waited on with
`select.select`: on Windows `select` only accepts sockets, never a pipe.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


class McpError(RuntimeError):
    """The server could not be started, or would not answer."""


class McpServer:
    """One MCP session against one stdio server, held open for the process.

    Each specialist agent owns one server, and they may run in parallel, so
    every server has its own process and its own lock.
    """

    def __init__(self, name: str, command: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self.name = name
        self._command = command
        self._env = env or {}
        self._cwd = cwd or str(ROOT)
        self._proc: subprocess.Popen[str] | None = None
        self._replies: queue.Queue[str] = queue.Queue()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        self._next_id = 0
        self._timeout = float(os.environ.get("MCP_TIMEOUT", "30"))
        self._lock = threading.Lock()
        self._tools: list[dict[str, Any]] | None = None

    # --- the session -----------------------------------------------------
    def _start(self) -> subprocess.Popen[str]:
        if self._proc and self._proc.poll() is None:
            return self._proc
        command = list(self._command)
        if command and command[0] == "node":
            node = shutil.which("node")
            if not node:
                raise McpError(f"node is not on PATH, and the {self.name} server runs on it")
            command[0] = node
        try:
            self._proc = subprocess.Popen(
                command,
                cwd=self._cwd,
                env={**os.environ, **self._env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # Kept separate: diagnostics on stdout would corrupt the protocol.
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as err:
            raise McpError(f"could not start {self.name}: {err}") from None

        self._replies = queue.Queue()
        self._stderr_tail = collections.deque(maxlen=20)
        threading.Thread(target=self._pump, args=(self._proc.stdout, self._replies.put), daemon=True).start()
        # stderr is drained too: an unread pipe fills up and stalls the server.
        threading.Thread(target=self._pump, args=(self._proc.stderr, self._stderr_tail.append), daemon=True).start()

        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "travel-planner", "version": "1.0.0"},
        })
        self._notify("notifications/initialized")
        return self._proc

    @staticmethod
    def _pump(stream, sink) -> None:
        """Copy lines from one pipe until it closes; "" marks the end."""
        try:
            for line in iter(stream.readline, ""):
                sink(line)
        except (OSError, ValueError):
            pass
        sink("")

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(f"{self.name} is not running")

        self._next_id += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise McpError(f"{self.name} closed its input") from None

        # One request in flight at a time (the lock), and the server sends
        # nothing unsolicited, so the next line is this call's reply.
        try:
            line = self._replies.get(timeout=self._timeout)
        except queue.Empty:
            try:
                proc.kill()
            except OSError:
                pass
            self._proc = None
            raise McpError(f"{self.name} timed out after {self._timeout:g}s") from None
        if not line:
            self._proc = None
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            err = "".join(line for line in self._stderr_tail if line)
            raise McpError(f"{self.name} stopped answering. {err.strip()[:200]}")

        try:
            reply = json.loads(line)
        except json.JSONDecodeError:
            raise McpError(f"{self.name} sent something that is not JSON: {line[:160]}") from None
        if "error" in reply:
            raise McpError(f"{self.name} refused {method}: {reply['error']}")
        return reply.get("result") or {}

    def _notify(self, method: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    # --- what the agents use ---------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        """The server's tools, as MCP describes them. Listed once per process."""
        with self._lock:
            if self._tools is None:
                self._start()
                self._tools = self._rpc("tools/list").get("tools", [])
            return self._tools

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One tool call. Errors come back as data, never as an exception.

        A server that is down is something the agent should be told about so
        it can say so, not a crash that loses the session around it.
        """
        try:
            with self._lock:
                self._start()
                result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        except McpError as err:
            return {"error": str(err)}

        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    out = json.loads(block.get("text") or "{}")
                except json.JSONDecodeError:
                    return {"error": f"{self.name} returned unreadable content"}
                return out if isinstance(out, dict) else {"result": out}
        return {"error": f"{self.name} returned no content"}

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if not proc or proc.poll() is not None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        # The pumps see end of stream once the server exits; close our ends too.
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass


_servers: dict[str, McpServer] = {}
_servers_lock = threading.Lock()


def declared() -> list[str]:
    """The server names `.mcp.json` declares, in file order."""
    try:
        return list(json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers", {}))
    except (OSError, json.JSONDecodeError):
        return []


def server(name: str) -> McpServer:
    """The process-wide session for one server declared in `.mcp.json`."""
    with _servers_lock:
        if name not in _servers:
            config_path = ROOT / ".mcp.json"
            try:
                declared = json.loads(config_path.read_text(encoding="utf-8")).get("mcpServers", {})
            except (OSError, json.JSONDecodeError) as err:
                raise McpError(f"could not read {config_path.name}: {err}") from None
            spec = declared.get(name)
            if not spec:
                raise McpError(f"{config_path.name} declares no server named {name}")
            _servers[name] = McpServer(name, [spec["command"], *spec.get("args", [])], spec.get("env"))
        return _servers[name]


def close_all() -> None:
    with _servers_lock:
        for srv in _servers.values():
            srv.close()
        _servers.clear()
