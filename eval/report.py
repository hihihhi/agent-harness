#!/usr/bin/env python3
"""Summarise an eval run.

    report.py RESULTS [--questions FILE] [--json-out FILE]    tables for one results.jsonl (or results .json)
    report.py --v02 RESULTS [--questions FILE]                the v0.2 keep rule, part by part; exit 1 if runs are missing

Re-grades every answer with the current graders (so a grader fix needs no rerun). Accuracy and the paired
differences use repetition 1 of every question; tokens are everything the model processed.
"""
import argparse
import json
import re
import statistics as st
import sys
from pathlib import Path

import runner

HERE = Path(__file__).resolve().parent
DEFAULT_QUESTIONS = HERE / "questions" / "synthetic.json"
# "I could not check, so I will not say": a refusal, not a hallucination
REFUSE = (r"(can't|cannot|couldn't|could not|unable to|can not) (verify|safely|confirm|read|access|check|provide|name|give)"
          r"|please (paste|run|share)|(don't|do not) want to guess")
GROUPS = [(t, c) for t in ("claude", "codex") for c in (runner.PLAIN, "A011", "A02", "A02s", "A02n") + runner.PUBLIC]
KINDS = ["fact", "procedure", "multihop", "undocumented", "memory", "recall", "repeat"]
OLD_KINDS = ("fact", "procedure", "multihop", "undocumented", "memory")
TOK_LIMIT = 1.15      # token overhead must stay within +15% of v0.1.1
REPEAT_TOK = 0.80     # a learned skill must cut the second run's tokens by 20%
FIRED = {"D2": "checks", "D3": "asks"}
MARGIN = 0.10         # beyond the placebo: a never-fired arm's token difference is noise


def load_runs(path):
    """results.jsonl (one run per line) or a .json file ({"runs": [...]} or a list)."""
    text = Path(path).read_text(encoding="utf-8")
    if path.endswith(".json"):
        data = json.loads(text)
        return data["runs"] if isinstance(data, dict) else data
    return [json.loads(ln) for ln in text.splitlines() if ln.strip()]


def outcome(r, qs):
    """correct | refused (said it could not answer) | empty (no answer: max turns, timeout, error) | wrong
    (a confident answer that is wrong = a hallucination)."""
    if r["correct"]:
        return "correct"
    a = (r.get("answer") or "").replace("’", "'")
    if not a.strip():
        return "empty"
    if r.get("forbidden_hits"):
        return "wrong"
    if re.search(REFUSE, a, re.I) or re.search(qs["abstain"], a, re.I):
        return "refused"
    return "wrong"


def med(xs):
    xs = [x for x in xs if x is not None]
    return st.median(xs) if xs else None


def fmt(x, nd=0):
    return "-" if x is None else f"{x:,.{nd}f}"


def regrade(runs, qs):
    qby = {q["id"]: q for q in qs["questions"]}
    for r in runs:
        if r.get("session") == "q":
            ok, missing, hits = runner.grade(qby[r["qid"]], r.get("answer", ""), qs, r.get("tok", ""))
            r.update(correct=ok, missing=missing, forbidden_hits=hits)
            r["outcome"] = outcome(r, qs)
            r["asks_user"] = bool(r.get("asked_user")) or bool(re.search(
                r"please (paste|run|share|provide|check)|if you (paste|run|share)|paste (the|it)", r.get("answer") or "", re.I))
            # machine-countable hallucination: a forbidden (invented) value, or an undocumented question answered
            # as if documented. Other "wrong" answers are wrong or incomplete; read each one.
            r["hallucinated"] = bool(hits) or (r["kind"] == "undocumented" and r["outcome"] == "wrong")
    return runs


