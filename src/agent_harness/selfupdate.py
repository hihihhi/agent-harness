"""harness self-update: keep this machine on the newest released harness, by itself.

    harness self-update            at most once a day: update when a newer release passed its checks
    harness self-update --check    say what it would do, change nothing
    harness self-update --now      ignore the once-a-day limit

What counts as a release: a tag vX.Y.Z of the harness repository (the profile's `update_repo`, else
HARNESS_UPDATE_REPO, else the project's own repository) whose commit has every GitHub check run completed and
successful. Never a branch, never a tag whose checks failed or are still running, never a downgrade. The
update itself is `harness update --source <that tag>`: memory, lessons and the active profile are kept, and
every file it replaces is backed up first. Afterwards `harness doctor` must pass, and the result (or the
reason nothing changed) is recorded in ~/.agent-harness/state/self-update.json. Every network failure means
"no update", never a half-done one.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

DEFAULT_REPO = "oscar-chw/agent-harness"     # the project's own home; override per profile or environment
API = "https://api.github.com/repos/%s"
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DAY = 24 * 3600
TIMEOUT = 20


def _get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "agent-harness-self-update"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def repo_for(hh: Path) -> str:
    env = os.environ.get("HARNESS_UPDATE_REPO", "").strip()
    if env:
        return env
    f = hh / "profile" / "profile.toml"
    try:
        m = re.search(r'^\s*update_repo\s*=\s*"([^"]+)"', f.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except OSError:
        pass
    return DEFAULT_REPO


def version_key(v: str) -> Tuple[int, int, int]:
    m = TAG.match(v if v.startswith("v") else "v" + v)
    return tuple(int(x) for x in m.groups()) if m else (-1, -1, -1)


def installed_version(hh: Path) -> str:
    try:
        m = re.search(r'__version__ = "([^"]+)"', (hh / "lib" / "agent_harness" / "__init__.py").read_text())
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def newest_release(repo: str, get=_get) -> Optional[dict]:
    """{"tag", "sha"} of the highest vX.Y.Z tag, or None."""
    tags = json.loads(get(API % repo + "/tags?per_page=100"))
    best = None
    for t in tags if isinstance(tags, list) else []:
        name = str(t.get("name") or "")
        if TAG.match(name) and (best is None or version_key(name) > version_key(best["tag"])):
            best = {"tag": name, "sha": (t.get("commit") or {}).get("sha")}
    return best


def checks_passed(repo: str, sha: str, get=_get) -> Tuple[bool, str]:
    d = json.loads(get(API % repo + "/commits/%s/check-runs?per_page=100" % sha))
    runs = d.get("check_runs") if isinstance(d, dict) else None
    if not runs:
        return False, "no check runs for %s" % sha[:12]
    bad = [r.get("name") for r in runs if r.get("status") != "completed" or r.get("conclusion") != "success"]
    return (not bad), ("all %d checks passed" % len(runs) if not bad else "checks not passed: %s" % ", ".join(bad))


def _state(hh: Path) -> Path:
    return hh / "state" / "self-update.json"


def _record(hh: Path, **kw) -> None:
    f = _state(hh)
    f.parent.mkdir(parents=True, exist_ok=True)
    kw["when"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    kw["epoch"] = time.time()
    f.write_text(json.dumps(kw) + "\n", encoding="utf-8")


def _extract(blob: bytes, dest: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
        for m in t.getmembers():      # nothing may land outside dest (absolute paths, .., links out)
            p = (dest / m.name).resolve()
            if not str(p).startswith(str(dest.resolve()) + os.sep) or m.issym() or m.islnk():
                raise ValueError("unsafe path in the release archive: %s" % m.name)
        t.extractall(dest)
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1 or not (tops[0] / "src" / "agent_harness").is_dir():
        raise ValueError("the release archive is not an agent-harness source tree")
    return tops[0]


def run(hh: Path, home: Path, check_only: bool = False, now: bool = False, get=_get,
        update_cmd: Optional[List[str]] = None, doctor_cmd: Optional[List[str]] = None) -> Tuple[int, str]:
    if not now and not check_only:
        try:
            last = json.loads(_state(hh).read_text(encoding="utf-8")).get("epoch", 0)
            if time.time() - float(last) < DAY:
                return 0, "self-update: checked within the last day"
        except (OSError, ValueError, TypeError):
            pass
    repo, have = repo_for(hh), installed_version(hh)
    try:
        rel = newest_release(repo, get)
        if rel is None:
            return 0, "self-update: %s has no release tags" % repo
        if version_key(rel["tag"]) <= version_key(have):
            if not check_only:
                _record(hh, result="current", version=have, newest=rel["tag"])
            return 0, "self-update: %s is current (newest release %s)" % (have, rel["tag"])
        ok, why = checks_passed(repo, rel["sha"], get)
        if not ok:
            if not check_only:
                _record(hh, result="held", version=have, newest=rel["tag"], why=why)
            return 0, "self-update: %s is out but not taken: %s" % (rel["tag"], why)
        if check_only:
            return 0, "self-update: would update %s -> %s (%s)" % (have, rel["tag"], why)
        blob = get("https://codeload.github.com/%s/tar.gz/refs/tags/%s" % (repo, rel["tag"]))
    except Exception as e:                     # network, API shape, rate limit: no update, never half of one
        return 0, "self-update: no update (%s)" % (str(e)[:200] or type(e).__name__)
    with tempfile.TemporaryDirectory(prefix="harness-update-") as t:
        try:
            src = _extract(blob, Path(t))
        except (ValueError, tarfile.TarError, OSError) as e:
            _record(hh, result="refused", version=have, newest=rel["tag"], why=str(e))
            return 1, "self-update: %s refused: %s" % (rel["tag"], e)
        cmd = update_cmd or [sys.executable, str(src / "bin" / "harness"), "--home", str(home), "update",
                             "--source", str(src)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if p.returncode != 0:
            _record(hh, result="failed", version=have, newest=rel["tag"], why=(p.stderr or p.stdout)[-300:])
            return 1, "self-update: harness update to %s failed: %s" % (rel["tag"], (p.stderr or p.stdout)[-300:])
    d = subprocess.run(doctor_cmd or [sys.executable, "-m", "agent_harness.cli", "--home", str(home), "doctor"],
                       capture_output=True, text=True, timeout=300,
                       env=dict(os.environ, PYTHONPATH=str(hh / "lib")))
    _record(hh, result="updated" if d.returncode == 0 else "updated-doctor-failed", version=rel["tag"],
            previous=have, doctor=d.returncode)
    if d.returncode != 0:
        return 1, "self-update: updated to %s, but harness doctor fails: %s" % (rel["tag"], d.stdout[-300:])
    return 0, "self-update: %s -> %s (%s); harness doctor OK" % (have, rel["tag"], why)
