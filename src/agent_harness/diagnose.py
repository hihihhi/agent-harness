"""`harness explain`: a receipt of what a session actually used, from files on disk and the session's transcript.

installed  the client's rules file exists and carries the harness rules
loaded     the session itself records those rules (Codex logs them; Claude Code does not: NOT_OBSERVABLE)
selected   skills the session was offered (Codex logs them; Claude Code: NOT_OBSERVABLE)
invoked    skills, harness memory/knowledge tools, workers and checks the transcript shows being called
verified   checks with their exit codes
Nothing is guessed: a signal the client does not record is NOT_OBSERVABLE. No message text is copied, only tool
names, skill names and check commands (which can hold secrets in theory, so they are passed through redaction).
"""
import hashlib
import json
import re
from pathlib import Path

from .capture import EXIT_LINE, is_check
from .redact import redact

NOT_OBSERVABLE = "NOT_OBSERVABLE"
MARK = "# Working rules"
RULES = {"claude-code": ".claude/CLAUDE.md", "codex": ".codex/AGENTS.md"}
SKILL_READ = re.compile(r"skills/([\w.-]+)/SKILL\.md")
SKILL_LINE = re.compile(r"^- ([\w.:-]+): ", re.M)


def _events(path):
    out = []
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def _rules_on_disk(client, home):
    f = Path(home) / RULES[client]
    if not f.is_file():
        return {"path": str(f), "installed": "MISSING"}
    data = f.read_bytes()
    return {"path": str(f), "installed": "yes" if MARK.encode() in data else "no",
            "sha256": hashlib.sha256(data).hexdigest()[:12], "bytes": len(data)}


def _check(cmd, out=None, is_error=False):
    m = EXIT_LINE.search(out or "") if out is not None else None
    if m:
        code = int(m.group(1))
    elif out is None:
        code = NOT_OBSERVABLE
    else:
        code = 1 if is_error else 0
    return {"command": redact(cmd)[:160], "exit": code}


