"""Plan, apply, back up and restore. The only module that writes into a user's tool config.

Every path an adapter touches is backed up once (the first time any install touches it) under
HARNESS_HOME/backup/<ts>/ and recorded in HARNESS_HOME/installed.json. Uninstall restores each path
byte-exactly when nothing changed it since install; when a tool rewrote a merged file meanwhile
(Claude Code rewrites ~/.claude.json constantly) only our keys are taken out, so the tool's own
changes survive; a changed replaced file is stashed under backup/<ts>/modified/ before the restore.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .adapters.base import Ctx, FileChange

PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = PKG_DIR.parent.parent
STATE = "installed.json"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")

WARNING = "this replaces your current {title} setup (backed up; `harness uninstall` restores it)"


class InstallError(Exception):
    pass


# ---------------------------------------------------------------- paths / state

def harness_home(home: Path) -> Path:
    env = os.environ.get("HARNESS_HOME")
    return Path(env).expanduser() if env else Path(home) / ".agent-harness"


def mcp_cmd(hh: Path) -> List[str]:
    return ["python3", str(hh / "lib" / "agent_harness" / "mcp" / "server.py")]


def load_state(hh: Path) -> dict:
    p = hh / STATE
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def save_state(hh: Path, state: dict) -> None:
    hh.mkdir(parents=True, exist_ok=True)
    (hh / STATE).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- TOML (read)

def load_toml(text: str) -> dict:
    try:
        import tomllib  # type: ignore
        return tomllib.loads(text)
    except ImportError:
        return _MiniToml(text).parse()


class _MiniToml:
    """Enough TOML for profiles and config.toml on Python 3.9/3.10 (no datetimes)."""

    def __init__(self, text: str):
        self.s, self.i = text, 0

    def err(self, msg):
        line = self.s.count("\n", 0, self.i) + 1
        raise ValueError(f"TOML line {line}: {msg}")

    def ws(self, newlines=False):
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in " \t\r" or (newlines and c == "\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(self.s) and self.s[self.i] != "\n":
                    self.i += 1
            else:
                break

    def parse(self) -> dict:
        root: dict = {}
        cur = root
        while True:
            self.ws(True)
            if self.i >= len(self.s):
                return root
            if self.s.startswith("[[", self.i):
                self.i += 2
                keys = self.keypath("]")
                self.expect("]]")
                parent = self.walk(root, keys[:-1])
                arr = parent.setdefault(keys[-1], [])
                arr.append({})
                cur = arr[-1]
            elif self.s[self.i] == "[":
                self.i += 1
                keys = self.keypath("]")
                self.expect("]")
                cur = self.walk(root, keys)
            else:
                keys = self.keypath("=")
                self.expect("=")
                self.ws()
                val = self.value()
                self.walk(cur, keys[:-1])[keys[-1]] = val
            self.ws()
            if self.i < len(self.s) and self.s[self.i] != "\n":
                self.err("expected end of line")

    def walk(self, d, keys):
        for k in keys:
            d = d.setdefault(k, {})
            if isinstance(d, list):
                d = d[-1]
        return d

    def expect(self, tok):
        self.ws()
        if not self.s.startswith(tok, self.i):
            self.err(f"expected {tok!r}")
        self.i += len(tok)

    def keypath(self, end) -> List[str]:
        keys = []
        while True:
            self.ws()
            c = self.s[self.i]
            if c in "\"'":
                keys.append(self.string())
            else:
                m = re.compile(r"[A-Za-z0-9_-]+").match(self.s, self.i)
                if not m:
                    self.err("bad key")
                keys.append(m.group())
                self.i = m.end()
            self.ws()
            if self.s[self.i] == ".":
                self.i += 1
                continue
            return keys

    def string(self) -> str:
        s = self.s
        for q in ('"""', "'''"):
            if s.startswith(q, self.i):
                j = s.index(q, self.i + 3)
                while s.startswith(q[0], j + 3):
                    j += 1
                raw = s[self.i + 3:j]
                self.i = j + 3
                if raw.startswith("\n"):
                    raw = raw[1:]
                if q == '"""':
                    raw = re.sub(r"\\\s*\n\s*", "", raw)
                    return self.unescape(raw)
                return raw
        q = s[self.i]
        j = self.i + 1
        while s[j] != q:
            j += 2 if (q == '"' and s[j] == "\\") else 1
        raw = s[self.i + 1:j]
        self.i = j + 1
        return self.unescape(raw) if q == '"' else raw

    @staticmethod
    def unescape(raw: str) -> str:
        return json.loads('"' + raw.replace("\n", "\\n").replace("\t", "\\t") + '"')

    def value(self):
        s, c = self.s, self.s[self.i]
        if c in "\"'":
            return self.string()
        if c == "[":
            self.i += 1
            out = []
            while True:
                self.ws(True)
                if s[self.i] == "]":
                    self.i += 1
                    return out
                out.append(self.value())
                self.ws(True)
                if s[self.i] == ",":
                    self.i += 1
        if c == "{":
            self.i += 1
            out = {}
            while True:
                self.ws()
                if s[self.i] == "}":
                    self.i += 1
                    return out
                keys = self.keypath("=")
                self.expect("=")
                self.ws()
                self.walk(out, keys[:-1])[keys[-1]] = self.value()
                self.ws()
                if s[self.i] == ",":
                    self.i += 1
        m = re.compile(r"[^\s,\]\}#]+").match(s, self.i)
        if not m:
            self.err("bad value")
        tok = m.group()
        self.i = m.end()
        if tok in ("true", "false"):
            return tok == "true"
        t = tok.replace("_", "")
        try:
            return int(t, 0) if not re.search(r"[.eE]", t) or t.startswith(("0x", "0o", "0b")) else float(t)
        except ValueError:
            self.err(f"unsupported value {tok!r}")


