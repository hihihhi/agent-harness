"""The harness's own disk use: a cap on every store it writes, pruned as it writes, reported by `harness doctor`.

The harness must stay megabytes, never gigabytes. What it writes and how each is bounded:
  candidates.jsonl            newest CAPS["candidates"] entries (capture appends every turn)
  installed-extensions.jsonl  newest CAPS["ledger"] entries
  proposals/                  newest CAPS["proposals"] files
  backup/<timestamp>/         newest CAPS["backups"], plus EVERY backup installed.json still points to (your
                              original files: uninstall restores from them) and every uninstall stash
                              (backup/<ts>/modified/). If installed.json cannot be read, no backup is pruned.
  state/stop-*, capture-*     newest CAPS["state"] of these marker files; nothing else in state/ (saved project
                              state lives there) is ever touched
`harness doctor` holds the whole folder to its footprint cap and names the largest parts when over it. Claude Code's own transcripts (~/.claude/projects) are Claude Code's, kept by
its cleanupPeriodDays setting; the harness reports them and never deletes them.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Set

CAPS = {"candidates": 500, "ledger": 1000, "proposals": 100, "backups": 3, "state": 200}
BUDGET_BYTES = 1024 * 1024 * 1024   # the absolute ceiling report() checks; doctor applies its tighter caps


def _keep_tail(path: Path, n: int) -> None:
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= n:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(x + "\n" for x in lines[-n:]), encoding="utf-8")
    tmp.replace(path)


def _referenced_backups(hh: Path) -> Optional[Set[str]]:
    """Top-level backup folders installed.json points to (uninstall restores the user's files from them), or None
    when it cannot be read: then nobody knows which backups are still needed, so none may go."""
    out: Set[str] = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "backup" and isinstance(x, str) and x.startswith("backup/"):
                    out.add(x.split("/")[1])
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    try:
        walk(json.loads((hh / "installed.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    return out


def _rm_tree(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        for c in p.iterdir():
            _rm_tree(c)
        p.rmdir()
    else:
        p.unlink()


def _mtime(p: Path) -> float:
    try:
        return p.lstat().st_mtime
    except OSError:
        return 0.0


def _keep_newest(items: List[Path], n: int, by_name: bool = False) -> None:
    items = sorted(items, key=(lambda p: p.name) if by_name else _mtime)
    for p in items[:-n] if len(items) > n else []:
        try:
            _rm_tree(p)
        except OSError:
            pass


def prune(hh: Path) -> None:
    """Bring every store back under its cap. Cheap: called by the Stop hook after each capture."""
    _keep_tail(hh / "candidates.jsonl", CAPS["candidates"])
    _keep_tail(hh / "installed-extensions.jsonl", CAPS["ledger"])
    if (hh / "proposals").is_dir():
        _keep_newest([p for p in (hh / "proposals").iterdir() if p.is_file()], CAPS["proposals"])
    refs = _referenced_backups(hh)
    if refs is not None and (hh / "backup").is_dir():
        _keep_newest([p for p in (hh / "backup").iterdir() if p.is_dir() and not p.is_symlink()
                      and p.name not in refs and not (p / "modified").exists()], CAPS["backups"], by_name=True)
    if (hh / "state").is_dir():
        _keep_newest([p for p in (hh / "state").iterdir() if p.name.startswith(("stop-", "capture-"))
                      and (p.is_symlink() or p.is_file())], CAPS["state"])


def _size(p: Path, apparent: bool) -> int:
    total = 0
    for root, dirs, files in os.walk(p):
        for f in files:
            try:
                st = os.lstat(os.path.join(root, f))
            except OSError:
                continue
            total += st.st_size if apparent else getattr(st, "st_blocks", 0) * 512
    return total


def report(hh: Path, apparent: bool = False) -> Dict:
    parts: List = []
    if hh.is_dir():
        for c in hh.iterdir():
            parts.append((c.name, _size(c, apparent) if c.is_dir() else
                          (c.lstat().st_size if apparent else getattr(c.lstat(), "st_blocks", 0) * 512)))
    parts.sort(key=lambda x: -x[1])
    total = sum(n for _, n in parts)
    return {"total_bytes": total, "budget_bytes": BUDGET_BYTES, "ok": total <= BUDGET_BYTES, "largest": parts[:5]}


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0
    return str(n)
