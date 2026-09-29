"""stdio MCP server `harness`: JSON-RPC 2.0, one JSON message per line on stdin/stdout.

Run: python3 <HARNESS_HOME>/lib/agent_harness/mcp/server.py
Nothing but protocol messages is ever written to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

if __package__ in (None, ""):  # executed as a script: make `agent_harness` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness.mcp.kb import KB  # type: ignore
    from agent_harness.mcp.memory import Memory  # type: ignore
    from agent_harness.mcp.state import State  # type: ignore
else:
    from .kb import KB
    from .memory import Memory
    from .state import State

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "harness", "version": "0.1.0"}
INSTRUCTIONS = (
    "Partial retrieval: never read whole documents. Call kb_index once to see every section id "
    "(or kb_search <words>), then kb_get only the ids you need; every result reports its bytes. "
    "Check lesson_search before a risky task, lesson_add after a failure or correction, "
    "mem_add for durable facts the user states."
)


def _s(**props) -> dict:
    return {"type": "object", "properties": props,
            "required": [k for k, v in props.items() if v.pop("_req", False)]}


TOOLS = [
    {"name": "kb_index",
     "description": "Compact list of every knowledge section (`id — title`, grouped by page); read it once, "
                    "then kb_get only the ids you need.",
     "inputSchema": _s(max_bytes={"type": "integer", "default": 8000})},
    {"name": "kb_search",
     "description": "BM25 search over the knowledge base returning ids, one-line snippets and bytes; search "
                    "first, then kb_get only the ids you need.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 5},
                       min_score={"type": "number", "description": "0..1; weaker matches are dropped"})},
    {"name": "kb_get",
     "description": "Fetch ONE section by id (from kb_index/kb_search), truncated at max_bytes; never fetch "
                    "sections you have not chosen.",
     "inputSchema": _s(id={"type": "string", "_req": True},
                       max_bytes={"type": "integer", "default": 8000})},
    {"name": "kb_toc",
     "description": "Headings tree (ids, titles, bytes; no bodies) of one page, or with no path a one-line-"
                    "per-page list; use it to pick sections, then kb_get them.",
     "inputSchema": _s(path={"type": "string", "default": ""})},
    {"name": "mem_add",
     "description": "Remember one short durable fact (near-duplicates are merged, not repeated); store facts, "
                    "not transcripts.",
     "inputSchema": _s(text={"type": "string", "_req": True},
                       tags={"type": "array", "items": {"type": "string"}, "default": []},
                       scope={"type": "string", "enum": ["user", "project"], "default": "user"},
                       pin={"type": "boolean", "default": False,
                            "description": "only when the user explicitly asks: shown at every session start"})},
    {"name": "mem_search",
     "description": "Find remembered facts relevant to the task (weak matches return nothing); ask for a few "
                    "(k) rather than many.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 5},
                       min_score={"type": "number"})},
    {"name": "mem_forget",
     "description": "Forget a remembered fact or lesson by id when it is wrong or stale (it is archived, "
                    "not deleted).",
     "inputSchema": _s(id={"type": "string", "_req": True})},
    {"name": "lesson_add",
     "description": "Record a lesson right after a failure or a user correction: what went wrong, the fix, "
                    "and the situation that should trigger recalling it.",
     "inputSchema": _s(mistake={"type": "string", "_req": True}, fix={"type": "string", "_req": True},
                       trigger={"type": "string", "_req": True})},
    {"name": "lesson_search",
     "description": "Before a non-trivial or risky step, look up past lessons that match what you are about "
                    "to do.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 3},
                       min_score={"type": "number"})},
    {"name": "state_save",
     "description": "Save this project's working state so nothing is lost when context compacts (<=4096 bytes, "
                    "replaces the last save); use sections: Goal / Decisions / Done / Next / Open questions / "
                    "Key files.",
     "inputSchema": _s(text={"type": "string", "_req": True}, project={"type": "string", "default": ""})},
    {"name": "state_load",
     "description": "Read this project's saved working state (after compaction or at session start) before "
                    "re-deriving anything.",
     "inputSchema": _s(project={"type": "string", "default": ""})},
    {"name": "session_note",
     "description": "At the end of a session, append one line (<=300 chars) saying what was done and what is "
                    "next.",
     "inputSchema": _s(summary={"type": "string", "_req": True})},
    {"name": "session_recent",
     "description": "The last k session notes for this project; a cheap way to see recent history.",
     "inputSchema": _s(k={"type": "integer", "default": 5})},
]


class Server:
    def __init__(self, home=None, kb_paths=None, fts: Optional[bool] = None):
        self._home, self._kb_paths, self._fts = home, kb_paths, fts
        self._kb: Optional[KB] = None
        self._mem: Optional[Memory] = None
        self._state: Optional[State] = None

    @property
    def kb(self) -> KB:
        if self._kb is None:
            self._kb = KB(home=self._home, paths=self._kb_paths, fts=self._fts)
        return self._kb

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = Memory(home=self._home, fts=self._fts)
        return self._mem

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = State(home=self._home)
        return self._state

    # ------------------------------------------------------------ tools
    def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Run a tool in-process and return its Python result."""
        a = dict(args or {})
        table: Dict[str, Callable[[], Any]] = {
            "kb_index": lambda: self.kb.build_index(int(a.get("max_bytes", 8000))),
            "kb_search": lambda: self.kb.search(a["query"], int(a.get("k", 5)), a.get("min_score")),
            "kb_get": lambda: self.kb.get(a["id"], int(a.get("max_bytes", 8000))),
            "kb_toc": lambda: self.kb.toc(a.get("path", "") or ""),
            "mem_add": lambda: self.mem.mem_add(a["text"], a.get("tags") or [], a.get("scope", "user"),
                                                bool(a.get("pin", False))),
            "mem_search": lambda: self.mem.mem_search(a["query"], int(a.get("k", 5)), a.get("min_score")),
            "mem_forget": lambda: self.mem.mem_forget(a["id"]),
            "lesson_add": lambda: self.mem.lesson_add(a["mistake"], a["fix"], a.get("trigger", "")),
            "lesson_search": lambda: self.mem.lesson_search(a["query"], int(a.get("k", 3)), a.get("min_score")),
            "state_save": lambda: self.state.state_save(a["text"], a.get("project", "") or ""),
            "state_load": lambda: self.state.state_load(a.get("project", "") or ""),
            "session_note": lambda: self.state.session_note(a["summary"]),
            "session_recent": lambda: self.state.session_recent(int(a.get("k", 5))),
        }
        if name not in table:
            raise LookupError(name)
        return table[name]()

    @staticmethod
    def render(name: str, result: Any) -> str:
        if name == "kb_get":
            head = "[%s] %s — %d bytes%s" % (result["id"], result["path"], result["bytes"],
                                              ", truncated" if result["truncated"] else "")
            if result.get("next_part"):
                head += "; continues in %s" % result["next_part"]
            return head + "\n" + result["text"]
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    # ------------------------------------------------------------ JSON-RPC
    def handle(self, msg: Any) -> Optional[dict]:
        """Handle one decoded message; returns the response dict, or None for notifications."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to something we never sent; ignore
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        is_note = "id" not in msg
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {
                    "protocolVersion": want if want in PROTOCOL_VERSIONS else LATEST,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    return None if is_note else _err(mid, -32602, "Unknown tool: %s" % name)
                try:
                    out = self.call_tool(name, params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": self.render(name, out)}], "isError": False}
                except (KeyError, ValueError, TypeError, LookupError) as e:
                    text = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
                    if isinstance(e, KeyError) and name != "kb_get":
                        text = "missing argument: %s" % text
                    result = {"content": [{"type": "text", "text": str(text)}], "isError": True}
            elif method.startswith("notifications/"):
                return None
            else:
                return None if is_note else _err(mid, -32601, "Method not found: %s" % method)
        except Exception as e:  # never let one bad call kill the server
            traceback.print_exc(file=sys.stderr)
            return None if is_note else _err(mid, -32603, "Internal error: %s" % e)
        return None if is_note else {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                resp = _err(None, -32700, "Parse error")
            else:
                resp = self.handle(msg)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
                stdout.flush()


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main() -> None:
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    Server().serve()


if __name__ == "__main__":
    main()