# ---------------------------------------------------------------- TOML (table replace)

_HDR = re.compile(r"^\s*\[(\[?)\s*(.+?)\s*\]\]?\s*(#.*)?$")


def _norm_table(name: str) -> Tuple[str, ...]:
    parts, cur, q = [], "", None
    for ch in name:
        if q:
            if ch == q:
                q = None
            else:
                cur += ch
        elif ch in "\"'":
            q = ch
        elif ch == ".":
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return tuple(parts)


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_val(x)}" for k, x in v.items()) + " }"
    raise InstallError(f"cannot write {type(v).__name__} to TOML")


def toml_table(name: str, body: dict) -> str:
    head = ".".join(_toml_key(p) for p in _norm_table(name))
    lines = [f"[{head}]"] + [f"{_toml_key(k)} = {_toml_val(v)}" for k, v in body.items()]
    return "\n".join(lines) + "\n"


def _split_blocks(text: str) -> List[Tuple[Optional[Tuple[str, ...]], str]]:
    """[(table-path or None for the preamble, raw text)], concatenation == text."""
    blocks: List[Tuple[Optional[Tuple[str, ...]], str]] = [(None, "")]
    for line in text.splitlines(keepends=True):
        m = _HDR.match(line)
        if m:
            blocks.append((_norm_table(m.group(2)), line))
        else:
            blocks[-1] = (blocks[-1][0], blocks[-1][1] + line)
    return blocks


def _owned(path: Tuple[str, ...], tables: List[Tuple[str, ...]]) -> bool:
    return any(path[:len(t)] == t for t in tables)


def toml_remove(text: str, names: List[str]) -> Tuple[str, str]:
    """Remove our WHOLE tables (and their sub-tables). Returns (rest, removed-text). Shared "+name" tables
    are never removed here: only their keys are edited (_set_keys / _drop_keys)."""
    tables = [_norm_table(n) for n in names if not n.startswith(SHARED)]
    keep, gone = [], []
    for path, raw in _split_blocks(text):
        (gone if path is not None and _owned(path, tables) else keep).append(raw)
    return "".join(keep), "".join(gone)


