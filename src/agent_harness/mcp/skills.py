"""Procedural skills learned from experience (skill_manage): the agent writes down a procedure it worked out,
so the next session, in any tool, starts from the working path instead of rediscovering it.

    ~/.agents/skills/learned/<name>/SKILL.md     agentskills.io format; Codex, Gemini CLI, Cursor and Copilot
                                                 list it natively (Codex finds nested folders: probed 2026-09-30)
    <HARNESS_HOME>/skills-usage.json             {name: {uses, last}}: views and updates through this tool
    <HARNESS_HOME>/skills-archive/<name>-<time>/ archived skills (the cap, or skill_manage archive): never deleted

Claude Code does not read ~/.agents/skills, and a native listing costs every request; so for Claude the
per-prompt recall hook names a learned skill only when the prompt is about it (memory.recall).

The tool writes the frontmatter itself (name, description, metadata origin/version/updated) and requires the
body to have the four sections When to Use / Procedure / Pitfalls / Verification. Text is redacted before it
is written. Caps: body 12,000 characters (an error, so the agent condenses), 40 active learned skills (the
least used are archived). A near-duplicate of any installed or learned skill is refused with the name to update.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_harness.mcp import kb as _kb  # type: ignore
    from agent_harness.redact import redact  # type: ignore
else:
    from . import kb as _kb
    from ..redact import redact

SECTIONS = ("When to Use", "Procedure", "Pitfalls", "Verification")
BODY_MAX = 12000
BODY_WARN = 6000
DESC_MAX = 1024           # agentskills.io: description 1-1024 characters
ACTIVE_MAX = 40
DUP_JACCARD = 0.5         # name + description word overlap with an existing skill
MIN_SCORE = 0.25          # recall: prompt vs name + description + When to Use
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")   # agentskills.io: lowercase, digits, hyphens, <= 64
_FM = re.compile(r"^---\n(.*?)\n---\n?", re.S)
_SECTION = re.compile(r"^##\s+(.+?)\s*$", re.M)
_LOGGY = re.compile(r"(#\d{2,}|\b\d{4}-\d\d-\d\d\b)")


def _front(text: str) -> Dict[str, str]:
    m = _FM.match(text)
    out: Dict[str, str] = {}
    for line in (m.group(1).splitlines() if m else []):
        k, sep, v = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            v = v.strip()
            try:          # written as a JSON string (a valid YAML double-quoted scalar): "Use when: x" stays one value
                out[k.strip()] = json.loads(v) if v.startswith('"') else v
            except ValueError:
                out[k.strip()] = v.strip('"')
        elif sep and line.strip().startswith("version"):
            out["version"] = v.strip().strip('"')
    return out


def _body(text: str) -> str:
    m = _FM.match(text)
    return text[m.end():] if m else text


def _section(body: str, title: str) -> str:
    parts = _SECTION.split(body)
    for i in range(1, len(parts) - 1, 2):
        if parts[i].strip().lower() == title.lower():
            return parts[i + 1].strip()
    return ""


def _words(text: str) -> set:
    return {t for t in _kb.tokens(text) if t not in _kb.STOPWORDS and len(t) > 1}


def _jaccard(a: str, b: str) -> float:
    sa, sb = _words(a), _words(b)
    return len(sa & sb) / float(len(sa | sb)) if sa and sb else 0.0


class Skills:
    def __init__(self, home: Optional[os.PathLike] = None, user_home: Optional[os.PathLike] = None,
                 active_max: int = ACTIVE_MAX):
        self.home = _kb.harness_home(home)
        self.user_home = Path(user_home) if user_home else Path.home()
        self.root = self.user_home / ".agents" / "skills"
        self.learned = self.root / "learned"
        self.archive_dir = self.home / "skills-archive"
        self.usage_file = self.home / "skills-usage.json"
        self.active_max = active_max

    # ------------------------------------------------------------ reading
    def _all(self) -> List[Tuple[str, str, Path]]:
        """(origin, name, SKILL.md) of every skill an agent here can load: installed and learned."""
        out = []
        for origin, d in (("learned", self.learned), ("installed", self.root),
                          ("installed", self.user_home / ".claude" / "skills")):
            if not d.is_dir():
                continue
            for sub in sorted(d.iterdir()):
                f = sub / "SKILL.md"
                if sub.name != "learned" and f.is_file():
                    out.append((origin, sub.name, f))
        seen, uniq = set(), []
        for o, n, f in out:              # ~/.claude/skills links to ~/.agents/skills: one entry per name
            if n not in seen:
                seen.add(n)
                uniq.append((o, n, f))
        return uniq

    def _usage(self) -> dict:
        try:
            return json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _bump(self, name: str) -> None:
        u = self._usage()
        e = u.setdefault(name, {"uses": 0, "last": 0})
        e["uses"] = int(e.get("uses", 0)) + 1
        e["last"] = time.time()
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.usage_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(u, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(self.usage_file))

    @staticmethod
    def _last_used(name: str, u: dict) -> float:
        """The recorded use only. A file's access time cannot tell a native read (Codex) from the harness's own
        reads (recall, dedupe, list read every learned SKILL.md), so it is not used."""
        return float((u.get(name) or {}).get("last", 0))

    # ------------------------------------------------------------ checks
    @staticmethod
    def lint(description: str, body: str) -> Tuple[List[str], List[str]]:
        """(errors, warnings). Errors refuse the write; warnings come back with the result."""
        errors, warnings = [], []
        if not description.strip():
            errors.append("description is empty: one line saying what the skill does and when to use it")
        if len(description) > DESC_MAX:
            errors.append("description is %d characters (max %d)" % (len(description), DESC_MAX))
        if len(body) > BODY_MAX:
            errors.append("body is %d characters (max %d): keep the general procedure, drop the log"
                          % (len(body), BODY_MAX))
        have = {h.strip().lower() for h in _SECTION.findall(body)}
        missing = [s for s in SECTIONS if s.lower() not in have]
        if missing:
            errors.append("missing sections: " + ", ".join("## " + s for s in missing))
        if len(body) > BODY_WARN and not errors:
            warnings.append("long (%d characters): the whole body loads into context when used" % len(body))
        if len(_LOGGY.findall(body)) > 8:
            warnings.append("reads like the log of one incident (many dates or issue numbers): keep what "
                            "generalises")
        return errors, warnings

    def _similar(self, name: str, description: str, skip: str = "") -> Optional[str]:
        for _, n, f in self._all():
            if n == skip:
                continue
            if n == name:
                return n
            try:
                d = _front(f.read_text(encoding="utf-8", errors="replace")).get("description", "")
            except OSError:
                continue
            if _jaccard(name.replace("-", " ") + " " + description, n.replace("-", " ") + " " + d) > DUP_JACCARD:
                return n
        return None

    # ------------------------------------------------------------ writing
    def _render(self, name: str, description: str, body: str, version: int) -> str:
        desc = " ".join(description.split())
        return ("---\nname: %s\ndescription: %s\nmetadata:\n  origin: learned\n  version: \"%d\"\n"
                "  updated: \"%s\"\n---\n\n%s\n" % (name, json.dumps(desc, ensure_ascii=False), version,
                                                     time.strftime("%Y-%m-%d"),
                                                     body.strip()))

    def _write(self, name: str, text: str) -> Path:
        d = self.learned / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "SKILL.md"
        tmp = d / "SKILL.md.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(str(tmp), str(f))
        return f

    @staticmethod
    def _name(name: str) -> str:
        """Every write builds a path from the name: only [a-z0-9-] reaches it (no ../, no absolute path)."""
        name = (name or "").strip()
        if not NAME_RE.match(name) or len(name) > 64:
            raise ValueError("name must be lowercase letters, digits and hyphens (<= 64), e.g. count-sz-fills")
        return name

    def create(self, name: str, description: str, body: str) -> dict:
        name = self._name(name)
        description, body = redact(description or ""), redact(body or "")
        errors, warnings = self.lint(description, body)
        if errors:
            raise ValueError("; ".join(errors))
        other = self._similar(name, description)
        if other:
            raise ValueError("a similar skill exists: %r. Improve it with skill_manage update (view it first)"
                             % other)
        f = self._write(name, self._render(name, description, body, 1))
        archived = self._enforce_cap(keep=name)
        out = {"ok": True, "name": name, "path": str(f)}
        if warnings:
            out["warnings"] = warnings
        if archived:
            out["archived"] = archived
        return out

    def update(self, name: str, description: str = "", body: str = "", old: str = "", new: str = "") -> dict:
        """Replace the description and/or the body, or replace one exact passage (old -> new) in the body."""
        name = self._name(name)
        f = self.learned / name / "SKILL.md"
        if not f.is_file():
            raise ValueError("no learned skill %r (only learned skills can be updated; see skill_manage list)" % name)
        text = f.read_text(encoding="utf-8")
        fm = _front(text)
        cur_body = _body(text).strip()
        if old:
            if cur_body.count(old) != 1:
                raise ValueError("old text must occur exactly once in the body (found %d)" % cur_body.count(old))
            cur_body = redact(cur_body.replace(old, new))   # whole body: a name=value split across old/new
        elif body:
            cur_body = redact(body)
        elif not description:
            raise ValueError("nothing to update: give body, description, or old and new")
        desc = redact(description) if description else fm.get("description", "")
        errors, warnings = self.lint(desc, cur_body)
        if errors:
            raise ValueError("; ".join(errors))
        try:
            version = int(fm.get("version", "1")) + 1
        except ValueError:
            version = 2
        self._write(name, self._render(name, desc, cur_body, version))
        self._bump(name)
        out = {"ok": True, "name": name, "version": version}
        if warnings:
            out["warnings"] = warnings
        return out

    def view(self, name: str) -> str:
        for origin, n, f in self._all():
            if n == name:
                text = f.read_text(encoding="utf-8", errors="replace")
                if origin == "learned":
                    self._bump(name)
                return text
        raise ValueError("no skill %r (skill_manage list shows them)" % name)

    def list(self) -> List[dict]:
        u = self._usage()
        out = []
        for origin, n, f in self._all():
            try:
                d = _front(f.read_text(encoding="utf-8", errors="replace")).get("description", "")
            except OSError:
                d = ""
            item = {"name": n, "origin": origin, "description": d[:200]}
            if origin == "learned":
                item["uses"] = int((u.get(n) or {}).get("uses", 0))
            out.append(item)
        return out

    def archive(self, name: str, reason: str = "asked") -> dict:
        name = self._name(name)
        src = self.learned / name
        if not (src / "SKILL.md").is_file():
            raise ValueError("no learned skill %r" % name)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dst = self.archive_dir / ("%s-%s" % (name, time.strftime("%Y%m%dT%H%M%S")))
        n = 1
        while dst.exists():
            n += 1
            dst = self.archive_dir / ("%s-%s-%d" % (name, time.strftime("%Y%m%dT%H%M%S"), n))
        shutil.move(str(src), str(dst))
        (dst / "ARCHIVED").write_text("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), reason), encoding="utf-8")
        return {"ok": True, "archived": name, "to": str(dst)}

    def _enforce_cap(self, keep: str) -> List[str]:
        learned = [(n, f) for o, n, f in self._all() if o == "learned" and n != keep]
        over = len(learned) + 1 - self.active_max
        if over <= 0:
            return []
        u = self._usage()
        learned.sort(key=lambda x: (int((u.get(x[0]) or {}).get("uses", 0)), self._last_used(x[0], u), x[0]))
        out = []
        for n, _ in learned[:over]:
            self.archive(n, reason="cap")
            out.append(n)
        return out

    # ------------------------------------------------------------ recall (Claude Code's per-prompt hook)
    def scored(self, prompt: str, min_score: float = MIN_SCORE) -> List[Tuple[float, str, str]]:
        """(score, name, description) of learned skills relevant to the prompt, best first."""
        qt = _kb.query_terms(prompt or "")
        if not qt:
            return []
        docs = []
        for origin, n, f in self._all():
            if origin != "learned":
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            d = _front(text).get("description", "")
            docs.append((n, d, _kb.tokens(n.replace("-", " ") + " " + d + " " + _section(_body(text), "When to Use"))))
        if not docs:
            return []
        df: Dict[str, int] = {}
        for _, _, toks in docs:
            for w in set(toks):
                df[w] = df.get(w, 0) + 1
        n_docs, avgdl = len(docs), sum(len(t) for _, _, t in docs) / float(len(docs))
        out = [(_kb.relevance(qt, toks, n_docs, avgdl, df, doc_coverage=True), n, d) for n, d, toks in docs]
        return sorted((x for x in out if x[0] >= min_score), key=lambda x: -x[0])


def render(action: str, result) -> str:
    if action == "view":
        return result
    if action == "list":
        if not result:
            return "no skills"
        return "\n".join("%s (%s%s): %s" % (r["name"], r["origin"], ", %d uses" % r["uses"] if "uses" in r else "",
                                            r["description"]) for r in result)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
