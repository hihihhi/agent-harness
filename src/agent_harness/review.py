"""harness review: a fresh-context review of the current changes, and a gate that fails on any must-fix.

    harness review --run [--base REF] [--out FILE]   review the diff, write the findings file
    harness review --gate FILE                       exit 0 only for a valid file with no must-fix

No review tool blocks anything by default (Claude's check run is neutral, Copilot only comments), so the gate
is here, and a missing or invalid findings file FAILS it: an absent review must never read as a clean one. The
reviewer is the `review` routing tier in a fresh `claude -p` (or `codex exec`) context, told to verify each
finding by reading the code or running a command before reporting it, because same-model agreement is not
evidence; a finding it could not verify is reported as such, not dropped silently.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from . import installer as I

SCHEMA = {
    "type": "object", "required": ["findings"], "additionalProperties": False,
    "properties": {"findings": {"type": "array", "items": {
        "type": "object", "required": ["severity", "file", "summary", "verified"], "additionalProperties": False,
        "properties": {
            "severity": {"type": "string", "enum": ["must-fix", "suggestion"]},
            "file": {"type": "string"}, "line": {"type": "integer"},
            "summary": {"type": "string"}, "evidence": {"type": "string"},
            "verified": {"type": "boolean"}}}}}}

INSTRUCTIONS = """
You are reviewing in a fresh context: you did not write these changes, and you treat them as wrong until shown
otherwise. Review the diff of {scope}.
Before reporting any finding, VERIFY it: read the code it depends on, or run the command or test that shows it.
Set "verified": true only when you did. Report an unverified suspicion only as a "suggestion" with
"verified": false. "must-fix" is for a verified defect: wrong behaviour, a security or data-loss risk, or a
weakened test. Give file and line, the evidence (the command and what it printed, or the line), and the fix.
Answer with the JSON object only: {{"findings": [...]}}. An empty list means you found nothing important.
"""


def gate(path: str) -> Tuple[int, str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return 2, "review: no valid findings file at %s (%s); a missing review is not a clean one" % (path, e)
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return 2, "review: %s has no findings list; a malformed review is not a clean one" % path
    found = data["findings"]
    for k, f in enumerate(found):     # a finding the gate cannot read is a review it cannot pass
        if not isinstance(f, dict) or f.get("severity") not in ("must-fix", "suggestion") \
                or not all(isinstance(f.get(x), str) and f.get(x) for x in ("file", "summary")):
            return 2, "review: finding %d in %s is malformed (%r); fix the review, it is not passed" % (k, path, f)
    must = [f for f in found if f["severity"] == "must-fix"]
    if must:
        lines = ["  %s:%s %s" % (f.get("file", "?"), f.get("line", "?"), f.get("summary", "")) for f in must]
        return 1, "review: %d must-fix\n%s" % (len(must), "\n".join(lines))
    return 0, "review: no must-fix (%d suggestions)" % (len(found) - len(must))


def _prompt(scope: str) -> str:
    base = ""
    for p in (I.REPO_ROOT / "content" / "prompts" / "review.md", I.PKG_DIR / "content" / "prompts" / "review.md"):
        if p.is_file():
            base = p.read_text(encoding="utf-8")
            break
    return base + INSTRUCTIONS.format(scope=scope)


def _tier(home: Path) -> Tuple[Optional[str], Optional[str], bool]:
    """(model, effort, agent file exists) for the review tier, from the agent file the installer wrote."""
    p = home / ".claude" / "agents" / "harness-review.md"
    out = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[1:]:
            if line.strip() == "---":
                break
            m = re.match(r"^(model|effort):\s*(\S+)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2)
    except OSError:
        return None, None, False
    effort = out.get("effort")
    return out.get("model"), effort if effort in ("low", "medium", "high", "xhigh", "max") else None, True


def _parse(stdout: str) -> Optional[dict]:
    """The findings object from `claude -p --output-format json` (structured_output, else the result text)."""
    try:
        o = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(o, dict) and isinstance(o.get("structured_output"), dict):
        return o["structured_output"]
    text = o.get("result") if isinstance(o, dict) else None
    if isinstance(text, str):
        m = re.search(r"\{.*\}", text, re.S)
        try:
            v = json.loads(m.group(0)) if m else None
        except ValueError:
            v = None
        return v if isinstance(v, dict) else None
    return None


def run(base: Optional[str], out: str, home: Path, timeout: int = 1800) -> Tuple[int, str]:
    scope = "the current branch against %s" % base if base else "the uncommitted and staged changes (git diff HEAD)"
    prompt = _prompt(scope)
    runner = os.environ.get("PLAN_RUNNER", "").strip().lower() or ("claude" if shutil.which("claude") else "codex")
    if runner == "codex":
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(SCHEMA, f)
        cmd = ["codex", "exec", "--json", "--sandbox", "read-only", "--output-schema", f.name, prompt]
    else:
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
               "--setting-sources", "user", "--strict-mcp-config"]
        model, effort, has_agent = _tier(home)
        if has_agent:
            cmd += ["--agent", "harness-review"]
        cmd += (["--model", model] if model else []) + (["--effort", effort] if effort else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 2, "review: the reviewer could not run (%s)" % e
    if runner == "codex":
        found = None
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            item = ev.get("item") if isinstance(ev, dict) else None
            if isinstance(item, dict) and item.get("type") in ("agent_message", "assistant_message"):
                found = _parse(json.dumps({"result": item.get("text", "")})) or found
    else:
        found = _parse(p.stdout)
    if not isinstance(found, dict) or not isinstance(found.get("findings"), list):
        return 2, "review: the reviewer returned no findings object (exit %s): %s" % (
            p.returncode, (p.stdout or p.stderr)[-300:])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(found, indent=1) + "\n", encoding="utf-8")
    n = len(found["findings"])
    must = sum(1 for f in found["findings"] if isinstance(f, dict) and f.get("severity") == "must-fix")
    return 0, "review: %d finding(s), %d must-fix -> %s (gate it: harness review --gate %s)" % (n, must, out, out)


def default_out(root: Path) -> str:
    return str(root / ".agent-harness-review.json")