# A table name starting with "+" (e.g. "+features") is SHARED with the user: our keys are set inside their
# own [name] block, line by line (their other keys, sub-tables and comments stay as they are); uninstall
# takes out only our keys. Plain names are ours whole.
SHARED = "+"
_KEYLINE = r"^\s*{key}\s*="


def _header_index(blocks, path):
    return next((i for i, (bp, _) in enumerate(blocks) if bp == path), None)


def _defined_elsewhere(text: str, path: Tuple[str, ...]) -> bool:
    """The table exists (dotted keys or an inline table) without a [name] header of its own."""
    try:
        d = load_toml(text)
    except ValueError:
        return False
    for part in path:
        if not isinstance(d, dict) or part not in d:
            return False
        d = d[part]
    return True


def _set_keys(text: str, name: str, body: dict) -> str:
    path = _norm_table(name)
    blocks = _split_blocks(text)
    i = _header_index(blocks, path)
    if i is None:
        if _defined_elsewhere(text, path):
            raise InstallError(f"your config defines [{name}] without a [{name}] header (dotted keys or an inline "
                               f"table); write it as a [{name}] table, then retry")
        head = "" if not text.strip() else ("\n" if text.endswith("\n") else "\n\n")
        return text + head + toml_table(name, body)
    raw = blocks[i][1]
    lines = raw.splitlines(keepends=True)
    for k, v in body.items():
        new = f"{_toml_key(k)} = {_toml_val(v)}\n"
        rx = re.compile(_KEYLINE.format(key=re.escape(_toml_key(k))))
        hit = next((n for n, ln in enumerate(lines) if n > 0 and rx.match(ln)), None)
        if hit is not None:
            lines[hit] = new
        else:
            last = max(n for n, ln in enumerate(lines) if n == 0 or ln.strip())   # after the last non-blank line
            if not lines[last].endswith("\n"):
                lines[last] += "\n"
            lines.insert(last + 1, new)
    blocks[i] = (path, "".join(lines))
    return "".join(r for _, r in blocks)


def _drop_keys(text: str, name: str, body: dict, orig: str) -> str:
    """Our keys out of the user's [name]; a key the original had gets the original's line back; the header
    goes too when we created the table and nothing else is left in it."""
    path = _norm_table(name)
    blocks = _split_blocks(text)
    i = _header_index(blocks, path)
    if i is None:
        return text
    ob = _split_blocks(orig or "")
    oi = _header_index(ob, path)
    olines = ob[oi][1].splitlines(keepends=True) if oi is not None else []
    lines = blocks[i][1].splitlines(keepends=True)
    for k in body:
        rx = re.compile(_KEYLINE.format(key=re.escape(_toml_key(k))))
        hit = next((n for n, ln in enumerate(lines) if n > 0 and rx.match(ln)), None)
        was = next((ln for n, ln in enumerate(olines) if n > 0 and rx.match(ln)), None)
        if hit is not None:
            if was is not None:
                lines[hit] = was if was.endswith("\n") else was + "\n"
            else:
                del lines[hit]
    if oi is None and not any(ln.strip() for ln in lines[1:]):
        del blocks[i]
        out = "".join(r for _, r in blocks)
        return out.rstrip("\n") + "\n" if out.strip() else ""
    blocks[i] = (path, "".join(lines))
    return "".join(r for _, r in blocks)


def toml_unmerge(text: str, patch: Dict[str, dict], orig: str) -> str:
    """Take one patch out of `text`: plain tables go (the original's copy comes back); from a shared
    "+name" table only our keys go, each put back to the original's line when it had one."""
    whole = [n for n in patch if not n.startswith(SHARED)]
    rest, _ = toml_remove(text, whole)
    if orig and whole:
        _, ours_before = toml_remove(orig, whole)
        if ours_before:
            rest = rest + ("" if rest.endswith("\n") or not rest else "\n") + ours_before
    for n, body in patch.items():
        if n.startswith(SHARED):
            rest = _drop_keys(rest, n[1:], body, orig)
    return rest


