"""stdio MCP server `harness`: JSON-RPC 2.0, one JSON message per line on stdin/stdout.

Run: python3 <HARNESS_HOME>/lib/agent_harness/mcp/server.py
Nothing but protocol messages is ever written to stdout; diagnostics go to stderr.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

if __package__ in (None, ""):  # executed as a script: make `agent_harness` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness import __version__  # type: ignore
    from agent_harness.mcp import checks  # type: ignore
    from agent_harness.mcp.kb import KB, harness_home  # type: ignore
    from agent_harness.mcp.memory import Memory  # type: ignore
    from agent_harness.mcp import sessions, skills  # type: ignore
    from agent_harness.mcp.state import State  # type: ignore
    from agent_harness.workgraph import tool as workgraph_tool  # type: ignore
else:
    from .. import __version__
    from . import checks
    from .kb import KB, harness_home
    from .memory import Memory
    from . import sessions, skills
    from .state import State
    from ..workgraph import tool as workgraph_tool

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST = PROTOCOL_VERSIONS[0]
SERVER_INFO = {"name": "harness", "version": __version__}
INSTRUCTIONS = (
    "Knowledge: pick a section id from the Knowledge index in your rules and kb_get it (kb_get <page> "
    "lists a page's sections); kb_search only when nothing there fits. Memory: mem_search before answering "
    "about the user's own setup or past decisions; mem_add when told to remember something."
)
# HARNESS_HOME/notices.json = {"notices": ["one line", ...]}: written by whatever installed the harness
# (an organisation's own wrapper, e.g. "AI tools update available (v0.1.0 -> v0.2.0): run `acme ai update`"),
# read here, never fetched. The first pending line joins the instructions of the first session of the
# day (HARNESS_HOME/.notices-mcp-day), so the assistant mentions it once; none pending, nothing is added.
NOTICES = "notices.json"
NOTICES_DAY = ".notices-mcp-day"


def notice_line(home=None, today: Optional[str] = None) -> str:
    """The first pending notice, once a day; '' when none is pending, it was given today, or on any error."""
    hh = harness_home(home)
    try:
        data = json.loads((hh / NOTICES).read_text(encoding="utf-8"))
        notes = [n.strip() for n in data.get("notices", []) if isinstance(n, str) and n.strip()]
    except (OSError, ValueError, AttributeError):
        return ""
    if not notes:
        return ""
    day = today or datetime.date.today().isoformat()
    stamp = hh / NOTICES_DAY
    try:
        if stamp.read_text(encoding="utf-8").strip() == day:
            return ""
    except OSError:
        pass
    try:
        stamp.write_text(day + "\n", encoding="utf-8")
    except OSError:
        pass
    return " ".join(notes[0].split())[:300]


def snapshot_text(home=None) -> str:
    """Arm memory_snapshot (HARNESS_ENABLE=memory_snapshot; off by default: the owner asked for relevance-only
    recall, kept unless measured better): the saved facts, frozen at session start ('' on any error)."""
    if checks.disabled("memory_snapshot"):
        return ""
    try:
        m = Memory(home=home)
        try:
            facts = m.snapshot()
        finally:
            m.close()
    except Exception:
        return ""
    return "\n\nWhat you remember (saved facts, as of this session's start):\n" + facts.rstrip("\n") if facts else ""


def instructions(home=None) -> str:
    text = INSTRUCTIONS + snapshot_text(home)
    line = notice_line(home)
    if not line:
        return text
    return text + "\n\nNotice for the user (tell them once, in one line, then carry on): " + line


def _s(**props) -> dict:
    return {"type": "object", "properties": props,
            "required": [k for k, v in props.items() if v.pop("_req", False)]}


TOOLS = [
    {"name": "kb_search",
     "description": "Search the knowledge base (section ids, snippets, bytes), only when the Knowledge index in "
                    "your rules has no fitting id; then kb_get the ids you need.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 5},
                       min_score={"type": "number", "description": "0..1; weaker matches are dropped"})},
    {"name": "kb_get",
     "description": "Fetch ONE section by id (<page>#<slug>). A page name alone returns its section ids.",
     "inputSchema": _s(id={"type": "string", "_req": True},
                       max_bytes={"type": "integer", "default": 8000})},
    {"name": "kb_toc",
     "description": "Headings tree (ids, titles, bytes) of one page; with no path, the list of pages.",
     "inputSchema": _s(path={"type": "string", "default": ""})},
    {"name": "mem_add",
     "description": "Remember one short durable fact the user stated (near-duplicates are merged).",
     "inputSchema": _s(text={"type": "string", "_req": True},
                       tags={"type": "array", "items": {"type": "string"}, "default": []},
                       scope={"type": "string", "enum": ["user", "project"], "default": "user"},
                       pin={"type": "boolean", "default": False,
                            "description": "only when the user explicitly asks: shown at every session start"})},
    {"name": "mem_search",
     "description": "Find remembered facts about the user's own setup, folders, preferences or past decisions.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 5},
                       min_score={"type": "number"})},
    {"name": "mem_forget",
     "description": "Forget a remembered fact or lesson by id when it is wrong or stale (archived, not deleted).",
     "inputSchema": _s(id={"type": "string", "_req": True})},
    {"name": "lesson_add",
     "description": "After a check failed and then passed, or the user corrected you: the mistake, the fix, "
                    "and when to recall it.",
     "inputSchema": _s(mistake={"type": "string", "_req": True}, fix={"type": "string", "_req": True},
                       trigger={"type": "string", "_req": True})},
    {"name": "lesson_search",
     "description": "Past lessons that match a risky step you are about to take.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 3},
                       min_score={"type": "number"})},
    {"name": "state_save",
     "description": "Save unfinished multi-step work (<=4096 bytes, replaces the last save): goal, stage, "
                    "decisions, next, key files.",
     "inputSchema": _s(text={"type": "string", "_req": True}, project={"type": "string", "default": ""})},
    {"name": "state_load",
     "description": "Read this project's saved working state (after a compaction, or to resume work).",
     "inputSchema": _s(project={"type": "string", "default": ""})},
    {"name": "session_note",
     "description": "Only when files changed: one line (<=300 chars) on what was done and what is next.",
     "inputSchema": _s(summary={"type": "string", "_req": True})},
    {"name": "session_recent",
     "description": "The last k session notes for this project.",
     "inputSchema": _s(k={"type": "integer", "default": 5})},
    {"name": "session_search",
     "description": "Search the user's past conversations (Claude Code, Codex) for what was said or decided "
                    "earlier: dated excerpts.",
     "inputSchema": _s(query={"type": "string", "_req": True}, k={"type": "integer", "default": 5},
                       session={"type": "string", "description": "only this session id (prefix)"})},
    {"name": "skill_manage",
     "description": "Learned methods. create one when finding the working method took several tool calls, or the "
                    "user corrected how; body: ## When to Use, ## Procedure, ## Pitfalls, ## Verification. "
                    "update (body, or old -> new); list, view, archive.",
     "inputSchema": _s(action={"type": "string", "enum": ["create", "update", "list", "view", "archive"],
                               "_req": True},
                       name={"type": "string"}, description={"type": "string"}, body={"type": "string"},
                       old={"type": "string"}, new={"type": "string"})},
    {"name": "plan",
     "description": "Multi-step work as plan/<slug>.md: a node is done only when its gate exits 0. args: "
                    "new <slug> \"<goal>\" | ready | context <slug> <id> | gate <slug> <id> | status | resume.",
     "inputSchema": _s(args={"type": "string", "_req": True}, project={"type": "string", "default": ""})},
    {"name": "run_checks",
     "description": "Run this project's own tests/checks (found automatically, or cmd) and return only the "
                    "failures and the summary (<=2 KB). Use after your last edit.",
     "inputSchema": _s(cmd={"type": "string", "default": "", "description": "a check command, when none is found"},
                       timeout={"type": "integer", "default": 300})},
]


def tool_list() -> list:
    """The advertised tools; run_checks only with HARNESS_ENABLE=run_checks (off by default, see checks.py)."""
    return [t for t in TOOLS if not checks.disabled(t["name"])]


class Server:
    def __init__(self, home=None, kb_paths=None, fts: Optional[bool] = None):
        self._home, self._kb_paths, self._fts = home, kb_paths, fts
        self._kb: Optional[KB] = None
        self._mem: Optional[Memory] = None
        self._state: Optional[State] = None
        self._sessions = None
        self._skills = None
        self._started = time.time()   # sessions that began after this are the one asking: not a past conversation

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

    @property
    def sessions(self):
        if self._sessions is None:
            self._sessions = sessions.Sessions(home=self._home, fts=self._fts)
        return self._sessions

    @property
    def skills(self):
        if self._skills is None:
            self._skills = skills.Skills(home=self._home)
        return self._skills

    def skill_manage(self, a: Dict[str, Any]) -> Any:
        act, name = a.get("action"), (a.get("name") or "").strip()
        if act == "list":
            return self.skills.list()
        if not name:
            raise ValueError("missing argument: name")
        if act == "create":
            return self.skills.create(name, a.get("description") or "", a.get("body") or "")
        if act == "update":
            return self.skills.update(name, a.get("description") or "", a.get("body") or "", a.get("old") or "",
                                      a.get("new") or "")
        if act == "view":
            return self.skills.view(name)
        if act == "archive":
            return self.skills.archive(name)
        raise ValueError("action must be create, update, list, view or archive")

    # ------------------------------------------------------------ tools
    def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        """Run a tool in-process and return its Python result."""
        a = dict(args or {})
        table: Dict[str, Callable[[], Any]] = {
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
            "session_search": lambda: self.sessions.search(a["query"], int(a.get("k", 5)), a.get("session", "") or "",
                                                           before=self._started - 5),
            "skill_manage": lambda: self.skill_manage(a),
            "run_checks": lambda: checks.run_checks(a.get("cmd", "") or "", int(a.get("timeout", 300)),
                                                    home=self._home),
            "plan": lambda: workgraph_tool.run_cli(a.get("args", "") or "", a.get("project", "") or ""),
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
        if name == "session_search":
            return sessions.render(result)
        if name == "skill_manage":
            return skills.render("view" if isinstance(result, str) else "list" if isinstance(result, list) else "",
                                 result)
        if name == "plan":
            return result["output"] if result.get("exit") in (0, None) else "exit %s\n%s" % (
                result["exit"], result["output"])
        if name == "run_checks":
            if result.get("exit") is None:
                return result["output"]
            return "exit %s: %s (%s s, %d bytes of output)\n%s" % (
                result["exit"], result["command"], result.get("seconds"), result.get("output_bytes", 0), result["output"])
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
                    "instructions": instructions(self._home),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_list()}
            elif method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in tool_list()}:
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


SHELL_TOOLS = ("plan", "run_checks")   # both run commands on the machine


def apply_flags(argv) -> None:
    """--no-shell: the tool this server serves has no shell of its own (Claude desktop, Jupyter AI), so the
    tools that run commands are never offered to it, whatever HARNESS_ENABLE says. Handing a chat app a way
    to run gate commands would be a capability it did not have before the harness was installed."""
    if "--no-shell" in argv:
        have = [x for x in os.environ.get("HARNESS_DISABLE", "").split(",") if x.strip()]
        os.environ["HARNESS_DISABLE"] = ",".join(have + [t for t in SHELL_TOOLS if t not in have])


def main() -> None:
    apply_flags(sys.argv[1:])
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    Server().serve()


if __name__ == "__main__":
    main()
