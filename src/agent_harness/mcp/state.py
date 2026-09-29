"""Per-project working state (survives context compaction) and a short session log.

    <HARNESS_HOME>/state/<key>/STATE.md            current state, <= 4096 bytes
    <HARNESS_HOME>/state/<key>/STATE.<ts>.md       the last 10 previous versions
    <HARNESS_HOME>/sessions/<key>.log              one line per session note, last 200 kept

<key> is a slug of the project path; the project is the git root of the given dir (default: cwd),
else the dir itself.

CLI (used by a SessionStart hook; prints nothing when there is no state):
    python3 -m agent_harness.mcp.state print [--project DIR]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness.mcp.kb import harness_home  # type: ignore
else:
    from .kb import harness_home

STATE_MAX_BYTES = 4096
STATE_VERSIONS = 10
SESSION_LINES = 200
NOTE_CHARS = 300
TEMPLATE = ("## Goal\n## Decisions\n## Done\n## Next\n## Open questions\n## Key files\n")


def project_root(project: Optional[os.PathLike] = None) -> Path:
    """Git root of `project` (default: HARNESS_PROJECT or cwd), else the directory itself."""
    start = Path(project or os.environ.get("HARNESS_PROJECT") or os.getcwd()).expanduser().resolve()
    for d in (start,) + tuple(start.parents):
        if (d / ".git").exists():
            return d
    return start


def project_key(root: os.PathLike) -> str:
    s = str(root)
    slug = re.sub(r"[^0-9A-Za-z]+", "-", s).strip("-").lower()[-60:] or "root"
    return "%s-%s" % (slug, hashlib.sha1(s.encode("utf-8")).hexdigest()[:8])


def _now() -> _dt.datetime:
    return _dt.datetime.now()


class State:
    def __init__(self, home: Optional[os.PathLike] = None, project: Optional[os.PathLike] = None):
        self.home = harness_home(home)
        self.default_project = project

    def _root(self, project: Optional[str]) -> Path:
        return project_root(project or self.default_project)

    def _dir(self, project: Optional[str]) -> Path:
        return self.home / "state" / project_key(self._root(project))

    # ------------------------------------------------------------ working state
    def state_save(self, text: str, project: str = "") -> dict:
        data = text.encode("utf-8")
        if len(data) > STATE_MAX_BYTES:
            raise ValueError("state is %d bytes; the cap is %d. Save a shorter version: keep goal, decisions, "
                             "next steps and open questions; drop narration." % (len(data), STATE_MAX_BYTES))
        d = self._dir(project)
        d.mkdir(parents=True, exist_ok=True)
        cur = d / "STATE.md"
        if cur.exists():
            ts = _dt.datetime.fromtimestamp(cur.stat().st_mtime).strftime("%Y%m%dT%H%M%S%f")
            dst = d / ("STATE.%s.md" % ts)
            n = 1
            while dst.exists():
                dst = d / ("STATE.%s-%d.md" % (ts, n))
                n += 1
            os.replace(str(cur), str(dst))
        tmp = d / "STATE.md.tmp"
        tmp.write_bytes(data)
        os.replace(str(tmp), str(cur))
        for old in self.versions(project)[STATE_VERSIONS:]:
            old.unlink()
        return {"ok": True, "bytes": len(data), "path": str(cur), "project": str(self._root(project))}

    def versions(self, project: str = "") -> List[Path]:
        """Previous versions, newest first."""
        d = self._dir(project)
        if not d.is_dir():
            return []
        return sorted((p for p in d.glob("STATE.*.md") if ".conflict-" not in p.name),
                      key=lambda p: p.name, reverse=True)

    def conflicts(self, project: str = "") -> List[Path]:
        """`STATE.conflict-<host>.md` files written by sync; never loaded, never rotated away."""
        d = self._dir(project)
        return sorted(d.glob("STATE.conflict-*.md")) if d.is_dir() else []

    def state_path(self, project: str = "") -> Optional[Path]:
        p = self._dir(project) / "STATE.md"
        return p if p.is_file() else None

    def state_load(self, project: str = "") -> str:
        p = self.state_path(project)
        text = p.read_text(encoding="utf-8") if p else "no saved state"
        conf = self.conflicts(project)
        if conf:
            text = text.rstrip("\n") + "\n\n%d unresolved sync conflicts: %s (merge them into STATE.md with " \
                "state_save, then remove them)\n" % (len(conf), ", ".join(str(c) for c in conf))
        return text

    # ------------------------------------------------------------ session log
    def _log(self, project: Optional[str] = None) -> Path:
        return self.home / "sessions" / (project_key(self._root(project)) + ".log")

    def session_note(self, summary: str, project: str = "") -> dict:
        one = re.sub(r"\s+", " ", summary).strip()[:NOTE_CHARS]
        if not one:
            raise ValueError("summary is empty")
        f = self._log(project)
        f.parent.mkdir(parents=True, exist_ok=True)
        line = "%s\t%s\t%s\n" % (_now().strftime("%Y-%m-%d %H:%M"), self._root(project), one)
        lines = f.read_text(encoding="utf-8").splitlines(True) if f.exists() else []
        lines.append(line)
        tmp = f.with_suffix(".tmp")
        tmp.write_text("".join(lines[-SESSION_LINES:]), encoding="utf-8")
        os.replace(str(tmp), str(f))
        return {"ok": True}

    def session_recent(self, k: int = 5, project: str = "") -> List[dict]:
        f = self._log(project)
        if not f.exists():
            return []
        out = []
        for line in f.read_text(encoding="utf-8").splitlines()[-max(1, int(k)):]:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append({"date": parts[0], "project": parts[1], "summary": parts[2]})
        return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m agent_harness.mcp.state")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("print", help="print the saved working state (nothing if none)")
    pr.add_argument("--project", default="")
    a = ap.parse_args(argv)
    st = State()
    p = st.state_path(a.project)
    if p or st.conflicts(a.project):
        saved = _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if p else "never"
        sys.stdout.write("# Saved working state (saved %s; state_save updates it)\n" % saved)
        sys.stdout.write(st.state_load(a.project).rstrip("\n") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
