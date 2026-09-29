"""Two-way sync of memory, lessons, task state and session logs between two HARNESS_HOMEs.

The unit is one file (one fact, lesson or state). Per file:
- only one side changed since the last sync -> that side's version is copied over;
- both changed -> the newest mtime wins; the loser is kept as <id>.conflict-<host><ext> and reported;
- deleted on one side -> a tombstone <id>.deleted (holding the deletion time) removes it on the other
  side, unless that side changed it after the deletion; tombstones expire after 30 days.
Session logs (sessions/*.log) are merged line-wise: the union, sorted (lines start with the date).
Index databases are never synced; each side rebuilds its own.

The remote is either a local path (tests, shared mounts) or `<ssh-target>:<remote HARNESS_HOME>`,
reached with one `tar` stream each way over ssh (`--ssh-cmd` for a wrapper or extra options).
The last-synced hashes live in HARNESS_HOME/sync-state.json, per remote.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DIRS = ("memory", "lessons", "state", "sessions")
TOMBSTONE = ".deleted"
TOMBSTONE_TTL = 30 * 86400
SKIP_SUFFIXES = (".sqlite", ".sqlite-wal", ".sqlite-shm", ".tmp", ".lock")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def is_log(rel: str) -> bool:
    return rel.startswith("sessions/") and rel.endswith(".log")


def tomb_of(rel: str) -> str:
    """memory/user/<id>.json -> memory/user/<id>.deleted"""
    head, _, name = rel.rpartition("/")
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    return (f"{head}/" if head else "") + stem + TOMBSTONE


def scan(root: Path) -> Dict[str, Tuple[str, float]]:
    """{relpath: (sha256, mtime)} of the synced trees."""
    out = {}
    for d in DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.is_symlink() and p.name != ".DS_Store" and not p.name.startswith("._") \
                    and not p.name.endswith(SKIP_SUFFIXES) and not p.name.endswith(".synctmp"):
                out[str(p.relative_to(root)).replace(os.sep, "/")] = (_sha(p.read_bytes()), p.stat().st_mtime)
    return out


class Side:
    """A HARNESS_HOME on disk. Records what changed so a remote copy can be pushed back."""

    def __init__(self, root: Path, host: str):
        self.root, self.host = Path(root), host
        self.written: List[str] = []
        self.deleted: List[str] = []
        self.files = scan(self.root)

    def read(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()

    def mtime(self, rel: str) -> float:
        return self.files[rel][1]

    def write(self, rel: str, data: bytes, mtime: Optional[float] = None) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".synctmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        mtime = time.time() if mtime is None else mtime
        os.utime(p, (mtime, mtime))
        self.files[rel] = (_sha(data), mtime)
        self.written.append(rel)
        if rel in self.deleted:
            self.deleted.remove(rel)

    def delete(self, rel: str) -> None:
        p = self.root / rel
        if p.exists():
            p.unlink()
        self.files.pop(rel, None)
        if rel in self.written:
            self.written.remove(rel)
        self.deleted.append(rel)


def _tomb_time(side: Side, rel: str) -> Optional[float]:
    t = tomb_of(rel)
    if t not in side.files:
        return None
    try:
        return float(side.read(t).decode().strip())
    except ValueError:
        return side.mtime(t)


def _conflict_name(rel: str, host: str) -> str:
    head, _, name = rel.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    new = f"{stem}.conflict-{host}.{ext}" if dot else f"{name}.conflict-{host}"
    return f"{head}/{new}" if head else new


def merge(a: Side, b: Side, base: Dict[str, str], now: Optional[float] = None) -> Tuple[Dict[str, str], List[str]]:
    """Two-way merge of side a and side b. Returns (new base, report lines)."""
    now = time.time() if now is None else now
    report: List[str] = []
    # 1. deletions since the last sync become tombstones
    for side in (a, b):
        for rel in base:
            if rel not in side.files and tomb_of(rel) not in side.files and not rel.endswith(TOMBSTONE) \
                    and not is_log(rel):
                side.write(tomb_of(rel), f"{now}\n".encode(), now)
    data = sorted({r for s in (a, b) for r in s.files if not r.endswith(TOMBSTONE)}
                  | {r for r in base if not r.endswith(TOMBSTONE)})
    for rel in data:
        la, lb = a.files.get(rel), b.files.get(rel)
        if is_log(rel):
            lines = set()
            for s, have in ((a, la), (b, lb)):
                if have:
                    lines |= {ln for ln in s.read(rel).decode("utf-8", "replace").splitlines() if ln.strip()}
            merged = ("\n".join(sorted(lines)) + "\n").encode() if lines else b""
            for s, have in ((a, la), (b, lb)):
                if merged and (not have or have[0] != _sha(merged)):
                    s.write(rel, merged)
            continue
        tomb = max([t for t in (_tomb_time(a, rel), _tomb_time(b, rel)) if t is not None], default=None)
        newest = max([x[1] for x in (la, lb) if x], default=None)
        if tomb is not None and (newest is None or newest <= tomb):
            for s in (a, b):
                if rel in s.files:
                    s.delete(rel)
                if tomb_of(rel) not in s.files:
                    s.write(tomb_of(rel), f"{tomb}\n".encode(), tomb)
            if la or lb:
                report.append(f"deleted {rel}")
            continue
        for s in (a, b):  # a newer edit outlives an older deletion
            if tomb_of(rel) in s.files:
                s.delete(tomb_of(rel))
        if la and lb and la[0] == lb[0]:
            continue
        if not la or not lb:
            src, dst = (a, b) if la else (b, a)
            dst.write(rel, src.read(rel), src.mtime(rel))
            continue
        old = base.get(rel)
        if old == la[0]:
            a.write(rel, b.read(rel), lb[1])
        elif old == lb[0]:
            b.write(rel, a.read(rel), la[1])
        else:
            win, lose = (a, b) if la[1] >= lb[1] else (b, a)
            loser_bytes, loser_mtime = lose.read(rel), lose.mtime(rel)
            cname = _conflict_name(rel, lose.host)
            lose.write(rel, win.read(rel), win.mtime(rel))
            for s in (a, b):
                s.write(cname, loser_bytes, loser_mtime)
            report.append(f"conflict {rel}: kept {win.host}'s newer version; {lose.host}'s is {cname}")
    # tombstones: spread them, expire the old ones
    for rel in sorted({r for s in (a, b) for r in s.files if r.endswith(TOMBSTONE)}):
        holders = [s for s in (a, b) if rel in s.files]
        src = holders[0]
        try:
            t = float(src.read(rel).decode().strip())
        except ValueError:
            t = src.mtime(rel)
        if now - t > TOMBSTONE_TTL:
            for s in holders:
                s.delete(rel)
            continue
        for s in (a, b):
            if rel not in s.files:
                s.write(rel, src.read(rel), src.mtime(rel))
    new_base = {r: h for r, (h, _) in a.files.items() if b.files.get(r, ("",))[0] == h}
    return new_base, report


# ---------------------------------------------------------------- transports

def parse_remote(remote: str) -> Tuple[Optional[str], str]:
    """'host:path' -> (host, path); a plain local path -> (None, path)."""
    if os.path.isabs(remote) or remote.startswith(("./", "../", "~")) or ":" not in remote:
        return None, os.path.expanduser(remote)
    host, _, path = remote.partition(":")
    return host, path or ".agent-harness"


def _remote_dir(path: str) -> str:
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _ssh(ssh_cmd: str, host: str, command: str, data: Optional[bytes] = None) -> bytes:
    argv = shlex.split(ssh_cmd) + [host, command]
    p = subprocess.run(argv, input=data, capture_output=True, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"ssh {host} failed ({p.returncode}): {p.stderr.decode(errors='replace').strip()[:300]}")
    return p.stdout


def pull(ssh_cmd: str, host: str, path: str, staging: Path) -> None:
    d = _remote_dir(path)
    cmd = f"mkdir -p {d} && cd {d} && mkdir -p {' '.join(DIRS)} && COPYFILE_DISABLE=1 tar -cf - {' '.join(DIRS)}"
    with tarfile.open(fileobj=io.BytesIO(_ssh(ssh_cmd, host, cmd))) as tf:
        members = [m for m in tf.getmembers()
                   if (m.isfile() or m.isdir()) and not m.name.startswith("/") and ".." not in m.name.split("/")
                   and not m.name.rpartition("/")[2].startswith("._")]
        tf.extractall(staging, members=members)


def push(ssh_cmd: str, host: str, path: str, staging: Path, side: Side) -> None:
    d = _remote_dir(path)
    if side.written:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for rel in sorted(set(side.written)):
                tf.add(staging / rel, arcname=rel)
        _ssh(ssh_cmd, host, f"cd {d} && tar -xf -", buf.getvalue())
    if side.deleted:
        _ssh(ssh_cmd, host, f"cd {d} && rm -f -- " + " ".join(shlex.quote(r) for r in sorted(set(side.deleted))))


# ---------------------------------------------------------------- entry point

def local_host() -> str:
    return socket.gethostname().split(".")[0] or "local"


def run_sync(hh: Path, remote: str, ssh_cmd: str = "ssh", dry_run: bool = False,
             now: Optional[float] = None) -> List[str]:
    host, path = parse_remote(remote)
    state_file = hh / "sync-state.json"
    states = json.loads(state_file.read_text()) if state_file.is_file() else {}
    base = states.get(remote, {})
    remote_label = (host.rpartition("@")[2] if host else "remote").replace("/", "_")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if dry_run:  # work on copies of both sides
            shutil.copytree(hh, tmp / "local", ignore=shutil.ignore_patterns("lib", "content", "backup", "*.sqlite*"))
            local_root = tmp / "local"
        else:
            local_root = hh
        if host:
            remote_root = tmp / "remote"
            remote_root.mkdir()
            pull(ssh_cmd, host, path, remote_root)
        elif dry_run:
            remote_root = tmp / "remote"
            if Path(path).is_dir():
                shutil.copytree(path, remote_root, ignore=shutil.ignore_patterns("lib", "content", "backup", "*.sqlite*"))
            else:
                remote_root.mkdir()
        else:
            remote_root = Path(path)
            remote_root.mkdir(parents=True, exist_ok=True)
        a, b = Side(local_root, local_host()), Side(remote_root, remote_label)
        if a.host == b.host:
            b.host = b.host + "-remote"
        new_base, report = merge(a, b, base, now)
        pulled = len(set(a.written) - {r for r in a.written if r.endswith(TOMBSTONE)})
        pushed = len(set(b.written) - {r for r in b.written if r.endswith(TOMBSTONE)})
        if dry_run:
            return [f"dry run: would pull {pulled} and push {pushed} file(s)"] + report
        if host:
            push(ssh_cmd, host, path, remote_root, b)
    states[remote] = new_base
    state_file.write_text(json.dumps(states, indent=1, sort_keys=True) + "\n")
    return [f"synced with {remote}: pulled {pulled}, pushed {pushed} file(s)"] + report
