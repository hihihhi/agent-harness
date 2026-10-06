"""Candidate lessons, captured at the end of every turn by the Stop hook, without the model having to remember.

Only two signals count, because a lesson is only as good as the signal it came from (Reflexion got WORSE on
MBPP when 16% of its "passing" tests were false positives):
  gate  a check that failed and then passed in the same turn: run_checks, `plan gate`, or a test command
  user  an explicit correction from the user ("no, ...", "don't ...", "use X instead")
They are kept apart (source), redacted, deduplicated with a count, and written to
~/.agent-harness/candidates.jsonl, never straight into lessons: `harness improve` promotes a repeated one, and
only behind an eval (append small deltas and curate; rewriting or hoarding memory made agents worse in ACE and
Dynamic Cheatsheet). The store is capped by agent_harness.storage.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import List, Optional

from .redact import redact

STORE = "candidates.jsonl"
# A check is a command whose PROGRAM is a test runner or linter, in any step of the command line: `grep -rn
# pytest docs/` names pytest and is not one. Parsed with shlex, so text in quotes or a heredoc is never a step;
# wrappers are skipped (VAR=1, env, time, timeout N, nice, sudo -u X, xvfb-run, uv/poetry/pdm/hatch run, npx,
# any python[3[.x]] -m, a venv's or /usr/bin's python); --version/--help is not a check run.
WRAPPERS = {"time", "nohup", "xvfb-run", "npx", "command", "exec"}
# Wrappers whose own options come before the wrapped command: {name: options that take a value}.
OPT_WRAPPERS = {"env": {"-u", "--unset", "-C", "--chdir"}, "nice": {"-n"}, "timeout": {"-s", "-k", "--signal", "--kill-after"}, "sudo": {"-u", "-g"},
                "ionice": {"-c", "-n"}}
RUNNERS = {"pytest", "py.test", "unittest", "mypy", "tsc", "phase-check", "phase-check.py"}
RUFF_SUBCOMMANDS = {"format", "version", "rule", "config", "linter", "clean", "server", "analyze", "help"}
HEREDOC_BODY = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\s*(\n|$)", re.S)


def _steps(cmd: str) -> List[List[str]]:
    import shlex
    cmd = HEREDOC_BODY.sub("\n", cmd)
    lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        return []
    out, cur = [], []
    for t in toks:
        if t and set(t) <= set(";&|()"):        # a subshell's parentheses separate steps like ; does
            out.append(cur)
            cur = []
        else:
            cur.append(t)
    out.append(cur)
    return [x for x in out if x]


def _skip_opts(w: List[str], i: int, valued) -> int:
    while i < len(w) and w[i].startswith("-"):
        i += 2 if w[i] in valued else 1
    return i


def _is_check_step(w: List[str]) -> bool:
    i = 0
    while i < len(w):
        t = w[i]
        b = t.rsplit("/", 1)[-1]
        if re.match(r"^[A-Za-z_]\w*=", t) or b in WRAPPERS:
            i += 1
        elif b in OPT_WRAPPERS:
            i = _skip_opts(w, i + 1, OPT_WRAPPERS[b])
            if b == "timeout" and i < len(w):
                i += 1                                   # the duration
        elif b in ("uv", "poetry", "pdm", "hatch") and i + 1 < len(w) and w[i + 1] == "run":
            i = _skip_opts(w, i + 2, {"--with", "--python", "-p", "--project", "--directory", "--env-file"})
        elif re.match(r"^python[\d.]*$", b):
            j = i + 1                                    # python's own flags (-u, -X dev, ...) before -m
            while j < len(w) and w[j].startswith("-") and w[j] != "-m":
                j += 2 if w[j] in ("-X", "-W") else 1
            if j + 1 < len(w) and w[j] == "-m":
                w, i = [w[j + 1]] + w[j + 2:], 0
            else:
                return False                             # python <script>: not a known check
        else:
            break
    if i >= len(w):
        return False
    prog, rest = w[i].rsplit("/", 1)[-1], w[i + 1:]
    if any(a in ("--version", "--help", "-h", "-V") for a in rest):
        return False
    if prog in RUNNERS:
        return True
    if prog == "ruff":
        return not rest or rest[0] == "check" or (rest[0] not in RUFF_SUBCOMMANDS)   # `ruff src/` is a check
    if prog in ("go", "cargo"):
        return bool(rest) and rest[0] == "test"
    if prog in ("npm", "yarn", "pnpm"):
        script = rest[1] if rest[:1] == ["run"] and len(rest) > 1 else (rest[0] if rest else "")
        return script == "test" or script.startswith("test:")      # not `npm install test`
    if prog == "make":
        return any(t in ("test", "check") for t in rest)
    if prog in ("plan", "plan.py"):
        return bool(rest) and rest[0] == "gate"
    return False


def is_check(cmd: str) -> bool:
    return any(_is_check_step(step) for step in _steps(cmd))


# Claude Code's Bash tool reports a failing command as a result that STARTS with "Exit code N"; the same words
# later in passing output ("the child exited with code 1 as expected") are output, not a status.
EXIT_LINE = re.compile(r"\A\s*Exit code (-?\d+)")

# Strict on purpose: "Stop the server" is an instruction, not a correction.
CORRECTION = re.compile(r"^\s*(no[,.!]|nope\b|don'?t\b|do not\b|wrong\b|that'?s (wrong|not)|not like that)"
                        r"|\b(instead of|use \S+ (instead|not))\b", re.I)
FAIL_LINE = re.compile(r"(FAILED|ERROR|Error|Traceback|assert|FAIL\b|failed)")
MAX_TEXT = 300


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return ""


TAIL_BYTES = 4 * 1024 * 1024


def _last_turn(path: str) -> list:
    return _last_turn_info(path)[0]


def _last_turn_info(path: str):
    """(events of the last user turn, whether its start is in view). Only the file's tail is read: a long
    session's transcript runs to tens of megabytes, and this runs at the end of every turn. A turn longer than
    the window has no plain user message in it; the caller must then not treat it as a new turn. Known limit:
    a NEW turn whose start is also out of the window is then taken for the old one, so a repeat inside it can
    be missed (an undercount; it can never make one occurrence look like two)."""
    events, started = [], False
    with open(path, "rb") as fb:
        size = fb.seek(0, 2)
        fb.seek(max(0, size - TAIL_BYTES))
        if size > TAIL_BYTES:
            fb.readline()                         # a partial first line
        lines = fb.read().decode("utf-8", "replace").splitlines()
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        content = (e.get("message") or {}).get("content")
        plain = e.get("type") == "user" and (isinstance(content, str) or any(
            isinstance(c, dict) and c.get("type") == "text" for c in content or []))
        if plain:
            events, started = [], True
        events.append(e)
    return events, started


def _checks(events) -> List[dict]:
    """[(command, exit, output)] for each check-like tool call in order, matched to its result."""
    calls, out = {}, []
    for e in events:
        for c in (e.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                name, inp = str(c.get("name", "")), c.get("input") or {}
                if name.endswith("run_checks"):
                    calls[c.get("id")] = "run_checks"
                elif name.endswith("__plan") and str(inp.get("args", "")).startswith("gate"):
                    calls[c.get("id")] = "plan " + str(inp.get("args"))
                elif name == "Bash" and is_check(str(inp.get("command", ""))):
                    calls[c.get("id")] = str(inp.get("command"))   # whole: it is redacted before any cut
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                text = _text(c.get("content"))
                code = None
                try:
                    code = int(json.loads(text).get("exit"))
                except (ValueError, TypeError, AttributeError):
                    m = EXIT_LINE.match(text)
                    code = int(m.group(1)) if m else (1 if c.get("is_error") else 0)
                out.append({"cmd": calls[c["tool_use_id"]], "exit": code, "text": text})
    return out


def _key(source: str, text: str) -> str:
    norm = re.sub(r"[0-9]+", "#", re.sub(r"\s+", " ", text.lower())).strip()
    return hashlib.sha1((source + ":" + norm).encode()).hexdigest()[:16]


def _norm(cmd: str) -> str:
    return re.sub(r"\s+", " ", cmd).strip()


def candidates(transcript: str, events: Optional[list] = None) -> List[dict]:
    events = _last_turn(transcript) if events is None else events
    found = []
    first = events[0] if events else {}
    prompt = _text((first.get("message") or {}).get("content")) if first.get("type") == "user" else ""
    if prompt and CORRECTION.search(prompt):
        t = redact(prompt.strip())[:MAX_TEXT]       # redact the whole text first, then cut
        found.append({"source": "user", "summary": t, "evidence": t, "key": _key("user", t)})
    checks = _checks(events)
    for i, c in enumerate(checks):
        if c["exit"] == 0:
            continue
        fixed = next((d for d in checks[i + 1:] if _norm(d["cmd"]) == _norm(c["cmd"]) and d["exit"] == 0), None)
        if fixed is None:
            continue
        text = redact(c["text"].replace("\\n", "\n"))   # whole text first: a key cut in half escapes the pattern
        lines = [x for x in text.splitlines() if FAIL_LINE.search(x)][:3]
        ev = ("\n".join(lines) or text)[:MAX_TEXT]
        cmd = redact(c["cmd"])[:MAX_TEXT]
        found.append({"source": "gate", "summary": "`%s` failed, then passed after a change" % cmd,
                      "evidence": ev, "key": _key("gate", c["cmd"] + "|" + (lines[0] if lines else ""))})
        break                                    # one lesson per turn: the first thing that was wrong
    return found


def capture_turn(hh: Path, transcript: str, session: str) -> List[dict]:
    """The Stop hook's entry point. A turn can end in more than one Stop (after the harness's own block, the
    model works on and stops again), so the marker names the turn and the lessons already recorded for it, and
    a later Stop of the same turn records only what is new: one fail-then-pass must count once, or a single
    occurrence would pass the "has recurred" rule harness improve depends on."""
    events, started = _last_turn_info(transcript)
    first = events[0] if events else {}
    turn = "%s:%s" % (transcript, first.get("uuid") or hashlib.sha1(
        json.dumps(first, sort_keys=True).encode()).hexdigest())
    mark = hh / "state" / ("capture-%s" % re.sub(r"[^\w.-]", "_", session or "x"))
    seen = []
    try:
        m = json.loads(mark.read_text(encoding="utf-8"))
        if not started and str(m.get("turn", "")).startswith(transcript + ":"):
            turn = m["turn"]          # the turn's start scrolled out of the window: it is the same turn
        seen = m.get("keys", []) if m.get("turn") == turn else []
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    new = [f for f in candidates(transcript, events) if f["key"] not in seen]   # the transcript is parsed once
    rows = record(hh, new)
    # The marker is written BEFORE anything else can fail: if pruning raised first, the next Stop of this turn
    # would count the same lesson again (one occurrence would then pass the "has recurred" rule).
    mark.parent.mkdir(parents=True, exist_ok=True)
    mark.write_text(json.dumps({"turn": turn, "keys": seen + [f["key"] for f in new]}), encoding="utf-8")
    try:
        from . import storage
        storage.prune(hh)                        # capture runs every turn, so the cap is enforced here
    except Exception:
        pass
    return rows


def record(hh: Path, found: List[dict], now: Optional[str] = None) -> List[dict]:
    """Merge into the store: a repeat raises the count of the entry it repeats."""
    if not found:
        return []
    now = now or time.strftime("%Y-%m-%dT%H:%M:%S")
    path = hh / STORE
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    by_key = {r.get("key"): r for r in rows if r.get("key")}
    for f in found:
        r = by_key.get(f["key"])
        if r:
            r["count"] = int(r.get("count", 1)) + 1
            r["last"] = now
            rows.remove(r)
            rows.append(r)                       # most recent last, so pruning drops the stalest first
        else:
            r = dict(f, id=f["key"][:12], count=1, first=now, last=now)
            rows.append(r)
            by_key[f["key"]] = r
    hh.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)
    return rows