def toml_merge(text: str, tables: Dict[str, dict]) -> str:
    whole = {n: b for n, b in tables.items() if not n.startswith(SHARED)}
    rest, _ = toml_remove(text, list(whole))
    rest = rest.rstrip("\n") + "\n" if rest.strip() else ""       # no blank lines piling up per update
    add = "\n".join(toml_table(n, b) for n, b in whole.items())
    out = rest + ("\n" if rest and add else "") + add
    for n, b in tables.items():
        if n.startswith(SHARED):
            out = _set_keys(out, n[1:], b)
    try:
        load_toml(out)
    except ValueError as e:  # pragma: no cover - only on hand-broken configs
        raise InstallError(f"merged TOML would not parse: {e}")
    return out


# ---------------------------------------------------------------- JSON merge

def json_merge(base, patch):
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = dict(base)
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = json_merge(out[k], v)
        elif k in out and isinstance(out[k], list) and isinstance(v, list):
            out[k] = list(out[k]) + [copy.deepcopy(x) for x in v if x not in out[k]]
        else:
            out[k] = copy.deepcopy(v)
    return out


def json_unmerge(cur: dict, patch: dict, orig: dict) -> dict:
    """Take our patch back out of `cur`, keeping whatever else changed since install."""
    cur = dict(cur)
    for k, pv in patch.items():
        if k not in cur:
            continue
        cv = cur[k]
        if k in orig:
            ov = orig[k]
            if isinstance(cv, dict) and isinstance(pv, dict) and isinstance(ov, dict):
                cur[k] = json_unmerge(cv, pv, ov)
            elif isinstance(cv, list) and isinstance(pv, list) and isinstance(ov, list):
                cur[k] = [x for x in cv if x not in pv or x in ov]
            else:
                cur[k] = ov
        else:
            if isinstance(cv, dict) and isinstance(pv, dict):
                cur[k] = json_unmerge(cv, pv, {})
                if not cur[k]:
                    del cur[k]
            elif isinstance(cv, list) and isinstance(pv, list):
                cur[k] = [x for x in cv if x not in pv]
                if not cur[k]:
                    del cur[k]
            else:
                del cur[k]
    return cur


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        raise InstallError(f"{path} is not plain JSON ({e}); fix or move it, then retry")
    if not isinstance(data, dict):
        raise InstallError(f"{path} does not hold a JSON object")
    return data


def _dump_json(data: dict) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------- hashing / copying

def path_hash(p: Path) -> Optional[str]:
    if p.is_symlink():
        return "l:" + os.readlink(p)
    if not p.exists():
        return None
    h = hashlib.sha256()
    if p.is_dir():
        for f in sorted(x for x in p.rglob("*")):
            h.update(str(f.relative_to(p)).encode() + b"\0")
            if f.is_file():
                h.update(f.read_bytes())
        return "d:" + h.hexdigest()
    h.update(p.read_bytes())
    return "f:" + h.hexdigest()


