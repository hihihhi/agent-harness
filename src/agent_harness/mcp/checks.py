"""run_checks: find the project's own checks, run them, return only the failures (<= 2 KB).

The result is recorded per project (HARNESS_HOME/state/<key>/checks.json: command, exit code, time and a
fingerprint of the working tree), so the Stop hook can tell whether files changed since the last passing
run. Never edits anything. Discovery, first match wins:
    pytest      pyproject.toml [tool.pytest*] / pytest.ini / setup.cfg [tool:pytest] / tox.ini [pytest] /
                conftest.py, or a tests|test dir with test_*.py        -> python3 -m pytest -q
    npm         package.json with scripts.test                         -> npm test --silent
    make        Makefile with a `test:` target                         -> make test
    unittest    a tests|test dir with *test*.py (no pytest installed)  -> python3 -m unittest discover
Arm switch (for A/B evals): HARNESS_DISABLE containing "run_checks" hides the tool and the Stop gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness.mcp.kb import harness_home  # type: ignore
    from agent_harness.mcp.state import project_key, project_root  # type: ignore
else:
    from .kb import harness_home
    from .state import project_key, project_root

CAP = 2048
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = 1800
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".pytest_cache", ".mypy_cache"}
FAIL_LINE = re.compile(r"^(FAILED |ERROR |FAIL: |ERROR: |E  |.*\bError\b|.*Traceback|.*\bfailed\b|.*\bfailing\b|"
                       r"\s+at .*:\d+|.*assert)", re.I)


def disabled(name: str) -> bool:
    return name in {x.strip() for x in os.environ.get("HARNESS_DISABLE", "").split(",")}


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has_tests(root: Path, pattern: str) -> bool:
    for d in ("tests", "test"):
        if (root / d).is_dir() and any((root / d).rglob(pattern)):
            return True
    return False


def _pytest_available() -> bool:
    try:
        return subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def discover(root: Path) -> Optional[List[str]]:
    """The project's test command, or None when it has none that can be found."""
    pyproject = _read(root / "pyproject.toml")
    if ("[tool.pytest" in pyproject or (root / "pytest.ini").is_file() or "[tool:pytest]" in _read(root / "setup.cfg")
            or "[pytest]" in _read(root / "tox.ini") or (root / "conftest.py").is_file()
            or (_has_tests(root, "test_*.py") and _pytest_available())):
        return [sys.executable, "-m", "pytest", "-q"]
    try:
        pkg = json.loads(_read(root / "package.json") or "{}")
    except ValueError:
        pkg = {}
    if isinstance(pkg, dict) and (pkg.get("scripts") or {}).get("test"):
        return ["npm", "test", "--silent"]
    if re.search(r"^test\s*:", _read(root / "Makefile"), re.M):
        return ["make", "test"]
    if _has_tests(root, "*test*.py"):
        d = "tests" if (root / "tests").is_dir() else "test"
        return [sys.executable, "-m", "unittest", "discover", "-s", d, "-q"]
    return None


def fingerprint(root: Path) -> str:
    """A hash of the project's files (path, size, mtime), ignoring caches and VCS dirs."""
    h = hashlib.sha1()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP)
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(("%s\0%d\0%d\n" % (os.path.relpath(p, root), st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()


def failures(out: str, cap: int = CAP) -> str:
    """Only what explains a failure: failing-test and error lines, then the last lines (the summary)."""
    lines = out.splitlines()
    keep = [ln for ln in lines if FAIL_LINE.match(ln)]
    tail = lines[-8:]
    text = "\n".join(dict.fromkeys(keep + ["..."] + tail)) if keep else "\n".join(tail)
    b = text.encode("utf-8")
    if len(b) > cap:
        text = b[: cap - 60].decode("utf-8", "ignore") + "\n[cut: %d of %d bytes]" % (cap - 60, len(b))
    return text


def record_path(root: Path, home=None) -> Path:
    return harness_home(home) / "state" / project_key(project_root(root)) / "checks.json"


def run_checks(cmd: str = "", timeout: int = DEFAULT_TIMEOUT, project: str = "", home=None) -> dict:
    root = project_root(project or None)
    argv: Optional[List[str]]
    shell = False
    if cmd.strip():
        argv, shell = [cmd], True
    else:
        argv = discover(root)
    if not argv:
        return {"exit": None, "command": None, "project": str(root),
                "output": "No checks found (no pytest/npm/make/unittest setup). Pass cmd= with the project's check "
                          "command, or say that the project has none."}
    timeout = max(10, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    t0 = time.time()
    try:
        p = subprocess.run(argv[0] if shell else argv, shell=shell, cwd=str(root), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        code, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        code = 124
        out = ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        out += "\n[timed out after %d s]" % timeout
    except OSError as e:
        code, out = 127, str(e)
    shown = " ".join(argv) if not shell else argv[0]
    rec = {"command": shown, "exit": code, "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "seconds": round(time.time() - t0, 1), "fingerprint": fingerprint(root)}
    f = record_path(root, home)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec), encoding="utf-8")
    except OSError:
        pass
    body = ("passed. " + failures(out, 400).splitlines()[-1]) if code == 0 and out.strip() else failures(out)
    return {"exit": code, "command": shown, "project": str(root), "seconds": rec["seconds"],
            "output_bytes": len(out.encode("utf-8")), "output": body}


def unchecked_changes(root: Path, home=None) -> Tuple[bool, str]:
    """(True, why) when files changed since the last PASSING run_checks for this project."""
    root = project_root(root)
    try:
        rec = json.loads(record_path(root, home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, "run_checks has not been run in this project"
    if rec.get("exit") != 0:
        return True, "the last run_checks failed (exit %s)" % rec.get("exit")
    if rec.get("fingerprint") != fingerprint(root):
        return True, "files changed since the last passing run_checks"
    return False, ""