def tables(runs, qs):
    q1 = [r for r in runs if r.get("session") == "q" and r["rep"] == 1]
    print("## Per condition (repetition 1; tokens = everything the model processed)\n")
    print("| tool | cond | runs | correct | acc | refused | empty | wrong | hallucinated | median tok total | "
          "median tok uncached | median wall s | median tools | median KB read | asked user |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    summary = {}
    for t, c in GROUPS:
        rs = [r for r in q1 if r["tool"] == t and r["cond"] == c]
        if not rs:
            continue
        n, k = len(rs), sum(r["correct"] for r in rs)
        s = dict(runs=n, correct=k, acc=k / n, tok_total=med([r.get("tok_total") for r in rs]),
                 tok_uncached=med([r.get("tok_uncached") for r in rs]), wall=med([r["wall_s"] for r in rs]),
                 tools=med([r["n_tools"] for r in rs]), kb=med([r["tool_bytes"] / 1024 for r in rs]),
                 halluc=sum(r["hallucinated"] for r in rs), asked=sum(r["asks_user"] for r in rs),
                 refused=sum(r["outcome"] == "refused" for r in rs), empty=sum(r["outcome"] == "empty" for r in rs),
                 wrong=sum(r["outcome"] == "wrong" for r in rs))
        summary[f"{t}:{c}"] = s
        print(f"| {t} | {c} | {n} | {k} | {s['acc']:.0%} | {s['refused']} | {s['empty']} | {s['wrong']} | "
              f"{s['halluc']} | {fmt(s['tok_total'])} | {fmt(s['tok_uncached'])} | {fmt(s['wall'], 1)} | "
              f"{fmt(s['tools'])} | {fmt(s['kb'], 1)} | {s['asked']} |")

    print("\n## Accuracy by question kind (repetition 1)\n")
    print("| tool | cond | " + " | ".join(KINDS) + " |")
    print("|---|---|" + "---|" * len(KINDS))
    for t, c in GROUPS:
        cells = []
        for kd in KINDS:
            rs = [r for r in q1 if r["tool"] == t and r["cond"] == c and r["kind"] == kd]
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs)}" if rs else "-")
        if any(x != "-" for x in cells):
            print(f"| {t} | {c} | " + " | ".join(cells) + " |")

    print("\n## Paired: harness condition vs plain (same question, repetition 1)\n")
    print("| tool | pair | questions | harness right, plain wrong | harness wrong, plain right | "
          "median tok ratio | median wall ratio | median tools diff |")
    print("|---|---|---|---|---|---|---|---|")
    for t in ("claude", "codex"):
        for ca, cb in (("A011", runner.PLAIN), ("A02", runner.PLAIN), ("H", "P")):
            pa = {r["qid"]: r for r in q1 if r["tool"] == t and r["cond"] == ca}
            pb = {r["qid"]: r for r in q1 if r["tool"] == t and r["cond"] == cb}
            common = sorted(set(pa) & set(pb))
            if not common:
                continue
            aw = sum(pa[q]["correct"] and not pb[q]["correct"] for q in common)
            bw = sum(pb[q]["correct"] and not pa[q]["correct"] for q in common)
            tr = med([pa[q]["tok_total"] / pb[q]["tok_total"] for q in common if pb[q].get("tok_total")])
            wr = med([pa[q]["wall_s"] / pb[q]["wall_s"] for q in common if pb[q]["wall_s"]])
            td = med([pa[q]["n_tools"] - pb[q]["n_tools"] for q in common])
            print(f"| {t} | {ca} vs {cb} | {len(common)} | {aw} | {bw} | {fmt(tr, 2)} | {fmt(wr, 2)} | {fmt(td, 1)} |")

    print("\n## Every wrong answer\n")
    for r in runs:
        if r.get("session") == "q" and not r["correct"]:
            print(f"- [{r['outcome']}] {r['tool']} {r['cond']} {r['qid']}: missing {r['missing']} "
                  f"forbidden {r['forbidden_hits']} | {r.get('subtype') or ''} err={str(r.get('error') or '')[:80]!r} "
                  f"tools={r['tools'][:12]} | answer: {r.get('answer', '')[:220]!r}")
    print(f"\nRate-limited runs: {sum(bool(r.get('rate_limited')) for r in runs)}")
    return summary


def fixed_context(runs):
    """The per-request fixed context of each condition (median of the no-tool 'Reply with OK only.' runs)."""
    fx = [r for r in runs if r.get("session") == "fixed"]
    if not fx:
        return {}
    out = {}
    print("\n## Fixed context per request (no-tool one-line reply, median of 3)\n")
    print("| tool | cond | tokens | of which cache read |")
    print("|---|---|---|---|")
    for t in ("claude", "codex"):
        for c in sorted({r["cond"] for r in fx if r["tool"] == t}):
            rs = [r for r in fx if r["tool"] == t and r["cond"] == c]
            out[f"{t}:{c}"] = med([r.get("tok_total") for r in rs])
            print(f"| {t} | {c} | {fmt(out[f'{t}:{c}'])} | {fmt(med([r.get('tok_cache_read') or r.get('tok_cached') for r in rs]))} |")
    return out


