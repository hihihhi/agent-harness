"""harness improve: the harness getting better from its own history, only where an eval shows it did.

    harness improve --propose [--json]           candidates seen at least twice -> proposals
    harness improve --apply ID --eval FILE       apply one, only if FILE shows a gain under the keep rule
    harness improve --trend HISTORY.jsonl        refuse a release that scores below the one before it

Why every change waits on an eval: skills agents wrote for themselves scored 8-11.5 points BELOW no skills at
all (SkillsBench), while curated ones gained 16.6; a lesson from a false signal made Reflexion worse. So a
candidate (agent_harness.capture) becomes a proposal only once it has recurred, and a proposal becomes a lesson
only when a paired with/without eval on the same tasks shows more passes at no more than +15% tokens: the repo's
keep rule (accuracy no worse, a measurable gain, token overhead within +15%).

The eval file names the proposal it measured: {"proposal": ID, "base": {"pass", "n", "tokens"}, "cand": {...}},
tokens on both sides (a missing count is not "no overhead").
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import List, Tuple

from .capture import STORE

MIN_COUNT = 2
TOKEN_OVERHEAD = 0.15


def _candidates(hh: Path) -> List[dict]:
    p = hh / STORE
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def propose(hh: Path) -> List[dict]:
    d = hh / "proposals"
    open_ = []
    for c in _candidates(hh):
        if int(c.get("count", 1)) < MIN_COUNT or not c.get("id"):
            continue
        f = d / ("%s.json" % c["id"])
        if f.is_file():
            prop = json.loads(f.read_text(encoding="utf-8"))
        else:
            prop = {"id": c["id"], "kind": "lesson", "source": c.get("source"), "summary": c.get("summary"),
                    "evidence": c.get("evidence"), "count": c.get("count"), "status": "proposed",
                    "proposed": time.strftime("%Y-%m-%dT%H:%M:%S")}
            d.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(prop, indent=1) + "\n", encoding="utf-8")
        if prop.get("status") == "proposed":
            open_.append(prop)
    return open_


def keep_rule(result: dict) -> Tuple[bool, str]:
    try:
        b, c = result["base"], result["cand"]
        bp, bn, cp, cn = int(b["pass"]), int(b["n"]), int(c["pass"]), int(c["n"])
        bt, ct = float(b["tokens"]), float(c["tokens"])
    except (KeyError, TypeError, ValueError) as e:
        return False, "the eval file is not {base: {pass, n, tokens}, cand: {...}} (missing %s)" % e
    if not (0 <= bp <= bn and 0 <= cp <= cn):
        return False, "an impossible score (%d/%d, %d/%d)" % (bp, bn, cp, cn)
    if not (math.isfinite(bt) and math.isfinite(ct)) or bt <= 0 or ct <= 0:
        return False, "token counts must be measured on both sides (%s vs %s)" % (bt, ct)
    if bn != cn or bn <= 0:
        return False, "base and candidate ran a different number of tasks (%d vs %d); not a paired eval" % (bn, cn)
    if cp <= bp:
        return False, "no measurable gain: %d/%d with it vs %d/%d without" % (cp, cn, bp, bn)
    if ct > bt * (1 + TOKEN_OVERHEAD):
        return False, "token overhead %+.0f%% is over the +%d%% the keep rule allows" % (
            (ct / bt - 1) * 100, TOKEN_OVERHEAD * 100)
    return True, "%d/%d with it vs %d/%d without, tokens %+.0f%%" % (cp, cn, bp, bn, (ct / bt - 1) * 100)


def apply(hh: Path, pid: str, eval_path: str = None) -> Tuple[int, str]:
    f = hh / "proposals" / ("%s.json" % pid)
    if not f.is_file():
        return 2, "improve: no proposal %s" % pid
    if not eval_path:
        return 1, "improve: %s not applied: no eval. Run the paired eval, then --eval FILE" % pid
    try:
        result = json.loads(Path(eval_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return 2, "improve: cannot read the eval (%s)" % e
    prop = json.loads(f.read_text(encoding="utf-8"))
    if prop.get("status") != "proposed":
        return 1, "improve: %s is already %s" % (pid, prop.get("status"))
    if not isinstance(result, dict) or result.get("proposal") != pid:
        return 1, "improve: that eval measured %r, not %s" % (
            result.get("proposal") if isinstance(result, dict) else None, pid)
    ok, why = keep_rule(result)
    if not ok:
        return 1, "improve: %s not applied: %s" % (pid, why)
    from .mcp.memory import Memory
    Memory(home=hh).lesson_add(mistake=str(prop.get("summary") or ""), fix=str(prop.get("evidence") or ""),
                               trigger="learned from %s (seen %s times); eval: %s" % (
                                   prop.get("source"), prop.get("count"), why))
    prop.update(status="applied", applied=time.strftime("%Y-%m-%dT%H:%M:%S"), eval=why)
    f.write_text(json.dumps(prop, indent=1) + "\n", encoding="utf-8")
    return 0, "improve: %s applied as a lesson (%s)" % (pid, why)


RUNS_PER_ALLOWED_MISS = 54   # a release may score n // 54 below the BEST accepted release: a single run (n=18) is
#                              strict, three runs of 18 allow one. The owner, 2026-10-07, after one run of noise held
#                              a measured gain back: "it is ok to iterate not to give up updating". The reference is
#                              always the best accepted release, so misses cannot compound. docs/design-decisions.md


def _cost(r) -> float:
    try:
        t = float(r.get("tokens") or 0)
    except (TypeError, ValueError):
        return float("inf")
    return t if t > 0 and math.isfinite(t) else float("inf")


def _judge(cur: dict, earlier: list, first_ok: bool = True) -> Tuple[bool, str]:
    """cur against the best of `earlier` (same question count, all accepted): the best pass rate, and among
    equals the cheapest. No earlier release on this eval: cur is its baseline when `first_ok`."""
    n = float(cur["n"])
    if not earlier:
        if first_ok:
            return True, "%s %s/%s, the first on this eval (baseline)" % (cur.get("version"), cur["pass"], cur["n"])
        return False, ("%s ran %s questions, which no accepted release ran: not comparable. If the eval changed on "
                       "purpose, accept it as a new baseline with --new-baseline" % (cur.get("version"), cur["n"]))
    top = max(int(r["pass"]) for r in earlier)
    best = min((r for r in earlier if int(r["pass"]) == top), key=_cost)
    b_rate, c_rate = top / n, int(cur["pass"]) / n
    bt, ct = _cost(best), _cost(cur)
    tok_ok = bt != float("inf") and ct != float("inf") and ct <= bt * (1 + TOKEN_OVERHEAD)
    better = c_rate > b_rate and tok_ok
    same = c_rate <= b_rate and b_rate - c_rate <= (int(n) // RUNS_PER_ALLOWED_MISS) / n + 1e-12 and tok_ok
    ok = better or same
    return ok, "best accepted %s %s/%s -> %s %s/%s %s" % (best.get("version"), best["pass"], best["n"],
                                                          cur.get("version"), cur["pass"], cur["n"],
                                                          "ok" if ok else "REGRESSION")


def trend(history: str, release: str = None, new_baseline: bool = False) -> Tuple[int, str]:
    """`release` (default: this harness's own version) against the BEST earlier release, per tool, on the same
    number of questions: more than one question fewer (the measured single-run noise), or more than +15% tokens
    for passes within that one question, is a regression. Refused as well: no history, a release
    recorded twice for a tool (it would hide the first result), a tool the previous release measured and this
    one did not, and a row with no tasks."""
    rows = []
    try:
        for line in Path(history).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, ValueError) as e:
        return 2, "trend: cannot read %s (%s)" % (history, e)
    if not rows:
        return 1, "trend: no eval history; a release with no measured result is not shown to be better"
    seen, versions = {}, []
    for r in rows:
        try:
            if int(r["n"]) <= 0 or not 0 <= int(r["pass"]) <= int(r["n"]):
                return 1, "trend: a row with no tasks or an impossible score: %r" % r
        except (KeyError, TypeError, ValueError):
            return 1, "trend: a row without pass/n: %r" % r
        k = (r.get("version"), r.get("tool"))
        if k in seen:
            return 1, "trend: %s is recorded twice for %s; one result per release and tool" % k
        seen[k] = r
        if r.get("version") not in versions:
            versions.append(r.get("version"))
    if not release:                              # the release being shipped: this version, not the file's last row
        from . import __version__
        release = __version__
    if release not in versions:
        return 1, "trend: no rows for release %s" % release
    i = versions.index(release)
    if i == 0:
        return 0, "trend: %s is the first recorded release (baseline)" % release
    prev_v = versions[i - 1]
    tools = sorted({t for v, t in seen if v == prev_v}, key=str)
    bad, lines = [], []
    for tool in tools:
        cur = seen.get((release, tool))
        if cur is None:
            bad.append(tool)
            lines.append("%s: measured in %s but not in %s" % (tool, prev_v, release))
            continue
        # The reference is built only from releases that were themselves accepted, in order, on the same
        # number of questions: a refused release (3.4x the tokens) must never become the next one's yardstick,
        # and a new, larger eval starts a new baseline instead of blocking every later release.
        accepted = []
        for v in versions[:i]:
            row = seen.get((v, tool))
            if row is not None and _judge(row, [r for r in accepted if int(r["n"]) == int(row["n"])],
                                          first_ok=bool(row.get("new_baseline")) or not accepted)[0]:
                accepted.append(row)
        # A new question count is a new baseline only when someone says so: silently, shrinking the eval would
        # be a way to escape every comparison; refused forever, a deliberate new eval could never ship.
        ok, line = _judge(cur, [r for r in accepted if int(r["n"]) == int(cur["n"])],
                          first_ok=new_baseline or bool(cur.get("new_baseline")) or not accepted)
        lines.append("%s: %s" % (tool, line))
        if not ok:
            bad.append(tool)
    return (1 if bad else 0), "trend:\n  " + "\n  ".join(lines)