def _copy_any(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def _remove_any(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def dir_size(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


# ---------------------------------------------------------------- render

def render(change: FileChange, prior: Optional[list] = None, orig: Optional[Path] = None) -> Optional[bytes]:
    """New bytes for a file change; None for copy-dir. Reads, never writes.
    prior: the patches an earlier install/update merged into this file; they are taken out first (the
    original's values come back from `orig`, the backup), so an update REPLACES the harness's keys and
    list items instead of piling new ones next to stale ones."""
    p = Path(change.path)
    prior = [x for x in (prior or []) if x != change.content]
    if change.kind == "replace":
        c = change.content
        return c.read_bytes() if isinstance(c, Path) else str(c).encode("utf-8")
    if change.kind == "merge-json":
        cur = _read_json(p)
        base = {}
        if prior and orig is not None:
            try:
                base = _read_json(orig)
            except InstallError:      # a backup with comments: its values cannot be restored key by key
                base = {}
        for old in reversed(prior):
            cur = json_unmerge(cur, old, base)
        return _dump_json(json_merge(cur, change.content))
    if change.kind == "merge-toml":
        text = p.read_text(encoding="utf-8") if p.exists() else ""
        before = orig.read_text(encoding="utf-8") if (prior and orig is not None) else ""
        for old in reversed(prior):
            text = toml_unmerge(text, old, before)
        return toml_merge(text, change.content).encode("utf-8")
    if change.kind == "copy-dir":
        if not Path(change.content).is_dir():
            raise InstallError(f"missing source directory {change.content}")
        return None
    if change.kind == "symlink":
        return None
    raise InstallError(f"unknown change kind {change.kind!r}")


# ---------------------------------------------------------------- apply / uninstall

def _missing_parents(p: Path) -> List[str]:
    out = []
    d = p.parent
    while not d.exists():
        out.append(str(d))
        d = d.parent
    return out


def _inside(p: Path, roots: List[Path]) -> bool:
    ap = os.path.normpath(os.path.abspath(str(p)))
    for r in roots:
        rr = os.path.normpath(os.path.abspath(str(r)))
        if ap == rr or ap.startswith(rr.rstrip(os.sep) + os.sep):
            return True
    return False


def check_roots(paths: List[Path], roots: List[Path]) -> None:
    """Safety net: nothing is written outside the HOME we were given, HARNESS_HOME, or --project."""
    bad = [str(p) for p in paths if not _inside(p, roots)]
    if bad:
        raise InstallError("refusing to write outside " + ", ".join(str(r) for r in roots) + ": " + ", ".join(bad))


def own_patches(e: Optional[dict], tool: str) -> list:
    """The patch `tool` merged into this file earlier: from patch_by_tool, or, for a state written before
    v0.1.1, the recorded patches when this tool was the file's only writer."""
    if not e:
        return []
    by = e.get("patch_by_tool")
    if by is not None:
        return [by[tool]] if tool in by else []
    return list(e.get("patches") or []) if e.get("tools") == [tool] else []


def apply(hh: Path, tool_changes: List[Tuple[str, List[FileChange]]], state: dict,
          roots: Optional[List[Path]] = None) -> dict:
    """Back up, write, record. Everything is checked and rendered before the first write.
    `roots`: the only trees writes may land in (default: the ones recorded at install)."""
    roots = [Path(r) for r in (roots or state.get("roots") or [])]
    if not roots:
        raise InstallError("no write roots given")
    check_roots([Path(ch.path) for _, changes in tool_changes for ch in changes], roots + [hh])
    state["roots"] = sorted({str(r) for r in roots} | set(state.get("roots", [])))
    known = {e["path"]: e for e in state.get("entries", [])}
    prior_of = {(str(Path(ch.path)), tool): own_patches(known.get(str(Path(ch.path))), tool)
                for tool, changes in tool_changes for ch in changes}
    rendered = []
    for tool, changes in tool_changes:
        for ch in changes:
            e = known.get(str(Path(ch.path)))
            orig = hh / e["backup"] if e and e.get("backup") else None
            rendered.append((tool, ch, render(ch, own_patches(e, tool), orig)))
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = hh / "backup" / ts
    entries: Dict[str, dict] = {e["path"]: e for e in state.setdefault("entries", [])}
    for tool, ch, data in rendered:
        p = Path(ch.path)
        e = entries.get(str(p))
        if e is None:
            e = {"path": str(p), "kind": ch.kind, "existed": p.exists() or p.is_symlink(),
                 "backup": None, "created_dirs": _missing_parents(p), "tools": [], "patches": []}
            if e["existed"]:
                rel = f"backup/{ts}/files/{len(state['entries'])}"
                _copy_any(p, hh / rel)
                e["backup"] = rel
            state["entries"].append(e)
            entries[str(p)] = e
        if tool not in e["tools"]:
            e["tools"].append(tool)
        if ch.kind in ("merge-json", "merge-toml"):
            # one patch per tool: render() took out only THIS tool's earlier patch (two tools may share a file)
            by = e.setdefault("patch_by_tool", {})
            gone = prior_of.get((str(p), tool), [])
            by[tool] = copy.deepcopy(ch.content)
            e["patches"] = [x for x in e["patches"] if x not in gone and x not in by.values()] + list(by.values())
        p.parent.mkdir(parents=True, exist_ok=True)
        if ch.kind == "copy-dir":
            _remove_any(p)
            shutil.copytree(Path(ch.content), p, ignore=IGNORE)
        elif ch.kind == "symlink":
            _remove_any(p)
            p.symlink_to(Path(ch.content), target_is_directory=True)
        else:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.is_symlink():
                p.unlink()
            p.write_bytes(data)
        e["written"] = path_hash(p)
    if backup.exists():
        state.setdefault("backups", []).append(ts)
    return state


def uninstall(hh: Path, log: Callable[[str], None] = print) -> List[str]:
    state = load_state(hh)
    if not state:
        return []
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    done = []
    check_roots([Path(e["path"]) for e in state.get("entries", [])],
                [Path(r) for r in state.get("roots", [])] + [hh])
    for e in reversed(state.get("entries", [])):
        p = Path(e["path"])
        orig = hh / e["backup"] if e.get("backup") else None
        if path_hash(p) == e.get("written") or not (p.exists() or p.is_symlink()):
            _remove_any(p)
            if orig is not None:
                _copy_any(orig, p)
        elif e["kind"] == "merge-json":
            base = _read_json(orig) if orig is not None else {}
            cur = _read_json(p)
            for patch in reversed(e.get("patches", [])):
                cur = json_unmerge(cur, patch, base)
            if cur or orig is not None:
                p.write_bytes(_dump_json(cur))
            else:
                p.unlink()
            log(f"  {p}: changed since install; took out only the harness keys")
        elif e["kind"] == "merge-toml":
            rest = p.read_text(encoding="utf-8")
            before = orig.read_text(encoding="utf-8") if orig is not None else ""
            for patch in reversed(e.get("patches", [])):
                rest = toml_unmerge(rest, patch, before)
            if rest.strip() or orig is not None:
                p.write_text(rest, encoding="utf-8")
            else:
                p.unlink()
            log(f"  {p}: changed since install; took out only the harness tables")
        else:
            stash = hh / "backup" / ts / "modified" / str(len(done))
            _copy_any(p, stash)
            _remove_any(p)
            if orig is not None:
                _copy_any(orig, p)
            log(f"  {p}: you edited it after install; your edit is kept at {stash}")
        for d in e.get("created_dirs", []):
            dp = Path(d)
            try:
                dp.rmdir()
            except OSError:
                pass
        done.append(str(p))
    for sub in ("lib", "content", "profile"):
        _remove_any(hh / sub)
    (hh / STATE).unlink()
    return done


# ---------------------------------------------------------------- profile + rules

def load_profile(profile_dir: Optional[Path]) -> dict:
    if not profile_dir:
        return {}
    f = Path(profile_dir) / "profile.toml"
    if not f.is_file():
        return {}
    return load_toml(f.read_text(encoding="utf-8"))


def _pget(profile: dict, *keys, default=None):
    for k in keys:
        d = profile
        for part in k.split("."):
            if not isinstance(d, dict) or part not in d:
                break
            d = d[part]
        else:
            return d
    return default


def descriptor_name(profile: dict, profile_dir: Optional[Path]) -> Optional[str]:
    name = _pget(profile, "descriptor", "profile.descriptor", "rules.descriptor")
    if name:
        return str(name)
    if profile_dir and (Path(profile_dir) / "SERVER.md").is_file():
        return "SERVER.md"
    return None


DESCRIPTOR_MARK = "<!-- harness:descriptor -->"


def build_rules(content: Path, profile: dict, profile_dir: Optional[Path], hh: Path) -> str:
    """content/AGENTS.md + the profile's extra_rules file + a pointer to its descriptor.
    The pointer replaces the `<!-- harness:descriptor -->` line when AGENTS.md has one."""
    agents = Path(content) / "AGENTS.md"
    text = agents.read_text(encoding="utf-8").rstrip("\n") if agents.is_file() else "# Agent rules"
    desc = descriptor_name(profile, profile_dir)
    pointer = ""
    if desc:
        pointer = (f"This machine is described in `{desc}` (`{hh / 'profile' / desc}`); its section ids are in "
                   "the Knowledge index. Read the section you need before touching shared storage, services or "
                   "other users.")
    lines = text.split("\n")
    marks = [i for i, ln in enumerate(lines) if ln.strip() == DESCRIPTOR_MARK]
    if marks:
        for i in reversed(marks):
            if pointer:
                lines[i] = pointer
            else:
                del lines[i]
        text = "\n".join(lines)
    elif pointer:
        text += "\n\n## This machine\n\n" + pointer
    parts = [text]
    extra = _pget(profile, "extra_rules", "rules.extra_rules")
    if extra and profile_dir:
        f = Path(profile_dir) / os.path.expanduser(str(extra))
        if not f.is_file():
            raise InstallError(f"profile extra_rules file not found: {f}")
        parts.append(f.read_text(encoding="utf-8").strip())
    return "\n\n".join(p for p in parts if p) + "\n"


def kb_paths(profile: dict, profile_dir: Optional[Path]) -> List[Path]:
    """The profile's kb_paths, ~ expanded, relative ones resolved against the profile dir."""
    out = []
    for v in profile.get("kb_paths") or []:
        p = Path(os.path.expanduser(str(v)))
        if not p.is_absolute() and profile_dir:
            p = Path(profile_dir) / p
        out.append(p)
    return out


# ---------------------------------------------------------------- copying the harness itself

def copy_harness(hh: Path, source: Path, profile_dir: Optional[Path]) -> None:
    hh.mkdir(parents=True, exist_ok=True)
    for sub in ("memory", "lessons"):
        (hh / sub).mkdir(exist_ok=True)
    lib = hh / "lib" / "agent_harness"
    if lib.resolve() != PKG_DIR:
        _remove_any(lib)
        shutil.copytree(PKG_DIR, lib, ignore=IGNORE)
    src_content = Path(source) / "content"
    if src_content.is_dir() and src_content.resolve() != (hh / "content").resolve():
        _remove_any(hh / "content")
        shutil.copytree(src_content, hh / "content", ignore=IGNORE)
    if profile_dir and Path(profile_dir).resolve() != (hh / "profile").resolve():
        _remove_any(hh / "profile")
        shutil.copytree(Path(profile_dir), hh / "profile", ignore=IGNORE)


def extra_rules_text(profile: dict, profile_dir: Optional[Path]) -> str:
    extra = _pget(profile, "extra_rules", "rules.extra_rules")
    if not (extra and profile_dir):
        return ""
    f = Path(profile_dir) / os.path.expanduser(str(extra))
    return f.read_text(encoding="utf-8").strip() if f.is_file() else ""


def make_ctx(home: Path, hh: Path, content: Path, profile_dir: Optional[Path],
             project: Optional[Path] = None, extra_mcp: Optional[dict] = None) -> Ctx:
    profile = load_profile(profile_dir)
    return Ctx(home=Path(home), harness_home=hh, content=Path(content), profile=profile,
               rules=build_rules(content, profile, profile_dir, hh), mcp_cmd=mcp_cmd(hh),
               scope="project" if project else "user", project=Path(project) if project else None,
               extra_mcp=dict(extra_mcp or {}), extra_rules=extra_rules_text(profile, profile_dir))


def plan_lines(adapter, changes: List[FileChange]) -> List[str]:
    out = [f"{adapter.title}:"]
    for ch in changes:
        verb = {"replace": "write", "merge-json": "merge into", "merge-toml": "merge into",
                "copy-dir": "copy to"}.get(ch.kind, ch.kind)
        out.append(f"  {verb} {ch.path} - {ch.note}")
    return out


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False
