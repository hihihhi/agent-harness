"""Knowledge index with partial retrieval.

Markdown/text files are split into sections on headings (# .. ###). Each section has a stable id
``relpath#slug`` (numeric suffix on collision, ``-pN`` for parts of a long section split at ~4 KB).
Search is BM25: SQLite FTS5 when available, a pure-Python BM25 otherwise. The index lives in
``<HARNESS_HOME>/index.sqlite`` and is refreshed incrementally (mtime + size) before every query.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

TEXT_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".rst"}
DESCRIPTOR_BOOST = 1.5   # profile files outrank equal wiki matches (see search())
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "archive"}

def _ok(pred, p) -> bool:
    """Path predicate that treats an unreadable path (PermissionError and friends) as absent."""
    try:
        return pred(p)
    except OSError:
        return False

PART_BYTES = 4096
SNIPPET_CHARS = 200
MAX_FILE_BYTES = 2_000_000

_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_WORD = re.compile(r"[0-9A-Za-z\u00c0-\uffff]+")  # snake_case splits: access_token -> access, token


# ---------------------------------------------------------------- shared helpers

def harness_home(home: Optional[os.PathLike] = None) -> Path:
    """HARNESS_HOME: explicit argument, else env HARNESS_HOME, else ~/.agent-harness."""
    if home is not None:
        return Path(home)
    env = os.environ.get("HARNESS_HOME")
    return Path(env) if env else Path.home() / ".agent-harness"


def fts5_available() -> bool:
    if os.environ.get("HARNESS_NO_FTS"):
        return False
    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        con.close()
        return True
    except sqlite3.Error:
        return False


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return con


_SUFFIXES = ("ations", "ation", "ating", "ated", "ates", "ate", "ions", "ion", "ings", "ing", "ments", "ment",
             "edly", "ed", "ly", "es", "s")
STOPWORDS = frozenset("""a about above after again all am an and any are as at be been before being below
between both but by can could did do does doing down during each few for from further had has have having he
her here hers him his how i if in into is it its itself just me more most my no nor not now of off on once only
or other our ours out over own same she should so some such than that the their theirs them then there these
they this those through to too under until up very was we were what when where which while who whom why will
with would you your yours please can't don't use using used make want need get got let lets like also""".split())


def _stem(w: str) -> str:
    """Tiny suffix stripper (rotate/rotating/rotation -> rot, backups -> backup). Stdlib-only stand-in
    for a real stemmer; the same function stems documents and queries, so it only has to be consistent."""
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    if w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def tokens(text: str) -> List[str]:
    return [_stem(w.lower()) for w in _WORD.findall(text)]


def query_terms(query: str) -> Dict[str, str]:
    """Content terms of a query: {stem: original word}, stopwords and 1-char words dropped."""
    out: Dict[str, str] = {}
    for w in _WORD.findall(query):
        lw = w.lower()
        if len(lw) < 2 or lw in STOPWORDS:
            continue
        out.setdefault(_stem(lw), w)
    return out


def fts_query(words: Iterable[str]) -> str:
    """A safe FTS5 query: every word quoted, OR-ed (BM25 ranks the overlap)."""
    return " OR ".join('"%s"' % w.replace('"', "") for w in words)


K1, B = 1.2, 0.75


def idf(n: int, df: int) -> float:
    return math.log(1 + (n - df + 0.5) / (df + 0.5))


def relevance(qterms: Iterable[str], doc_tokens: List[str], n: int, avgdl: float, df: Dict[str, int],
              doc_coverage: bool = False) -> float:
    """BM25 normalised to 0..1 so one threshold works across corpus sizes.

    q = BM25(doc) / best possible BM25 for these query terms (every term present, tf -> infinity).
    Terms absent from the corpus keep their (maximal) idf in the denominator, so a query that is mostly
    about something the corpus never mentions scores low. A term present once at average length gives 1/(k1+1) = 0.45 of its
    share. With doc_coverage (short items such as memories, matched against long prompts) the score is
    max(q, d), where d = idf mass of the item's own terms that the query mentions / the item's idf mass.
    """
    q = set(qterms)
    if not q or n == 0:
        return 0.0
    tf: Dict[str, int] = {}
    for t in doc_tokens:
        tf[t] = tf.get(t, 0) + 1
    dl = len(doc_tokens)
    norm = 1 - B + B * (dl / avgdl if avgdl else 1)
    best = got = 0.0
    for t in q:
        w = idf(n, df.get(t, 0))
        best += w * (K1 + 1)
        f = tf.get(t, 0)
        if f:
            got += w * f * (K1 + 1) / (f + K1 * norm)
    score = got / best if best else 0.0
    if doc_coverage and tf:
        own = sum(idf(n, df.get(t, 0)) for t in tf if t not in STOPWORDS and len(t) > 1)
        hit = sum(idf(n, df.get(t, 0)) for t in tf if t in q)
        if own:
            score = max(score, hit / own)
    return score


class BM25:
    """Pure-Python Okapi BM25 over (key, tokens) documents. Used when FTS5 is missing."""

    def __init__(self, docs: Sequence[Tuple[object, List[str]]], k1: float = 1.2, b: float = 0.75):
        self.keys = [k for k, _ in docs]
        self.tfs: List[Dict[str, int]] = []
        self.lens: List[int] = []
        df: Dict[str, int] = {}
        for _, toks in docs:
            tf: Dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self.tfs.append(tf)
            self.lens.append(len(toks))
            for t in tf:
                df[t] = df.get(t, 0) + 1
        n = len(docs)
        self.avg = (sum(self.lens) / n) if n else 0.0
        self._df = df
        self.idf = {t: idf(n, d) for t, d in df.items()}
        self.k1, self.b = k1, b

    def df(self, t: str) -> int:
        return self._df.get(t, 0)

    def search(self, query_tokens: Iterable[str], k: int) -> List[Tuple[object, float]]:
        q = set(query_tokens)
        scored = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    norm = 1 - self.b + self.b * (self.lens[i] / self.avg if self.avg else 1)
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * norm)
            if s > 0:
                scored.append((self.keys[i], s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]


def snippet(text: str, query: str, limit: int = SNIPPET_CHARS) -> str:
    """One line, <= limit chars, centred on the first query term found."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= limit:
        return flat
    low = flat.lower()
    pos = -1
    for w in sorted({_stem(w.lower()) for w in _WORD.findall(query)}, key=len, reverse=True):
        if len(w) < 2:
            continue
        m = re.search(r"\b" + re.escape(w), low)
        if m and (pos < 0 or m.start() < pos):
            pos = m.start()
    start = max(0, pos - limit // 3) if pos >= 0 else 0
    body = flat[start: start + limit - 2]
    pre = "…" if start > 0 else ""
    post = "…" if start + len(body) < len(flat) else ""
    return (pre + body + post)[:limit]


# ---------------------------------------------------------------- splitting

def slugify(title: str) -> str:
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)   # a Markdown link counts by its text only
    s = s.strip().lower()
    s = re.sub(r"[`*_\[\]()<>{}!?.,:;'\"/\\|@#$%^&+=~]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s or "section"


def _utf8_cut(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _parts(body: str, limit: int = PART_BYTES) -> List[str]:
    """Split a long section at paragraph (then line, then byte) boundaries into chunks <= ~limit."""
    if len(body.encode("utf-8")) <= limit:
        return [body]
    pieces: List[str] = []
    for para in re.split(r"(?<=\n)\n+", body):
        if len(para.encode("utf-8")) <= limit:
            pieces.append(para)
            continue
        for line in para.splitlines(True):
            while len(line.encode("utf-8")) > limit:
                head = _utf8_cut(line, limit)
                pieces.append(head)
                line = line[len(head):]
            pieces.append(line)
    out, cur = [], ""
    for p in pieces:
        if cur and len((cur + p).encode("utf-8")) > limit:
            out.append(cur)
            cur = ""
        cur += p if not cur or cur.endswith("\n") else "\n" + p
    if cur.strip():
        out.append(cur)
    return out


def split_sections(rel: str, text: str) -> List[dict]:
    """Split Markdown into heading-delimited sections with stable ids."""
    raw: List[Tuple[int, str, List[str]]] = []  # (level, title, lines)
    level, title, lines = 0, Path(rel).stem, []
    in_fence = False
    for line in text.splitlines(True):
        if _FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line.rstrip("\n"))
        if m:
            if lines and "".join(lines).strip():
                raw.append((level, title, lines))
            level, title, lines = len(m.group(1)), m.group(2).strip(), [line]
        else:
            lines.append(line)
    if lines and "".join(lines).strip():
        raw.append((level, title, lines))

    seen: Dict[str, int] = {}
    out = []
    for lvl, ttl, lns in raw:
        base = "top" if lvl == 0 else slugify(ttl)
        n = seen.get(base, 0)
        seen[base] = n + 1
        slug = base if n == 0 else "%s-%d" % (base, n)
        body = "".join(lns).rstrip() + "\n"
        parts = _parts(body)
        for i, part in enumerate(parts):
            sid = "%s#%s" % (rel, slug if i == 0 else "%s-p%d" % (slug, i + 1))
            out.append({
                "id": sid,
                "title": ttl if i == 0 else "%s (part %d)" % (ttl, i + 1),
                "level": lvl,
                "body": part,
                "bytes": len(part.encode("utf-8")),
            })
    return out


# ---------------------------------------------------------------- paths

def _profile_kb_paths(hh: Path) -> List[Path]:
    """kb_paths from <HARNESS_HOME>/profile/profile.toml: ~ expanded, relative ones resolved against the
    profile folder (the same rule as installer.kb_paths, so the install-time index and search agree)."""
    pdir = hh / "profile"
    f = pdir / "profile.toml"
    if not _ok(Path.is_file, f):
        return []
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        import tomllib  # type: ignore  # Python 3.11+
        val = tomllib.loads(text).get("kb_paths", [])
        raw = [str(v) for v in val] if isinstance(val, list) else []
    except ImportError:
        m = re.search(r"^\s*kb_paths\s*=\s*\[(.*?)\]", text, re.S | re.M)
        raw = re.findall(r"[\"']([^\"']+)[\"']", m.group(1)) if m else []
    except Exception:
        return []
    return expand_kb_paths(raw, pdir)


def expand_kb_paths(raw, base: Optional[Path]) -> List[Path]:
    """kb_paths as written in a profile: ~ expanded, relative ones against the profile folder, and a glob
    (`~/.claude/projects/*/memory`) expanded to every match, so a profile shared by accounts and machines names
    a folder whose exact name differs on each (no username written)."""
    import glob as _glob
    out = []
    for v in raw:
        s = os.path.expanduser(str(v))
        if not os.path.isabs(s) and base:
            s = str(Path(base) / s)
        if any(c in s for c in "*?["):
            out += [Path(m) for m in sorted(_glob.glob(s))]
        else:
            out.append(Path(s))
    return out


def _profile_folder(hh: Path) -> List[Path]:
    """The active profile folder (SERVER.md, rules). Its top-level files are roots of their own, so a
    descriptor gets the id `SERVER.md#...` exactly as in the install-time index; subfolders are dir roots."""
    return profile_folder_roots(hh / "profile")


def profile_folder_roots(pdir: Path) -> List[Path]:
    if not _ok(Path.is_dir, pdir):
        return []
    try:
        entries = list(pdir.iterdir())
    except OSError:
        return []
    return sorted(p for p in entries
                  if not p.name.startswith(".") and (_ok(Path.is_dir, p) or p.suffix.lower() in TEXT_SUFFIXES))


def _project_root(start: Path) -> Optional[Path]:
    """The git root at or above `start`, or None. $HOME itself (a dotfiles repo, or no repo at all) is
    never a project: its docs/ are the account's private notes, not this session's project."""
    try:
        start = Path(start).expanduser().resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return None
    for d in (start,) + tuple(start.parents):
        if _ok(lambda x: (x / ".git").exists(), d):
            return None if d == home else d
    return None


def profile_roots(hh: Path) -> List[Path]:
    """The profile's own files and its kb_paths: the roots the install-time index and the server share."""
    return _profile_folder(hh) + _profile_kb_paths(hh)


def default_kb_paths(hh: Path, project: Optional[Path] = None) -> List[Path]:
    """The live roots of a session: the profile's, plus the current project's docs/README/AGENTS.md when
    the session runs inside a git project other than $HOME. Nothing from an unrelated cwd is indexed.
    The harness's own content (rules, skills) is not: every tool already loads it natively."""
    env = os.environ.get("HARNESS_KB_PATHS")
    if env:
        return [Path(os.path.expanduser(p)) for p in env.split(os.pathsep) if p]
    paths = profile_roots(hh)
    root = _project_root(project or Path.cwd())
    if root is not None:
        paths += [root / "docs", root / "README.md", root / "AGENTS.md"]
    return paths


# ---------------------------------------------------------------- the index

CANDIDATES = 50
# Relevance threshold on relevance() (0..1), calibrated on the 40-file fixture in tests/test_mcp.py
# (FTS5 and pure-Python BM25 give identical scores):
#   unrelated ("write a haiku about autumn leaves", "kubernetes pod eviction policy",
#              "the second test failed", "what is the capital of France")        best hit 0.00-0.09
#   distractors sharing words ("## API key storage" for a backup-key question)  0.19-0.25
#   paraphrases ("how do I change the key used to encrypt backups")             0.48-0.66
#   near-verbatim questions                                                      0.70-0.86
# 0.15 drops the unrelated tier and keeps distractors (still useful as top-3 context).
KB_MIN_SCORE = 0.15


def _doc_tokens(title: str, body: str) -> List[str]:
    # Title counted twice: a cheap stand-in for FTS5's column weight.
    return tokens(title) * 2 + tokens(body)


class KB:
    def __init__(self, home: Optional[os.PathLike] = None, paths: Optional[Sequence[os.PathLike]] = None,
                 fts: Optional[bool] = None, db_path: Optional[os.PathLike] = None):
        self.home = harness_home(home)
        self.roots = [Path(p) for p in paths] if paths is not None else default_kb_paths(self.home)
        self.fts = fts5_available() if fts is None else (fts and fts5_available())
        self.db_path = Path(db_path) if db_path else self.home / "index.sqlite"
        self.con = connect(self.db_path)
        self._bm25: Optional[BM25] = None
        self._init_schema()

    def _init_schema(self) -> None:
        c = self.con
        c.executescript("""
            CREATE TABLE IF NOT EXISTS kb_files(path TEXT PRIMARY KEY, rel TEXT, mtime REAL, size INTEGER);
            CREATE TABLE IF NOT EXISTS kb_sections(
                rowid INTEGER PRIMARY KEY, sid TEXT, path TEXT, rel TEXT, title TEXT, level INTEGER,
                ord INTEGER, body TEXT, bytes INTEGER, ntok INTEGER);
            CREATE INDEX IF NOT EXISTS kb_sections_sid ON kb_sections(sid);
            CREATE INDEX IF NOT EXISTS kb_sections_path ON kb_sections(path);
            CREATE TEMP TABLE IF NOT EXISTS kb_live(path TEXT PRIMARY KEY);
        """)
        if "ntok" not in [r[1] for r in c.execute("PRAGMA table_info(kb_sections)")]:
            c.execute("ALTER TABLE kb_sections ADD COLUMN ntok INTEGER DEFAULT 0")
        if self.fts:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5("
                      "title, body, tokenize='porter unicode61')")
        c.commit()

    # -- discovery
    def _walk(self) -> List[Tuple[Path, str]]:
        found: List[Tuple[Path, str]] = []
        used: set = set()
        for root in self.roots:
            if _ok(Path.is_file, root):
                items = [(root, root.name)]
            elif _ok(Path.is_dir, root):
                items = []
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
                    for fn in sorted(filenames):
                        p = Path(dirpath) / fn
                        if p.suffix.lower() in TEXT_SUFFIXES:
                            items.append((p, "%s/%s" % (root.name, p.relative_to(root).as_posix())))
            else:
                continue
            for p, rel in items:
                if rel in used:
                    rel = "%s/%s" % (root.resolve().parent.name, rel)
                used.add(rel)
                found.append((p.resolve(), rel))
        return found

    def refresh(self) -> dict:
        """Reindex files whose mtime/size changed; drop files that disappeared. Returns counts."""
        c = self.con
        found = self._walk()
        known = {r["path"]: (r["mtime"], r["size"], r["rel"]) for r in c.execute("SELECT * FROM kb_files")}
        changed = removed = 0
        live = set()
        for p, rel in found:
            key = str(p)
            live.add(key)
            try:
                st = p.stat()
            except OSError:
                continue
            k = known.get(key)
            if k and k[0] == st.st_mtime and k[1] == st.st_size and k[2] == rel:
                continue
            self._index_file(key, rel, st)
            changed += 1
        for key in known:
            if key not in live and not os.path.exists(key):
                self._drop_file(key)
                removed += 1
        c.execute("DELETE FROM kb_live")
        c.executemany("INSERT OR IGNORE INTO kb_live(path) VALUES (?)", [(k,) for k in live])
        c.commit()
        if changed or removed or self._bm25 is None:
            self._bm25 = None if self.fts else self._build_bm25()
        return {"files": len(live), "changed": changed, "removed": removed}

    def _drop_file(self, key: str) -> None:
        c = self.con
        if self.fts:
            c.execute("DELETE FROM kb_fts WHERE rowid IN (SELECT rowid FROM kb_sections WHERE path=?)", (key,))
        c.execute("DELETE FROM kb_sections WHERE path=?", (key,))
        c.execute("DELETE FROM kb_files WHERE path=?", (key,))

    def _index_file(self, key: str, rel: str, st: os.stat_result) -> None:
        self._drop_file(key)
        if st.st_size > MAX_FILE_BYTES:
            text = ""
        else:
            try:
                with open(key, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:                 # listed but unreadable for this user: index nothing
                text = ""
        c = self.con
        for i, s in enumerate(split_sections(rel, text)):
            cur = c.execute(
                "INSERT INTO kb_sections(sid,path,rel,title,level,ord,body,bytes,ntok) VALUES (?,?,?,?,?,?,?,?,?)",
                (s["id"], key, rel, s["title"], s["level"], i, s["body"], s["bytes"],
                 len(_doc_tokens(s["title"], s["body"]))))
            if self.fts:
                c.execute("INSERT INTO kb_fts(rowid,title,body) VALUES (?,?,?)",
                          (cur.lastrowid, s["title"], s["body"]))
        c.execute("INSERT OR REPLACE INTO kb_files(path,rel,mtime,size) VALUES (?,?,?,?)",
                  (key, rel, st.st_mtime, st.st_size))

    def _build_bm25(self) -> BM25:
        rows = self.con.execute(
            "SELECT s.rowid, s.title, s.body FROM kb_sections s JOIN kb_live l ON l.path=s.path").fetchall()
        return BM25([(r["rowid"], _doc_tokens(r["title"], r["body"])) for r in rows])

    # -- tools
    def search(self, query: str, k: int = 5, min_score: Optional[float] = None) -> List[dict]:
        """Top-k sections scoring >= min_score (normalised 0..1, see relevance()); [] beats weak matches.

        FTS5 (or the Python BM25) proposes up to CANDIDATES sections; each is re-scored with relevance()
        so the threshold means the same thing on both backends.
        """
        self.refresh()
        k = max(1, min(int(k), 50))
        min_score = KB_MIN_SCORE if min_score is None else float(min_score)
        qt = query_terms(query or "")
        if not qt:
            return []
        c = self.con
        if self.fts:
            rows = c.execute(
                "SELECT s.rowid, s.sid, s.title, s.rel, s.body, s.bytes FROM kb_fts f "
                "JOIN kb_sections s ON s.rowid=f.rowid JOIN kb_live l ON l.path=s.path "
                "WHERE kb_fts MATCH ? ORDER BY bm25(kb_fts, 5.0, 1.0) LIMIT ?",
                (fts_query(qt.values()), CANDIDATES)).fetchall()
            st = c.execute("SELECT COUNT(*), AVG(s.ntok) FROM kb_sections s JOIN kb_live l ON l.path=s.path"
                           ).fetchone()
            n, avgdl = st[0], st[1] or 0.0
            df = {t: c.execute("SELECT COUNT(*) FROM kb_fts WHERE kb_fts MATCH ?", (fts_query([w]),)).fetchone()[0]
                  for t, w in qt.items()}
        else:
            bm = self._bm25 or self._build_bm25()
            rows = []
            for rowid, _ in bm.search(qt.keys(), CANDIDATES):
                r = c.execute("SELECT rowid, sid, title, rel, body, bytes FROM kb_sections WHERE rowid=?",
                              (rowid,)).fetchone()
                if r:
                    rows.append(r)
            n, avgdl, df = len(bm.keys), bm.avg, {t: bm.df(t) for t in qt}
        scored = [(relevance(qt, _doc_tokens(r["title"], r["body"]), n, avgdl, df), r) for r in rows]
        # The profile's own files (the machine descriptor, the rules) are the authoritative source for
        # questions about this environment, so among relevant hits they rank first: a wiki page that just
        # repeats "GPU" should not push the descriptor's Machine section out of the top few. The threshold
        # still applies to the unboosted score, so a weak descriptor match is not let through.
        auth = self._authoritative()
        scored = sorted((x for x in scored if x[0] >= min_score),
                        key=lambda x: -(x[0] * (DESCRIPTOR_BOOST if x[1]["rel"] in auth else 1.0)))[:k]
        return [{"id": r["sid"], "title": r["title"], "path": r["rel"],
                 "snippet": snippet(r["body"].split("\n", 1)[-1] if r["body"].startswith("#") else r["body"],
                                    query),
                 "bytes": r["bytes"], "score": round(sc, 2)} for sc, r in scored]

    def _authoritative(self) -> set:
        """Names of the top-level files in the active profile folder (their ids start with the name)."""
        pdir = self.home / "profile"
        try:
            return {p.name for p in pdir.iterdir() if _ok(Path.is_file, p) and p.suffix.lower() in TEXT_SUFFIXES}
        except OSError:
            return set()

    def _row(self, sid: str) -> Optional[sqlite3.Row]:
        r = self.con.execute(
            "SELECT s.* FROM kb_sections s JOIN kb_live l ON l.path=s.path WHERE s.sid=? LIMIT 1",
            (sid,)).fetchone()
        if r is None and sid.startswith("profile/"):  # v0.1.0 printed profile ids with this prefix
            return self._row(sid[len("profile/"):])
        if r is None and sid.startswith("#"):  # bare slug from kb_index: accept it when unambiguous
            rows = self.con.execute(
                "SELECT s.* FROM kb_sections s JOIN kb_live l ON l.path=s.path WHERE substr(s.sid, -?)=? LIMIT 2",
                (len(sid), sid)).fetchall()
            r = rows[0] if len(rows) == 1 else None
        return r

    def _page_rows(self, page: str) -> List[sqlite3.Row]:
        rows = self.con.execute(
            "SELECT s.sid, s.title, s.level, s.bytes, s.ord, s.path FROM kb_sections s JOIN kb_live l "
            "ON l.path=s.path WHERE s.rel=? ORDER BY s.ord", (page,)).fetchall()
        if not rows and page.startswith("profile/"):
            return self._page_rows(page[len("profile/"):])
        return rows

    @staticmethod
    def _section_list(page: str, rows: List[sqlite3.Row]) -> str:
        lines = ["Sections of %s (kb_get <id> fetches one):" % page]
        for r in rows:
            if re.search(r"-p\d+$", r["sid"]) or not r["level"]:
                continue
            lines.append("%s%s  %s  (%d B)" % ("  " * max(0, (r["level"] or 1) - 1), r["sid"], r["title"], r["bytes"]))
        return "\n".join(lines) + "\n"

    def get(self, id: str, max_bytes: int = 8000) -> dict:
        """One section by id. A page name alone (`docs/x.md`), or the first section of a page, also
        returns the page's section list, so a page-level id leads straight to the section wanted."""
        self.refresh()
        page, _, slug = id.partition("#")
        r = self._row(id)
        if r is None and (not slug or slug == "top"):
            rows = self._page_rows(page)
            if rows:
                listing = self._section_list(rows[0]["sid"].split("#")[0], rows)
                return {"id": rows[0]["sid"].split("#")[0], "title": "sections", "path": rows[0]["sid"].split("#")[0],
                        "bytes": len(listing.encode("utf-8")), "truncated": False, "text": listing}
        if r is None:
            raise KeyError("no section %r; kb_get <page> lists a page's section ids, kb_search finds them" % id)
        max_bytes = max(200, int(max_bytes))
        text, truncated = r["body"], False
        if r["bytes"] > max_bytes:
            text = _utf8_cut(text, max_bytes)
            text += "\n[truncated: showed %d of %d bytes; call kb_get with a larger max_bytes if needed]\n" % (
                len(text.encode("utf-8")), r["bytes"])
            truncated = True
        if r["ord"] == 0:
            rows = self._page_rows(r["rel"])
            if len(rows) > 1:
                text = text.rstrip("\n") + "\n\n" + self._section_list(r["rel"], rows[1:])
        out = {"id": r["sid"], "title": r["title"], "path": r["rel"], "bytes": r["bytes"],
               "truncated": truncated, "text": text}
        nxt = self.con.execute("SELECT sid FROM kb_sections WHERE path=? AND ord=?",
                               (r["path"], r["ord"] + 1)).fetchone()
        if nxt and re.search(r"-p\d+$", nxt["sid"]):
            out["next_part"] = nxt["sid"]
        return out

    def toc(self, path: str = "") -> List[dict]:
        """No path: one line per file. With a path (relpath, prefix, or absolute): its heading tree."""
        self.refresh()
        c = self.con
        if not path:
            rows = c.execute(
                "SELECT s.rel, COUNT(*) n, SUM(s.bytes) b FROM kb_sections s JOIN kb_live l ON l.path=s.path "
                "GROUP BY s.rel ORDER BY s.rel").fetchall()
            return [{"path": r["rel"], "sections": r["n"], "bytes": r["b"]} for r in rows]
        absp = str(Path(os.path.expanduser(path)).resolve()) if os.path.isabs(os.path.expanduser(path)) else None
        rows = c.execute(
            "SELECT s.sid, s.rel, s.title, s.level, s.bytes FROM kb_sections s JOIN kb_live l ON l.path=s.path "
            "WHERE s.rel=? OR s.rel LIKE ? ESCAPE '\\' OR s.path=? ORDER BY s.rel, s.ord",
            (path, path.replace("%", "\\%").replace("_", "\\_").rstrip("/") + "/%", absp)).fetchall()
        files: Dict[str, dict] = {}
        for r in rows:
            f = files.setdefault(r["rel"], {"path": r["rel"], "headings": [], "_stack": []})
            node = {"id": r["sid"], "title": r["title"], "bytes": r["bytes"], "children": []}
            stack = f["_stack"]
            lvl = r["level"] or 0
            while stack and stack[-1][0] >= lvl and lvl > 0:
                stack.pop()
            if stack and lvl > stack[-1][0]:
                stack[-1][1]["children"].append(node)
            else:
                f["headings"].append(node)
            stack.append((lvl, node))
        out = []
        for f in files.values():
            f.pop("_stack")
            out.append(f)
        return out

    def build_index(self, max_bytes: int = 8000, detail: Optional[Sequence[str]] = None,
                    exclude: Sequence[str] = ()) -> str:
        """Compact index: one line per detailed page, `<page>: #slug #slug ...` (its level-1/2 sections,
        the page's own title heading left out), then one line naming the other pages. id = page + #slug,
        exactly as kb_get resolves it.

        detail: glob patterns of the pages whose section ids are listed (None = every page, as many as
        fit). exclude: pages left out (e.g. the rules file already in the rules). A page whose text is
        identical to one already listed (a descriptor copied into the wiki) is listed once. Profile files
        come first. Always fits max_bytes: detailed pages lose their ids from the end, then the other-
        pages line is cut, with a note.
        """
        import fnmatch
        import hashlib
        self.refresh()
        rows = self.con.execute(
            "SELECT s.sid, s.rel, s.title, s.level, s.ord, s.body FROM kb_sections s JOIN kb_live l "
            "ON l.path=s.path ORDER BY s.rel, s.ord").fetchall()
        pages: Dict[str, List[sqlite3.Row]] = {}
        for r in rows:
            pages.setdefault(r["rel"], []).append(r)
        auth = self._authoritative()
        order = sorted(pages, key=lambda rel: (rel not in auth, rel))
        seen, keep = set(), []
        for rel in order:
            h = hashlib.sha1("".join(r["body"] for r in pages[rel]).encode("utf-8")).hexdigest()
            if rel in exclude or h in seen:
                continue
            seen.add(h)
            keep.append(rel)

        def slugs(rel: str) -> List[str]:
            out, title_seen = [], False
            for r in pages[rel]:
                lvl = r["level"] or 0
                if lvl == 0 or lvl > 2 or re.search(r"-p\d+$", r["sid"]):
                    continue
                if lvl == 1 and not title_seen:     # the page's own title: the page name says it
                    title_seen = True
                    continue
                out.append(r["sid"][len(rel):])
            return out

        wanted = [rel for rel in keep if detail is None or any(fnmatch.fnmatch(rel, g) for g in detail)]
        wanted = [rel for rel in wanted if slugs(rel)]
        head = ("Section id = <page>#<slug>. The MCP tool kb_get(id) fetches one section; kb_get(<page>) lists a "
                "page's sections; kb_search(query) only when nothing here fits.\n")

        def render(n_detail: int, n_other: int) -> str:
            det = wanted[:n_detail]
            out = [head] + ["%s: %s\n" % (rel, " ".join(slugs(rel))) for rel in det]
            other = [rel for rel in keep if rel not in det]
            if other:
                shown = other[:n_other]
                out.append("Other pages: %s%s\n" % (" ".join(shown),
                           " (+%d more: kb_toc)" % (len(other) - len(shown)) if len(shown) < len(other) else ""))
            return "".join(out)

        n_other = len(keep)
        for n in range(len(wanted), -1, -1):
            text = render(n, n_other)
            if len(text.encode("utf-8")) <= max_bytes:
                return text
        while n_other > 0:
            n_other -= 1
            text = render(0, n_other)
            if len(text.encode("utf-8")) <= max_bytes:
                return text
        return _utf8_cut(head, max_bytes)

    def close(self) -> None:
        self.con.close()


def build_index(max_bytes: int = 8000, home: Optional[os.PathLike] = None,
                paths: Optional[Sequence[os.PathLike]] = None, detail: Optional[Sequence[str]] = None,
                exclude: Sequence[str] = ()) -> str:
    """Module-level convenience: the compact section index for the default (or given) KB paths."""
    kb = KB(home=home, paths=paths)
    try:
        return kb.build_index(max_bytes, detail, exclude)
    finally:
        kb.close()
