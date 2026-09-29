#!/usr/bin/env python3
"""check_guard: ask before a check is weakened (Claude Code PreToolUse on Edit|Write|MultiEdit).

An edit to an EXISTING test or gate file that removes an assertion, changes what one expects (a literal in
it; renamed names do not count), adds a skip/xfail/only, or changes a tolerance gets permissionDecision "ask" with the reason, so the human decides (inform-and-confirm; in a
non-interactive run nobody can say yes, so the edit is refused). New test files, and edits that keep every
assertion, pass untouched. Reads the hook JSON on stdin; prints nothing when it has no objection; never
fails the tool (any error = no objection).

OFF by default (v0.1.1 eval: no tampering in any of 36 coding runs, so no measurable gain): it acts only
with HARNESS_ENABLE containing "check_guard"; HARNESS_DISABLE wins (the A/B arm switch).
"""
import json
import os
import re
import sys
from pathlib import Path

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$|"
                       r"(^|/)conftest\.py$|(^|/)\.github/workflows/|(^|/)gates?([-_][^/]*)?\.(sh|py)$")
ASSERT = re.compile(r"\bassert\b|\bassert[A-Z]\w*\s*\(|\bexpect\s*\(|\bshould\b\.|\.to(Be|Equal|Throw)\w*\(|"
                    r"\bassert_\w+\s*\(|pytest\.raises|assertRaises")
SKIP = re.compile(r"(pytest\.mark\.(skip|skipif|xfail)|pytest\.skip\s*\(|unittest\.skip|skipIf|skipUnless|"
                  r"expectedFailure|\bxit\s*\(|\bxdescribe\s*\(|\.skip\s*\(|\.only\s*\(|\bit\.todo|continue-on-error)")
TOL = re.compile(r"\b(rel|abs|atol|rtol|delta|places|tol|tolerance|epsilon|eps)\s*=\s*([0-9][0-9._e+-]*)", re.I)


def _count(rx, text):
    return len(rx.findall(text or ""))


def _tols(text):
    return sorted(TOL.findall(text or ""))


def _asserts(text):
    """The assertion lines with every name masked, so a rename is not a change but a new expected value is."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ASSERT.search(ln)]
    return [re.sub(r"[A-Za-z_][A-Za-z0-9_]*", "N", ln) for ln in lines]


def objections(old: str, new: str) -> list:
    out = []
    a, b = _count(ASSERT, old), _count(ASSERT, new)
    if b < a:
        out.append("removes %d assertion(s)" % (a - b))
    else:
        left = list(_asserts(new))
        gone = 0
        for ln in _asserts(old):
            if ln in left:
                left.remove(ln)
            else:
                gone += 1
        if gone:
            out.append("changes what %d assertion(s) expect" % gone)
    s0, s1 = _count(SKIP, old), _count(SKIP, new)
    if s1 > s0:
        out.append("adds a skip/xfail/only")
    if _tols(old) != _tols(new) and _tols(old):
        out.append("changes a tolerance")
    return out


def pairs(tool: str, inp: dict, path: Path):
    """(old, new) text pairs the edit would make."""
    if tool == "Edit":
        return [(inp.get("old_string", ""), inp.get("new_string", ""))]
    if tool == "MultiEdit":
        return [(e.get("old_string", ""), e.get("new_string", "")) for e in inp.get("edits") or []]
    if tool == "Write":
        try:
            old = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            old = ""
        return [(old, inp.get("content", ""))]
    return []


def relative(path: Path, cwd: str) -> str:
    """The path inside its project (git root, else the session's cwd), so /home/Test/... or a checkout under
    a folder called test/ does not make every file look like a test."""
    d = path.parent
    for cand in (d,) + tuple(d.parents):
        try:
            if (cand / ".git").exists():
                return path.relative_to(cand).as_posix()
        except (OSError, ValueError):
            continue
    try:
        return path.relative_to(Path(cwd)).as_posix()
    except ValueError:
        return path.name


def decide(data: dict):
    names = lambda v: {x.strip() for x in os.environ.get(v, "").split(",")}  # noqa: E731
    if "check_guard" in names("HARNESS_DISABLE") or "check_guard" not in names("HARNESS_ENABLE"):
        return None
    tool = data.get("tool_name", "")
    inp = data.get("tool_input") or {}
    fp = inp.get("file_path") or inp.get("path") or ""
    if not fp or tool not in ("Edit", "Write", "MultiEdit"):
        return None
    path = Path(fp if os.path.isabs(fp) else os.path.join(data.get("cwd") or os.getcwd(), fp))
    rel = relative(path, data.get("cwd") or os.getcwd())
    if not TEST_FILE.search(rel) or not path.is_file():
        return None  # not a check, or a new file
    ps = pairs(tool, inp, path)       # a MultiEdit that moves an assertion counts once, as a whole
    found = objections("\n".join(o for o, _ in ps), "\n".join(n for _, n in ps))
    if not found:
        return None
    reason = ("This edit weakens a check in %s (%s). Never weaken a test or gate to make it pass: if the check "
              "itself is wrong, stop and explain why to the human instead." % (path.name, "; ".join(sorted(set(found)))))
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}


def main():
    try:
        out = decide(json.loads(sys.stdin.read() or "{}"))
        if out:
            print(json.dumps(out))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