def _claude(events, r):
    r["instructions"]["loaded_in_session"] = NOT_OBSERVABLE
    r["skills"]["available"] = NOT_OBSERVABLE
    results, pending, usage, seen = {}, [], {}, set()
    for e in events:
        msg = e.get("message") or {}
        if e.get("isCompactSummary") or e.get("type") == "summary":
            r["compactions"] += 1
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if c.get("type") == "tool_result":
                text = c.get("content")
                if isinstance(text, list):
                    text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                results[c.get("tool_use_id")] = (str(text or ""), bool(c.get("is_error")))
            if c.get("type") != "tool_use":
                continue
            name, inp = c.get("name", ""), c.get("input") or {}
            if name == "Skill":
                r["skills"]["invoked"].append(inp.get("skill") or inp.get("name") or "?")
            elif name.startswith("mcp__harness__"):
                r["memory"].append({"tool": name[len("mcp__harness__"):], "args": sorted(inp)})
            elif name in ("Agent", "Task"):
                r["workers"].append("%s:%s" % (name, inp.get("subagent_type", "general")))
            elif name == "Bash":
                cmd = inp.get("command", "")
                if is_check(cmd):
                    pending.append((c.get("id"), cmd))
                elif cmd.lstrip().startswith("codex "):
                    r["workers"].append("codex-cli")
        if e.get("type") == "assistant" and msg.get("usage") and msg.get("id") not in seen:
            seen.add(msg.get("id"))
            u = msg["usage"]
            for k, v in (("input", "input_tokens"), ("output", "output_tokens"),
                         ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
                usage[k] = usage.get(k, 0) + int(u.get(v) or 0)
    for tid, cmd in pending:
        out, err = results.get(tid, (None, False))
        r["checks"].append(_check(cmd, out, err))
    r["tokens"] = usage or NOT_OBSERVABLE


def _codex(events, r):
    loaded, usage = None, None
    for e in events:
        p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if e.get("type") == "world_state":
            st = p.get("state") or {}
            text = (st.get("agents_md") or {}).get("text")
            if text is not None:
                loaded = MARK in text
            skills = st.get("host_skills")
            if isinstance(skills, dict):          # Codex 0.16x: a markdown body, one "- name: description" per skill
                skills = SKILL_LINE.findall(skills.get("body") or "")
            if isinstance(skills, list):
                r["skills"]["available"] = sorted({s.get("name") if isinstance(s, dict) else str(s) for s in skills})
        elif p.get("type") == "custom_tool_call":
            cmd = str(p.get("input") or "")
            r["skills"]["invoked"] += [s for s in SKILL_READ.findall(cmd) if s not in r["skills"]["invoked"]]
            inner = re.findall(r'cmd:\s*"((?:[^"\\]|\\.)*)"', cmd)
            for c in inner:
                c = json.loads('"%s"' % c)
                if is_check(c):
                    r["checks"].append(_check(c, None))
                if c.lstrip().startswith("codex "):
                    r["workers"].append("codex-cli")
        elif p.get("type") == "function_call" and (p.get("namespace") == "harness"
                                                    or str(p.get("name", "")).startswith("mcp__harness__")):
            try:
                args = sorted(json.loads(p.get("arguments") or "{}"))
            except ValueError:
                args = []
            r["memory"].append({"tool": str(p.get("name")).replace("mcp__harness__", ""), "args": args})
        elif p.get("type") == "token_count":
            t = (p.get("info") or {}).get("total_token_usage") or {}
            usage = {"input": t.get("input_tokens", 0), "cache_read": t.get("cached_input_tokens", 0),
                     "output": t.get("output_tokens", 0), "reasoning": t.get("reasoning_output_tokens", 0)}
        elif p.get("type") in ("context_compacted", "compacted") or e.get("type") == "compacted":
            r["compactions"] += 1
    r["instructions"]["loaded_in_session"] = NOT_OBSERVABLE if loaded is None else ("yes" if loaded else "no")
    r["tokens"] = usage or NOT_OBSERVABLE


def explain(client, transcript, home):
    if client not in RULES:
        raise ValueError("client must be one of %s" % sorted(RULES))
    r = {"client": client, "transcript": str(transcript), "instructions": _rules_on_disk(client, home),
         "skills": {"available": [], "invoked": []}, "memory": [], "checks": [], "workers": [],
         "compactions": 0, "tokens": NOT_OBSERVABLE, "omissions": []}
    (_claude if client == "claude-code" else _codex)(_events(transcript), r)
    om = r["omissions"]
    if r["instructions"]["installed"] == "MISSING":
        om.append("rules file missing: %s" % r["instructions"]["path"])
    elif r["instructions"]["installed"] == "no":
        om.append("rules file lacks the harness rules")
    if r["instructions"]["loaded_in_session"] == "no":
        om.append("harness rules not loaded in this session")
    if not r["skills"]["invoked"]:
        om.append("no skill invoked")
    if not r["checks"]:
        om.append("no check run")
    failed = [c for c in r["checks"] if c["exit"] not in (0, NOT_OBSERVABLE)]
    if failed:
        om.append("%d of %d checks failed (last check exit %s)" % (len(failed), len(r["checks"]),
                                                                   r["checks"][-1]["exit"]))
    return r


def render(r):
    ins, sk = r["instructions"], r["skills"]
    tok = r["tokens"] if r["tokens"] == NOT_OBSERVABLE else ", ".join("%s %s" % kv for kv in r["tokens"].items())
    avail = sk["available"] if sk["available"] == NOT_OBSERVABLE else len(sk["available"])
    codes = [c["exit"] for c in r["checks"]]
    checks = ("%d passed, %d failed, %d unobserved; last exit %s" % (
        codes.count(0), len([c for c in codes if c not in (0, NOT_OBSERVABLE)]), codes.count(NOT_OBSERVABLE),
        codes[-1])) if codes else "none"
    lines = ["%s session %s" % (r["client"], Path(r["transcript"]).name),
             "rules     installed %s%s, loaded in session %s" % (
                 ins["installed"], " (%s B, sha %s)" % (ins["bytes"], ins["sha256"]) if "bytes" in ins else "",
                 ins["loaded_in_session"]),
             "skills    available %s, invoked %s" % (avail, ", ".join(sorted(set(sk["invoked"]))) or "none"),
             "harness   %d tool call(s): %s" % (len(r["memory"]), ", ".join(sorted({m["tool"] for m in r["memory"]}))
                                                or "none"),
             "checks    %s" % checks,
             "workers   %s" % (", ".join("%s x%d" % (w, r["workers"].count(w)) for w in sorted(set(r["workers"])))
                                or "none"),
             "compacted %d time(s)" % r["compactions"],
             "tokens    %s" % tok]
    if r["omissions"]:
        lines.append("omitted   " + "; ".join(r["omissions"]))
    return "\n".join(lines)


def latest(client, home, project=None):
    """The newest transcript for this client (Claude: of the project's folder, when given)."""
    home = Path(home)
    if client == "claude-code":
        base = home / ".claude" / "projects"
        if project:
            base = base / re.sub(r"[^A-Za-z0-9]", "-", str(Path(project).resolve()))
        files = list(base.glob("*.jsonl") if project else base.glob("*/*.jsonl"))
    else:
        files = list((home / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"))
    return max(files, key=lambda f: f.stat().st_mtime) if files else None
