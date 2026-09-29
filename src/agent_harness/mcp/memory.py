"""Memory and lessons (self-learning).

Source of truth: one small JSON file per item, human-readable and hand-editable:
    <HARNESS_HOME>/memory/<scope>/<id>.json      scope = "user" | "project"
    <HARNESS_HOME>/lessons/<id>.json
Archived items move to an ``archive/`` subfolder (never deleted). The files are mirrored into a
separate table (+ FTS5 table when available) in the shared ``index.sqlite``, synced by mtime.

CLI (session-start and per-prompt hooks; both print nothing when there is nothing to say):
    python3 -m agent_harness.mcp.memory digest [--project DIR] [--max-bytes 2048]
    python3 -m agent_harness.mcp.memory recall --session ID [--max-bytes 1200] [--project DIR] < prompt
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness.mcp import kb as _kb  # type: ignore
    from agent_harness.mcp.state import State, project_root  # type: ignore
else:
    from . import kb as _kb
    from .state import State, project_root

CAPS = {"memory": 500, "lessons": 200}
DUP_JACCARD = 0.8
SCOPES = ("user", "project")
RECALL_TTL_DAYS = 7
# Relevance threshold on the normalised score (kb.relevance with doc_coverage=True), calibrated on the
# memory fixtures in tests/test_mcp.py (FTS5 and pure-Python give identical scores):
#   unrelated prompts ("write a haiku about autumn leaves", "what is the capital of France")  0.00
#   prompts sharing one generic word ("the second test failed", "update the config file
#     for the server" against facts about tests / servers)                                   0.14-0.15
#   paraphrases ("which port does the staging db listen on?", "set up kaggle auth token",
#     "can you fix the network route on the VPN")                                             0.25-0.29
#   close restatements ("please make the tests pass", "how do I deploy a release")            0.48-0.61
# 0.2 sits in the gap between the second and third rows.
MEM_MIN_SCORE = 0.2


def _norm_set(text: str) -> set:
    return set(re.findall(r"[0-9a-zÀ-￿]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    sa, sb = _norm_set(a), _norm_set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / float(len(sa | sb))


class Memory:
    def __init__(self, home: Optional[os.PathLike] = None, fts: Optional[bool] = None,
                 caps: Optional[Dict[str, int]] = None, project: Optional[os.PathLike] = None,
                 db_path: Optional[os.PathLike] = None):
        self.home = _kb.harness_home(home)
        self.fts = _kb.fts5_available() if fts is None else (fts and _kb.fts5_available())
        self.caps = dict(CAPS, **(caps or {}))
        self.project = str(project_root(project))
        self.con = _kb.connect(Path(db_path) if db_path else self.home / "index.sqlite")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS mem_items(
                rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, kind TEXT, scope TEXT, project TEXT,
                text TEXT, data TEXT, uses INTEGER, updated REAL, file TEXT, mtime REAL);
        """)
        if self.fts:
            self.con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5("
                             "text, tags, tokenize='porter unicode61')")
        self.con.commit()

    # ------------------------------------------------------------ files
    def _dir(self, kind: str, scope: str = "user") -> Path:
        return self.home / "memory" / scope if kind == "memory" else self.home / "lessons"

    def _dirs(self):
        for s in SCOPES:
            yield "memory", s, self._dir("memory", s)
        yield "lesson", "", self._dir("lessons")

    @staticmethod
    def _text(kind: str, item: dict) -> str:
        if kind == "lesson":
            return "%s\n%s\n%s" % (item.get("trigger", ""), item.get("mistake", ""), item.get("fix", ""))
        return item.get("text", "")

    def _write(self, path: Path, item: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(item, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))

    def sync(self) -> None:
        """Mirror the JSON files into SQLite: (re)index new/changed files, drop vanished ones."""
        c = self.con
        known = {r["file"]: (r["rowid"], r["mtime"]) for r in c.execute("SELECT rowid, file, mtime FROM mem_items")}
        seen = set()
        for kind, scope, d in self._dirs():
            if not d.is_dir():
                continue
            for f in d.glob("*.json"):
                if is_conflict(f):  # `<id>.conflict-<host>.json` from sync: never an item
                    continue
                key = str(f)
                seen.add(key)
                mt = f.stat().st_mtime
                k = known.get(key)
                if k and k[1] == mt:
                    continue
                try:
                    item = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                self._upsert(kind, scope, item, key, mt, k[0] if k else None)
        for key, (rowid, _) in known.items():
            if key not in seen:
                self._unindex(rowid)
        c.commit()

    def _upsert(self, kind, scope, item, key, mt, rowid) -> None:
        c = self.con
        if rowid is not None:
            self._unindex(rowid)
        old = c.execute("SELECT rowid FROM mem_items WHERE id=?", (item["id"],)).fetchone()
        if old:
            self._unindex(old["rowid"])
        text = self._text(kind, item)
        cur = c.execute(
            "INSERT INTO mem_items(id,kind,scope,project,text,data,uses,updated,file,mtime) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (item["id"], kind, scope, item.get("project", ""), text, json.dumps(item),
             int(item.get("uses", 0)), float(item.get("updated", item.get("created", 0))), key, mt))
        if self.fts:
            c.execute("INSERT INTO mem_fts(rowid,text,tags) VALUES (?,?,?)",
                      (cur.lastrowid, text, " ".join(item.get("tags", []))))

    def _unindex(self, rowid: int) -> None:
        if self.fts:
            self.con.execute("DELETE FROM mem_fts WHERE rowid=?", (rowid,))
        self.con.execute("DELETE FROM mem_items WHERE rowid=?", (rowid,))

    def _load(self, id: str):
        r = self.con.execute("SELECT * FROM mem_items WHERE id=?", (id,)).fetchone()
        if not r:
            return None, None
        return r, json.loads(r["data"])

    def _bump(self, row, item) -> dict:
        item["uses"] = int(item.get("uses", 0)) + 1
        item["updated"] = time.time()
        p = Path(row["file"])
        self._write(p, item)
        self._upsert(row["kind"], row["scope"], item, str(p), p.stat().st_mtime, row["rowid"])
        return item

    # ------------------------------------------------------------ add / dedupe / caps
    def _visible(self, kind: str, scope: Optional[str] = None, a: str = ""):
        """SQL filter + params: this kind; project-scope items only for the current project."""
        if kind == "lesson":
            return "%skind='lesson'" % a, []
        sql = "{a}kind='memory' AND ({a}scope='user' OR {a}project=?)".format(a=a)
        params = [self.project]
        if scope:
            sql += " AND %sscope=?" % a
            params.append(scope)
        return sql, params

    def _add(self, kind: str, scope: str, item: dict) -> dict:
        self.sync()
        text = self._text(kind, item)
        vis, params = self._visible(kind, scope or None)
        for r in self.con.execute("SELECT * FROM mem_items WHERE " + vis, params).fetchall():
            if jaccard(text, r["text"]) > DUP_JACCARD:
                existing = json.loads(r["data"])
                self._bump(r, existing)
                self.con.commit()
                return {"id": existing["id"], "duplicate": True, "uses": existing["uses"]}
        prefix = "m-" if kind == "memory" else "l-"
        now = time.time()
        item.update({"id": prefix + uuid.uuid4().hex[:12], "created": now, "updated": now, "uses": 0})
        path = self._dir("memory" if kind == "memory" else "lessons", scope or "user") / (item["id"] + ".json")
        self._write(path, item)
        self._upsert(kind, scope, item, str(path), path.stat().st_mtime, None)
        archived = self._enforce_cap(kind, scope)
        self.con.commit()
        return {"id": item["id"], "duplicate": False, "archived": archived}

    def _enforce_cap(self, kind: str, scope: str) -> List[str]:
        """Archive the least-used, oldest items beyond the cap; pinned items go last."""
        cap = self.caps["memory" if kind == "memory" else "lessons"]
        where, params = ("kind='lesson'", []) if kind == "lesson" else ("kind='memory' AND scope=?", [scope])
        rows = self.con.execute("SELECT * FROM mem_items WHERE %s ORDER BY uses ASC, updated ASC" % where,
                                params).fetchall()
        rows.sort(key=lambda r: bool(json.loads(r["data"]).get("pinned")))  # stable: keeps uses/age order
        out = []
        for r in rows[: max(0, len(rows) - cap)]:
            self._archive(r)
            out.append(r["id"])
        return out

    def _archive(self, row, reason: str = "cap") -> None:
        src = Path(row["file"])
        item = json.loads(row["data"])
        item["archived"] = {"at": time.time(), "reason": reason}
        dst = src.parent / "archive" / src.name
        self._write(dst, item)
        if src.exists():
            src.unlink()
        self._unindex(row["rowid"])

    # ------------------------------------------------------------ search
    @staticmethod
    def _toks(row) -> List[str]:
        return _kb.tokens(row["text"] + " " + " ".join(json.loads(row["data"]).get("tags", [])))

    def scored(self, kind: str, query: str, min_score: Optional[float] = None) -> List[Tuple[float, object]]:
        """(score, row) for visible items scoring >= min_score, best first. Does not touch `uses`.

        Corpus statistics come from all visible items of this kind (<= the cap, so cheap); FTS5, when
        present, only narrows the candidates.
        """
        self.sync()
        min_score = MEM_MIN_SCORE if min_score is None else float(min_score)
        qt = _kb.query_terms(query or "")
        if not qt:
            return []
        vis, params = self._visible(kind)
        rows = self.con.execute("SELECT * FROM mem_items WHERE " + vis, params).fetchall()
        if not rows:
            return []
        toks = {r["rowid"]: self._toks(r) for r in rows}
        df: Dict[str, int] = {}
        for t in toks.values():
            for w in set(t):
                df[w] = df.get(w, 0) + 1
        n, avgdl = len(rows), sum(len(t) for t in toks.values()) / float(len(rows))
        cand = rows
        if self.fts:
            hit = {r[0] for r in self.con.execute("SELECT rowid FROM mem_fts WHERE mem_fts MATCH ?",
                                                  (_kb.fts_query(qt.values()),))}
            cand = [r for r in rows if r["rowid"] in hit]
        out = [(_kb.relevance(qt, toks[r["rowid"]], n, avgdl, df, doc_coverage=True), r) for r in cand]
        return sorted((x for x in out if x[0] >= min_score), key=lambda x: -x[0])

    def _search(self, kind: str, query: str, k: int, min_score: Optional[float] = None) -> List[dict]:
        k = max(1, min(int(k), 50))
        out = [self._bump(r, json.loads(r["data"])) for _, r in self.scored(kind, query, min_score)[:k]]
        self.con.commit()
        return out

    # ------------------------------------------------------------ tools
    def mem_add(self, text: str, tags: Sequence[str] = (), scope: str = "user", pin: bool = False) -> dict:
        if scope not in SCOPES:
            raise ValueError("scope must be 'user' or 'project'")
        if not text or not text.strip():
            raise ValueError("text is empty")
        item = {"text": text.strip(), "tags": [str(t) for t in tags], "pinned": bool(pin)}
        if scope == "project":
            item["project"] = self.project
        res = self._add("memory", scope, item)
        if pin and res.get("duplicate"):  # pinning an existing fact pins it
            r, existing = self._load(res["id"])
            if r is not None and not existing.get("pinned"):
                existing["pinned"] = True
                p = Path(r["file"])
                self._write(p, existing)
                self._upsert(r["kind"], r["scope"], existing, str(p), p.stat().st_mtime, r["rowid"])
                self.con.commit()
        return res

    def mem_search(self, query: str, k: int = 5, min_score: Optional[float] = None) -> List[dict]:
        return [{"id": i["id"], "text": i["text"], "tags": i.get("tags", []), "uses": i["uses"]}
                for i in self._search("memory", query, k, min_score)]

    def mem_forget(self, id: str) -> dict:
        """Forgotten items are archived (moved to archive/), never deleted; the index drops them."""
        self.sync()
        r = self.con.execute("SELECT * FROM mem_items WHERE id=?", (id,)).fetchone()
        if not r:
            return {"ok": False, "error": "no item %r" % id}
        self._archive(r, reason="forgotten")
        self.con.commit()
        return {"ok": True}

    def lesson_add(self, mistake: str, fix: str, trigger: str) -> dict:
        if not (mistake.strip() and fix.strip()):
            raise ValueError("mistake and fix are required")
        return self._add("lesson", "", {"mistake": mistake.strip(), "fix": fix.strip(),
                                        "trigger": trigger.strip(), "project": self.project})

    def lesson_search(self, query: str, k: int = 3, min_score: Optional[float] = None) -> List[dict]:
        return [{"id": i["id"], "trigger": i.get("trigger", ""), "mistake": i["mistake"], "fix": i["fix"],
                 "uses": i["uses"]} for i in self._search("lesson", query, k, min_score)]

    # ------------------------------------------------------------ session hooks
    def digest(self, max_bytes: int = 2048) -> str:
        """Session-start digest: pinned facts, a one-line count, the open-work pointer. Never bumps uses.

        Only explicitly pinned items are printed; everything else is recalled when relevant."""
        self.sync()
        vis, params = self._visible("memory")
        facts = [json.loads(r["data"]) for r in
                 self.con.execute("SELECT * FROM mem_items WHERE %s ORDER BY updated ASC" % vis, params)]
        n_lessons = self.con.execute("SELECT COUNT(*) FROM mem_items WHERE kind='lesson'").fetchone()[0]
        pinned = [i for i in facts if i.get("pinned") and not i.get("project")]
        pinned += [i for i in facts if i.get("pinned") and i.get("project")]
        st = State(home=self.home, project=self.project).state_path()
        if not facts and not n_lessons and st is None:
            return ""
        lines = ["Memory (harness):\n"]
        for i in pinned:
            lines.append("- %s%s\n" % ("[project] " if i.get("project") else "", _one_line(i["text"])))
        if facts or n_lessons:
            lines.append("%d facts, %d lessons stored; they are recalled when relevant (mem_search, "
                         "lesson_search).\n" % (len(facts), n_lessons))
        if st is not None:
            lines.append("open work: STATE.md saved %s (state_load reads it)\n"
                         % _dt.datetime.fromtimestamp(st.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
        return _fit(lines, max_bytes)

    def recall(self, prompt: str, session: str, max_bytes: int = 1200,
               min_score: Optional[float] = None) -> str:
        """Memories and lessons relevant to `prompt`, most relevant first, within max_bytes, skipping
        ids already recalled in this session. Empty string when nothing is relevant."""
        sdir = self.home / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - RECALL_TTL_DAYS * 86400
        for f in sdir.glob("recalled-*.txt"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        sid = re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:120] or "default"
        seen_f = sdir / ("recalled-%s.txt" % sid)
        seen = set(seen_f.read_text(encoding="utf-8").split()) if seen_f.exists() else set()
        hits = [(sc, r) for kind in ("memory", "lesson") for sc, r in self.scored(kind, prompt, min_score)
                if r["id"] not in seen]
        hits.sort(key=lambda x: -x[0])
        if not hits:
            return ""
        lines, used = ["Recalled from harness memory (relevant to this prompt):\n"], []
        size = len(lines[0].encode("utf-8"))
        for _, r in hits:
            i = json.loads(r["data"])
            if r["kind"] == "lesson":
                line = "- lesson: when %s -> %s (mistake was: %s)\n" % (
                    _one_line(i.get("trigger", "") or "relevant", 120), _one_line(i["fix"], 200),
                    _one_line(i["mistake"], 160))
            else:
                line = "- %s\n" % _one_line(i["text"])
            b = len(line.encode("utf-8"))
            if size + b > max_bytes:
                continue
            lines.append(line)
            size += b
            used.append(r)
        if not used:
            return ""
        for r in used:
            self._bump(r, json.loads(r["data"]))
        self.con.commit()
        with open(seen_f, "a", encoding="utf-8") as f:
            f.write("".join(r["id"] + "\n" for r in used))
        return "".join(lines)

    def close(self) -> None:
        self.con.close()


def is_conflict(path: Path) -> bool:
    return ".conflict-" in path.name


def _one_line(text: str, limit: int = 300) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _fit(lines: List[str], max_bytes: int) -> str:
    """Join lines in order, stopping before the byte cap is exceeded (a hard cap)."""
    out, size = [], 0
    for line in lines:
        b = len(line.encode("utf-8"))
        if size + b > max_bytes:
            break
        out.append(line)
        size += b
    return "".join(out) if len(out) > 1 else ""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m agent_harness.mcp.memory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest", help="session-start digest (pinned facts, counts, open work)")
    d.add_argument("--project", default="")
    d.add_argument("--max-bytes", type=int, default=2048)
    r = sub.add_parser("recall", help="relevant memories/lessons for the prompt on stdin")
    r.add_argument("--session", required=True)
    r.add_argument("--project", default="")
    r.add_argument("--max-bytes", type=int, default=1200)
    a = ap.parse_args(argv)
    m = Memory(project=a.project or None)
    try:
        if a.cmd == "digest":
            out = m.digest(a.max_bytes)
        else:
            out = m.recall(sys.stdin.read(), a.session, a.max_bytes)
    finally:
        m.close()
    if out:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
