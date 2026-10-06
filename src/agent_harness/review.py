"""harness review: a fresh-context review of the current changes, and a gate that fails on any must-fix.

    harness review --run [--base REF] [--out FILE]   review the diff, write the findings file
    harness review --gate FILE                       exit 0 only for a valid file with no must-fix

No review tool blocks anything by default (Claude's check run is neutral, Copilot only comments), so the gate
is here, and a missing or invalid findings file FAILS it: an absent review must never read as a clean one. A
review is also bound to the code it read: the file records the base commit, the untracked files and a hash of
the diff, and the gate recomputes it, so a review left over from last week never passes for changes nobody
read. (The hash binds a review to code, not to the fact that a reviewer ran: a file assembled by hand with the
same hash would pass; the gate guards against stale reviews, not forged ones.)
An empty diff is refused, not reviewed as clean (after a commit, review the branch with --base). The
reviewer is the `review` routing tier in a fresh `claude -p` (or `codex exec`) context, told to verify each
finding by reading the code or running a command before reporting it, because same-model agreement is not
evidence; a finding it could not verify is reported as such, not dropped silently.
"""
from __future__ import annotations

import hashlib
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




def resolve_base(root: Path, base: str) -> Optional[str]:
    """The commit `base` names, as a SHA, or None. Never passed to git as an option (`--output=...` is not a
    ref), and stored as the SHA so committing the reviewed work does not unbind the review."""
    try:
        p = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", "--end-of-options",
                            base + "^{commit}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = p.stdout.strip()
    return sha if p.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha) else None


def _untracked(root: Path, skip: set) -> Optional[list]:
    try:
        u = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if u.returncode != 0:
        return None
    return sorted(x for x in u.stdout.decode("utf-8", "replace").split("\0")
                  if x and str((root / x).resolve()) not in skip)


def snapshot(root: Path, sha: str, exclude: Tuple[str, ...] = (), untracked: Optional[list] = None):
    """(sha256 over every file the change touches, the untracked list), or (None, None) on a git error, or
    ("", list) when there is nothing to review. A file is hashed as its path and its current bytes, so the
    same code hashes the same whether it is untracked, staged or committed since `sha` (committing the
    reviewed work does not unbind the review). The findings file itself is excluded, wherever it lives."""
    skip = {str(Path(x).resolve()) for x in exclude}
    rel_skip = []
    for x in exclude:
        try:
            rel_skip.append(str(Path(x).resolve().relative_to(root.resolve())))
        except ValueError:
            pass
    cmd = ["git", "-C", str(root), "diff", "--name-only", "-z", sha, "--", "."] + \
          [":(exclude)%s" % r for r in rel_skip]
    try:
        d = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if d.returncode != 0:
        return None, None
    if untracked is None:
        untracked = _untracked(root, skip)
        if untracked is None:
            return None, None
    changed = sorted(set(x for x in d.stdout.decode("utf-8", "replace").split("\0") if x) | set(untracked))
    if not changed:
        return "", untracked
    h = hashlib.sha256()
    for rel in changed:
        h.update(b"\0" + rel.encode() + b"\0")
        try:
            p = root / rel
            h.update(b"x" if os.access(p, os.X_OK) else b"-")     # a mode change is a change (chmod +x)
            h.update(p.read_bytes())
        except OSError:
            h.update(b"\0deleted")
    return h.hexdigest(), untracked


def gate(path: str, root: Optional[Path] = None) -> Tuple[int, str]:
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
    rv = data.get("reviewed") if isinstance(data.get("reviewed"), dict) else {}
    if not rv.get("diff_sha256") or not rv.get("base") or not isinstance(rv.get("untracked"), list):
        return 2, "review: %s is not tied to any code (no reviewed base, diff hash and file list); run harness " \
                  "review --run" % path
    root = Path(root or Path.cwd())
    sha = resolve_base(root, str(rv["base"]))
    if sha is None:
        return 2, "review: the reviewed base %r is not a commit here" % rv["base"]
    now, _ = snapshot(root, sha, exclude=(path,), untracked=[str(x) for x in rv["untracked"]])
    if now != rv["diff_sha256"]:
        return 1, "review: the code changed since %s was reviewed (against %s); review it again" % (path, sha[:12])
    # Every untracked file that appeared since the review fails it, whatever its name (a Makefile, a
    # postinstall in package.json, a .pth file run at start-up are code), except what the reviewer itself
    # left behind while it ran (recorded as artefacts). Ignore anything else in .gitignore, or review again.
    seen = set(rv["untracked"]) | set(str(x) for x in rv.get("artefacts") or [])
    new = [x for x in (_untracked(root, {str(Path(path).resolve())}) or []) if x not in seen]
    if new:
        return 1, "review: files added since the review were never reviewed: %s (review again, or .gitignore " \
                  "them if they are build output)" % ", ".join(new[:5])
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


def run(base: Optional[str], out: str, home: Path, timeout: int = 1800, root: Optional[Path] = None) -> Tuple[int, str]:
    root = Path(root or Path.cwd())
    sha = resolve_base(root, base or "HEAD")
    if sha is None:
        return 2, "review: %r is not a commit in a git repository here" % (base or "HEAD")
    # The snapshot is taken BEFORE the reviewer runs: what it may leave behind (a .coverage from running the
    # tests) is not part of what was reviewed, and an edit it makes to tracked code unbinds the review.
    bound, untracked = snapshot(root, sha, exclude=(out,))
    if bound is None:
        return 2, "review: `git diff %s` failed" % sha[:12]
    if bound == "":
        return 2, "review: nothing to review (the diff against %s is empty); after a commit, use --base REF" % (
            base or "HEAD")
    scope = "the current branch against %s" % base if base else "the uncommitted and staged changes (git diff HEAD)"
    prompt = _prompt(scope)
    runner = os.environ.get("PLAN_RUNNER", "").strip().lower() or ("claude" if shutil.which("claude") else "codex")
    if runner == "codex":
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(SCHEMA, f)
        cmd = ["codex", "exec", "--json", "--sandbox", "read-only", "--output-schema", f.name, prompt]
        schema_file = f.name
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
    finally:
        if runner == "codex":
            try:
                os.unlink(schema_file)
            except OSError:
                pass
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
    after = _untracked(root, {str(Path(out).resolve())}) or []
    found["reviewed"] = {"base": sha, "untracked": untracked, "diff_sha256": bound,
                         "artefacts": [x for x in after if x not in untracked]}   # what the reviewer's run left
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(found, indent=1) + "\n", encoding="utf-8")
    n = len(found["findings"])
    must = sum(1 for f in found["findings"] if isinstance(f, dict) and f.get("severity") == "must-fix")
    return 0, "review: %d finding(s), %d must-fix -> %s (gate it: harness review --gate %s)" % (n, must, out, out)


def default_out(root: Path) -> str:
    return str(root / ".agent-harness-review.json")