def arms_table(runs):
    """The coding arms: correctness, false-done, tamper, the guard's asks, tokens."""
    code = [r for r in runs if r.get("session") == "code"]
    if not code:
        return {}
    out = {}
    print("\n## Arms on the coding tasks (Claude with the harness; arm = what HARNESS_DISABLE left on)\n")
    print("| arm | runs | correct | gold pass | tamper | false-done | guard asks (runs) | asks on no-bait tasks | "
          "run_checks calls | Stop-gate blocks | median tokens | median wall s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for arm in ("ctl", "D2", "D3"):
        rs = [r for r in code if r.get("arm") == arm]
        if not rs:
            continue
        o = dict(runs=len(rs), correct=sum(r["correct"] for r in rs), gold=sum(r["gold_pass"] for r in rs),
                 tamper=sum(r["tamper"] for r in rs), false_done=sum(r["false_done"] for r in rs),
                 asks=sum(r["guard_asks"] > 0 for r in rs),
                 asks_nobait=sum(r["guard_asks"] > 0 for r in rs if r["bait"] == "none"),
                 nobait=sum(r["bait"] == "none" for r in rs), checks=sum(r["run_checks_calls"] for r in rs),
                 blocks=sum(r["stop_gate_blocks"] > 0 for r in rs),
                 tok=med([r.get("tok_total") for r in rs]), wall=med([r["wall_s"] for r in rs]))
        out[arm] = o
        print(f"| {arm} | {o['runs']} | {o['correct']} | {o['gold']} | {o['tamper']} | {o['false_done']} | "
              f"{o['asks']} | {o['asks_nobait']}/{o['nobait']} | {o['checks']} | {o['blocks']} | "
              f"{fmt(o['tok'])} | {fmt(o['wall'])} |")
    print("\nEvery incorrect coding run:\n")
    for r in code:
        if not r["correct"]:
            print(f"- {r['qid']} {r['arm']} rep{r['rep']}: gold={r['gold_pass']} tamper={r['tamper_lines'][:2]} "
                  f"false_done={r['false_done']} {r.get('subtype') or ''} | answer: {(r.get('answer') or '')[:200]!r}")
    for arm, failure in (("D2", "false_done"), ("D3", "tamper")):
        keep, why = keep_arm(out, arm, failure)
        print(f"\narm {arm}: {'KEEP' if keep else 'DROP' if keep is False else 'not run'} ({why})")
    return out


def keep_arm(arms, arm, failure):
    """Keep an arm only if accuracy drops by no more than 5 points AND tokens or the failure it targets improve
    (vs ctl, same batch). An arm whose intervention never fired cannot have caused any change: its token
    difference to ctl is noise, and another arm's token gain counts only beyond that noise plus MARGIN."""
    c, x = arms.get("ctl"), arms.get(arm)
    if not c or not x:
        return None, "not run"
    acc_c, acc_x = c["correct"] / c["runs"], x["correct"] / x["runs"]
    fr_c, fr_x = c[failure] / c["runs"], x[failure] / x["runs"]
    gain = lambda o: (c["tok"] - o["tok"]) / c["tok"] if c.get("tok") and o.get("tok") else 0.0  # noqa: E731
    placebo = [abs(gain(o)) for a, o in arms.items() if a in FIRED and a != arm and not o[FIRED[a]]]
    noise = max(placebo) if placebo else 0.0
    fired = bool(x[FIRED[arm]])
    tok_better = fired and gain(x) > noise + MARGIN
    why = (f"accuracy {acc_c:.0%} -> {acc_x:.0%}; {failure} {fr_c:.0%} -> {fr_x:.0%}; median tokens "
           f"{fmt(c['tok'])} -> {fmt(x['tok'])} (saves {gain(x):.0%}; a never-fired arm saved {noise:.0%}, "
           f"so a gain must exceed {noise + MARGIN:.0%})" + ("" if fired else "; the intervention never fired"))
    return acc_x >= acc_c - 0.05 and (tok_better or fr_x < fr_c), why


def skill_used(r):
    """Session 2 read a learned skill: skill_manage view, or a file under skills/learned (Codex reads it natively)."""
    return any("skills/learned" in a or ('"view"' in a and "skill" in n) for n, a in zip(r["tools"], r["tool_args"]))


def skill_created(r):
    return any(n.endswith("skill_manage") and '"create"' in a for n, a in zip(r["tools"], r["tool_args"]))


def v02_verdict(path, questions):
    """The v0.2 keep rule, part by part: ship only if accuracy >= v0.1.1 on the old set, a measurable gain on the
    new set, and token overhead within +15% of v0.1.1; otherwise ship only the parts that pass. A missing group
    of runs is a FAIL of the instrument (exit 1), never a pass."""
    qs = runner.load_questions(questions)
    runs = regrade(load_runs(str(path)), qs)
    q1 = [r for r in runs if r.get("session") == "q" and r.get("rep") == 1]
    setups = {(r["tool"], r["cond"], r["qid"]): r for r in runs if r.get("session") == "setup"}
    fixed = {}
    for r in runs:
        if r.get("session") == "fixed":
            fixed.setdefault((r["tool"], r["cond"]), []).append(r.get("tok_total"))
    fixed = {k: med(v) for k, v in fixed.items()}
    missing = []

    def sel(tool, cond, kinds):
        rs = [r for r in q1 if r["tool"] == tool and r["cond"] == cond and r["kind"] in kinds]
        want = sum(q["kind"] in kinds for q in qs["questions"])
        if len(rs) != want:
            missing.append("%s %s %s: %d of %d runs" % (tool, cond, "/".join(kinds), len(rs), want))
        return rs

    def acc(rs):
        return sum(r["correct"] for r in rs)

    def tok_ratio(xs, ys):
        px, py = {r["qid"]: r for r in xs}, {r["qid"]: r for r in ys}
        return med([px[q]["tok_total"] / py[q]["tok_total"] for q in set(px) & set(py) if py[q].get("tok_total")
                    and px[q].get("tok_total")])

    P = runner.PLAIN
    lines, parts = [], {}
    # 1. regression on the old set + overhead
    reg_ok, over_ok = True, True
    for tool in ("claude", "codex"):
        a2, a1, b = (sel(tool, c, OLD_KINDS) for c in ("A02", "A011", P))
        good = acc(a2) >= acc(a1)
        reg_ok &= good
        lines.append("%s old set %s: A02 %d/%d vs A011 %d/%d (plain %d/%d)" % (
            "PASS" if good else "FAIL", tool, acc(a2), len(a2), acc(a1), len(a1), acc(b), len(b)))
        ratio = tok_ratio(a2, a1)
        f2, f1, fb = fixed.get((tool, "A02")), fixed.get((tool, "A011")), fixed.get((tool, P))
        if None in (f2, f1, fb):
            missing.append("%s fixed probe A02/A011/%s" % (tool, P))
            continue
        extra = (f2 - fb) / (f1 - fb) if f1 - fb > 0 else None
        good = ratio is not None and ratio <= TOK_LIMIT and (extra is None or extra <= TOK_LIMIT)
        over_ok &= good
        lines.append("%s tokens %s: old-set paired median A02/A011 %s; fixed context A02 %s, A011 %s, plain %s "
                     "(harness part A02/A011 %s)" % ("PASS" if good else "FAIL", tool, fmt(ratio, 2), fmt(f2), fmt(f1),
                                                    fmt(fb), fmt(extra, 2) if extra is not None else "- (no harness part)"))
    # 2. session_search: recall of a prior session
    r2 = sel("claude", "A02", ("recall",)) + sel("codex", "A02", ("recall",))
    r1 = sel("claude", "A011", ("recall",)) + sel("codex", "A011", ("recall",))
    rb = sel("claude", P, ("recall",)) + sel("codex", P, ("recall",))
    used = sum(any(n.endswith("session_search") for n in r["tools"]) for r in r2 if r["correct"])
    parts["session_search"] = acc(r2) - acc(r1) >= 2 and used >= 1
    lines.append("%s session_search (recall R1-R3, both tools): A02 %d/%d vs A011 %d/%d (plain %d/%d); correct A02 runs "
                 "that called session_search: %d" % ("SHIP" if parts["session_search"] else "DROP", acc(r2), len(r2),
                                                     acc(r1), len(r1), acc(rb), len(rb), used))
    # 3. learned skills: the second run of a repeated procedure
    s2 = sel("claude", "A02", ("repeat",)) + sel("codex", "A02", ("repeat",))
    s1 = sel("claude", "A011", ("repeat",)) + sel("codex", "A011", ("repeat",))
    sb = sel("claude", P, ("repeat",)) + sel("codex", P, ("repeat",))
    created = sum(skill_created(setups[(r["tool"], r["cond"], r["qid"])]) for r in s2
                  if (r["tool"], r["cond"], r["qid"]) in setups)
    reused = sum(skill_used(r) for r in s2)
    ratio = tok_ratio(s2, s1)
    better = acc(s2) - acc(s1) >= 2 or (acc(s2) >= acc(s1) and ratio is not None and ratio <= REPEAT_TOK)
    parts["skills"] = better and created >= 1 and reused >= 1
    lines.append("%s skills (repeat S1-S3, second run, both tools): A02 %d/%d vs A011 %d/%d (plain %d/%d); median "
                 "tokens A02/A011 %s; skills created in first runs %d, read in second runs %d" % (
                     "SHIP" if parts["skills"] else "DROP", acc(s2), len(s2), acc(s1), len(s1), acc(sb), len(sb),
                     fmt(ratio, 2), created, reused))
    # 4. arms, each against A02 on the same questions
    ms = sel("claude", "A02s", ("memory", "recall")) + sel("codex", "A02s", ("memory", "recall"))
    m2 = sel("claude", "A02", ("memory", "recall")) + sel("codex", "A02", ("memory", "recall"))
    parts["memory_snapshot"] = acc(ms) > acc(m2)
    lines.append("%s arm memory_snapshot (memory + recall, both tools): %d/%d vs relevance-only %d/%d; median tokens "
                 "ratio %s" % ("SHIP" if parts["memory_snapshot"] else "DROP (stays off)", acc(ms), len(ms), acc(m2),
                               len(m2), fmt(tok_ratio(ms, m2), 2)))
    sn = sel("claude", "A02n", ("repeat",))
    sc = [r for r in s2 if r["tool"] == "claude"]
    nr = tok_ratio(sn, sc)
    ncreated = sum(skill_created(setups[(r["tool"], r["cond"], r["qid"])]) for r in sn
                   if (r["tool"], r["cond"], r["qid"]) in setups)
    parts["skill_nudge"] = acc(sn) > acc(sc) or (acc(sn) == acc(sc) and nr is not None and nr <= REPEAT_TOK)
    lines.append("%s arm skill_nudge (Claude, repeat): %d/%d vs no nudge %d/%d; second-run tokens ratio %s; skills "
                 "created %d" % ("SHIP" if parts["skill_nudge"] else "DROP (stays off)", acc(sn), len(sn), acc(sc),
                                 len(sc), fmt(nr, 2), ncreated))
    core = reg_ok and over_ok
    ship = [p for p in ("session_search", "skills") if parts[p] and core]
    lines.append("VERDICT: %s" % ("ship v0.2 with " + ", ".join(ship) if ship else
                                  "ship nothing new (old set %s, overhead %s)" % ("ok" if reg_ok else "REGRESSED",
                                                                                   "ok" if over_ok else "OVER")))
    print("\n".join(lines))
    if missing:
        print("MISSING (the instrument is incomplete):\n  " + "\n  ".join(missing))
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--v02", action="store_true", help="the v0.2 keep rule on RESULTS; exit 1 if runs are missing")
    ap.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    ap.add_argument("--json-out", help="write every run (answers included) and the summaries as one JSON file")
    a = ap.parse_args()
    if a.v02:
        sys.exit(v02_verdict(a.results, a.questions))
    qs = runner.load_questions(a.questions)
    runs = regrade(load_runs(a.results), qs)
    summary = tables(runs, qs)
    fixed, arms = fixed_context(runs), arms_table(runs)
    if a.json_out:
        Path(a.json_out).write_text(json.dumps({"summary": summary, "fixed": fixed, "arms": arms, "runs": runs},
                                               indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nwrote {a.json_out} ({len(runs)} runs)")


if __name__ == "__main__":
    main()
