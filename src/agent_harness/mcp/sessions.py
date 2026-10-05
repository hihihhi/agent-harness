"""session_search: the user's own past conversations, searchable (no LLM, no summaries).

Sources are each tool's own local transcripts, read in place and never modified:
    Claude Code  ~/.claude/projects/<project>/<session>.jsonl   (top level only: subagent files are not sessions)
    Codex        ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
Only the words of the conversation are indexed: the user's messages and the assistant's replies. Tool calls,
tool output, thinking and text the tool injects itself (rules, environment blocks, reminders) are not. Text is
redacted (agent_harness.redact) BEFORE it is stored, so a secret pasted into a chat never reaches the index.

Index: tables in HARNESS_HOME/index.sqlite, filled incrementally by byte offset per transcript (transcripts
only grow; a file that shrank is read again from the start), newest files first, within a time budget per
call, so the first search on a machine with years of history returns at once and the rest follows on the next
calls. Rows of transcripts that no longer exist are dropped. The total stored text is capped
(HARNESS_SESSIONS_MAX_BYTES, default 64 MB): the oldest sessions are dropped first.

Only files the running user owns, found without following symlinks, are read.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness.mcp import kb as _kb  # type: ignore
    from agent_harness.redact import redact  # type: ignore
else:
    from . import kb as _kb
    from ..redact import redact

CAP_BYTES = 64 * 1024 * 1024
MSG_CHARS = 6000          # one message stored at most this long (head kept)
EXCERPT_CHARS = 300
BUDGET_S = 3.0            # indexing time per search call
K_MAX = 10
SOURCES = (("claude-code", ".claude/projects"), ("codex", ".codex/sessions"))
# A closed wrapper the tool itself puts in the user's turn (<environment_context>...</environment_context>,
# <system-reminder>, <command-name>): not the user's words. Codex also sends the rules as "# AGENTS.md instructions".
_WRAPPED = re.compile(r"^\s*<([A-Za-z][\w-]*)[\s>].*</\1>\s*$", re.S)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
_TS = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d):(\d\d):(\d\d)")


def _epoch(ts) -> float:
    m = _TS.match(str(ts or ""))
    if not m:
        return 0.0
    d = _dt.datetime(*map(int, m.group(1).split("-")), int(m.group(2)), int(m.group(3)), int(m.group(4)),
                     tzinfo=_dt.timezone.utc)
    return d.timestamp()


def _clean(text: str, role: str) -> str:
    text = _REMINDER.sub("", text or "").strip()
    if role == "user" and (_WRAPPED.match(text) or text.startswith("# AGENTS.md instructions")):
        return ""
    return text


def claude_messages(e: dict) -> Iterator[Tuple[str, str]]:
    """(role, text) of one Claude Code transcript line: the user's words and the assistant's text only."""
    t = e.get("type")
    if t not in ("user", "assistant") or e.get("isMeta") or e.get("isSidechain"):
        return
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text"]
    else:
        return
    text = _clean("\n".join(parts), t)
    if text:
        yield t, text


def codex_messages(e: dict) -> Iterator[Tuple[str, str]]:
    p = e.get("payload") or {}
    if e.get("type") != "response_item" or p.get("type") != "message" or p.get("role") not in ("user", "assistant"):
        return
    for c in p.get("content") or []:
        if isinstance(c, dict) and c.get("type") in ("input_text", "output_text"):
            text = _clean(c.get("text") or "", p["role"])
            if text:
                yield p["role"], text


class Sessions:
    def __init__(self, home: Optional[os.PathLike] = None, user_home: Optional[os.PathLike] = None,
                 fts: Optional[bool] = None, db_path: Optional[os.PathLike] = None,
                 cap_bytes: Optional[int] = None):
        self.home = _kb.harness_home(home)
        self.user_home = Path(user_home) if user_home else Path.home()
        self.fts = _kb.fts5_available() if fts is None else (fts and _kb.fts5_available())
        env_cap = os.environ.get("HARNESS_SESSIONS_MAX_BYTES")
        self.cap = int(cap_bytes if cap_bytes is not None else (env_cap or CAP_BYTES))
        self.con = _kb.connect(Path(db_path) if db_path else self.home / "index.sqlite")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS sess_files(
                path TEXT PRIMARY KEY, tool TEXT, session TEXT, project TEXT, started REAL,
                offset INTEGER, size INTEGER, mtime REAL, dropped INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sess_msgs(
                rowid INTEGER PRIMARY KEY, path TEXT, ts REAL, role TEXT, text TEXT);
            CREATE INDEX IF NOT EXISTS sess_msgs_path ON sess_msgs(path);
        """)
        if self.fts:
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS sess_fts USING fts5(text, "
                             "tokenize='porter unicode61')")
        self.con.commit()

    # ------------------------------------------------------------ discovery
    def files(self) -> List[Tuple[str, Path, os.stat_result]]:
        """(tool, path, stat) of every transcript the user owns, newest first; symlinks are never followed."""
        uid = os.getuid() if hasattr(os, "getuid") else None
        out = []
        for tool, rel in SOURCES:
            root = self.user_home / rel
            if not root.is_dir() or root.is_symlink():
                continue
            for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
                depth = len(Path(dirpath).relative_to(root).parts)
                if tool == "claude-code":
                    dirnames[:] = [] if depth >= 1 else [d for d in dirnames if not d.startswith(".")]
                    if depth != 1:
                        continue
                    names = [f for f in filenames if f.endswith(".jsonl")]
                else:
                    names = [f for f in filenames if f.startswith("rollout-") and f.endswith(".jsonl")]
                for f in names:
                    p = Path(dirpath) / f
                    try:
                        st = os.lstat(str(p))
                    except OSError:
                        continue
                    if not os.path.isfile(str(p)) or os.path.islink(str(p)):
                        continue
                    if uid is not None and st.st_uid != uid:
                        continue
                    out.append((tool, p, st))
        out.sort(key=lambda x: -x[2].st_mtime)
        return out

    # ------------------------------------------------------------ indexing
    def _drop_rows(self, path: str) -> None:
        if self.fts:
            self.con.execute("DELETE FROM sess_fts WHERE rowid IN (SELECT rowid FROM sess_msgs WHERE path=?)",
                             (path,))
        self.con.execute("DELETE FROM sess_msgs WHERE path=?", (path,))

    def _read(self, tool: str, path: Path, st, row) -> None:
        key = str(path)
        offset = int(row["offset"]) if row else 0
        session, project, started = (row["session"], row["project"], row["started"]) if row else ("", "", 0.0)
        if st.st_size < offset:          # rewritten or truncated: start again
            self._drop_rows(key)
            offset, session, project, started = 0, "", "", 0.0
        if not session and tool == "claude-code":
            session = path.stem
        with open(key, "rb") as f:
            f.seek(offset)
            data = f.read(st.st_size - offset)
        end = data.rfind(b"\n") + 1      # only complete lines; a line being written is read next time
        msgs = []
        for raw in data[:end].splitlines():
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict):
                continue
            if tool == "codex" and e.get("type") == "session_meta":
                p = e.get("payload") or {}
                session = str(p.get("id") or p.get("session_id") or session)
                project = project or str(p.get("cwd") or "")
                continue
            if tool == "claude-code" and not project and e.get("cwd"):
                project = str(e["cwd"])
            it = claude_messages(e) if tool == "claude-code" else codex_messages(e)
            ts = _epoch(e.get("timestamp"))
            for role, text in it:
                started = started or ts
                msgs.append((key, ts or started, role, redact(text)[:MSG_CHARS]))   # cut after: a cut secret no longer matches
        for m in msgs:
            cur = self.con.execute("INSERT INTO sess_msgs(path, ts, role, text) VALUES (?,?,?,?)", m)
            if self.fts:
                self.con.execute("INSERT INTO sess_fts(rowid, text) VALUES (?,?)", (cur.lastrowid, m[3]))
        self.con.execute(
            "INSERT OR REPLACE INTO sess_files(path, tool, session, project, started, offset, size, mtime, dropped) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (key, tool, session or path.stem, project, started or st.st_mtime, offset + end, st.st_size,
             st.st_mtime))

    def sync(self, budget_s: float = BUDGET_S) -> int:
        """Index what changed since the last call, newest transcripts first, for at most budget_s seconds.
        Returns how many transcripts are still waiting to be read."""
        t0 = time.time()
        known = {r["path"]: r for r in self.con.execute("SELECT * FROM sess_files")}
        seen, pending = set(), 0
        for tool, path, st in self.files():
            key = str(path)
            seen.add(key)
            row = known.get(key)
            if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
                continue
            if row and row["dropped"] and st.st_size >= row["size"]:
                continue                  # dropped for the cap: stays out
            if time.time() - t0 > budget_s:
                pending += 1
                continue
            try:
                self.con.commit()
                self.con.execute("BEGIN IMMEDIATE")   # two sessions' servers share the index: one reader per file
                row = self.con.execute("SELECT * FROM sess_files WHERE path=?", (key,)).fetchone()
                if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
                    self.con.commit()
                    continue
                self._read(tool, path, st, row)
            except OSError:
                self.con.rollback()
                continue
            self.con.commit()
        for key in set(known) - seen:     # transcript deleted or moved away: forget it
            self._drop_rows(key)
            self.con.execute("DELETE FROM sess_files WHERE path=?", (key,))
        self._enforce_cap()
        self.con.commit()
        return pending

    def _enforce_cap(self) -> None:
        total = self.con.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM sess_msgs").fetchone()[0]
        if total <= self.cap:
            return
        for r in self.con.execute("SELECT path FROM sess_files WHERE dropped=0 ORDER BY started ASC").fetchall():
            n = self.con.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM sess_msgs WHERE path=?",
                                 (r["path"],)).fetchone()[0]
            self._drop_rows(r["path"])
            self.con.execute("UPDATE sess_files SET dropped=1 WHERE path=?", (r["path"],))
            total -= n
            if total <= self.cap:
                break

    # ------------------------------------------------------------ search
    def search(self, query: str, k: int = 5, session: str = "", before: Optional[float] = None,
               budget_s: float = BUDGET_S) -> dict:
        """Best-matching messages: {"results": [{date, tool, session, project, role, text}], "pending": n}.

        before: ignore sessions that started after this time (the server passes its own start time, so the
        conversation asking is not its own top hit). A message must contain at least half of the query's
        content words (all of them for one or two), so one shared common word is not a match."""
        pending = self.sync(budget_s)
        k = max(1, min(int(k), K_MAX))
        qt = _kb.query_terms(query or "")
        if not qt:
            return {"results": [], "pending": pending}
        need = len(qt) if len(qt) <= 2 else (len(qt) + 1) // 2
        where, params = "", []
        if session:
            where += " AND (f.session=? OR f.session LIKE ?)"
            params += [session, session + "%"]
        if before is not None:
            where += " AND m.ts < ?"      # message time: a resumed old transcript's new messages are the asker's too
            params.append(before)
        if self.fts:
            sql = ("SELECT m.rowid, m.ts, m.role, m.text, f.tool, f.session, f.project FROM sess_fts "
                   "JOIN sess_msgs m ON m.rowid = sess_fts.rowid JOIN sess_files f ON f.path = m.path "
                   "WHERE sess_fts MATCH ?" + where + " ORDER BY bm25(sess_fts) LIMIT 200")
            rows = self.con.execute(sql, [_kb.fts_query(qt.values())] + params).fetchall()
        else:
            like = " OR ".join("m.text LIKE ?" for _ in qt)
            sql = ("SELECT m.rowid, m.ts, m.role, m.text, f.tool, f.session, f.project FROM sess_msgs m "
                   "JOIN sess_files f ON f.path = m.path WHERE (" + like + ")" + where + " ORDER BY m.ts DESC LIMIT 2000")
            rows = self.con.execute(sql, ["%" + w + "%" for w in qt.values()] + params).fetchall()
        scored = []
        for r in rows:
            have = set(_kb.tokens(r["text"]))
            hit = sum(1 for t in qt if t in have)
            if hit >= need:
                scored.append((hit, r["ts"] or 0, r))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        out = []
        for _, _, r in scored[:k]:
            out.append({"date": _dt.datetime.fromtimestamp(r["ts"] or 0).strftime("%Y-%m-%d %H:%M"),
                        "tool": r["tool"], "session": (r["session"] or "")[:8], "project": r["project"] or "",
                        "role": r["role"], "text": _kb.snippet(r["text"], query, EXCERPT_CHARS)})
        return {"results": out, "pending": pending}

    def close(self) -> None:
        self.con.close()


def render(res: dict, home: Optional[Path] = None) -> str:
    """One line per hit: date tool session project role: excerpt (the cheapest form for the model)."""
    home_s = str(home or Path.home())
    lines = []
    for r in res["results"]:
        proj = r["project"].replace(home_s, "~", 1) if r["project"].startswith(home_s) else r["project"]
        lines.append("%s %s %s %s %s: %s" % (r["date"], r["tool"], r["session"], proj or "-", r["role"], r["text"]))
    if not lines:
        lines.append("no past conversation matches")
    if res.get("pending"):
        lines.append("(%d older transcripts not indexed yet; searching again reads more)" % res["pending"])
    return "\n".join(lines)
