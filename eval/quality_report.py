"""Report and gain rule for the coding-quality eval (plan harness-coding).

    quality_report.py --baseline RESULTS.jsonl --cond A042      per-dimension headroom; exit 1 if a run is missing
    quality_report.py --gain RESULTS.jsonl --control A042 --cand A043
                                                               exit 0 only if the candidate gains (rule below)

Rows: {"tool", "cond", "task", "rep", "dims": {dim: bool}, "tok_total"}, one per run (the server runner's
`quality` arm). The gain rule, per tool: the WORST candidate run-total (dimension passes summed over every task
in one repetition) must exceed the BEST control run-total -- separated ranges, no statistics to misuse; no
dimension's total over all runs may fall; median tokens <= 1.15x control. Every task x repetition the control
has must be there for the candidate, and the reverse: a missing run FAILS, it never shrinks the comparison.
"""
import argparse
import json
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality_tasks import DIMENSIONS, TASKS  # noqa: E402

TOKEN_CEILING = 1.15
TASK_IDS = [t["id"] for t in TASKS]


def _by(rows, cond, tool):
    return {(r["task"], r["rep"]): r for r in rows if r["cond"] == cond and r["tool"] == tool}


def _missing(runs, reps):
    return [(t, rep) for t in TASK_IDS for rep in reps if (t, rep) not in runs]


def _totals(runs, reps):
    return [sum(sum(bool(v) for v in runs[(t, rep)]["dims"].values()) for t in TASK_IDS) for rep in reps]


def _dim_totals(runs):
    out = {d: 0 for d in DIMENSIONS}
    for r in runs.values():
        for d, v in r["dims"].items():
            out[d] += bool(v)
    return out


def gain(rows, control, cand):
    tools = sorted({r["tool"] for r in rows if r["cond"] in (control, cand)})
    if not tools:
        return False, "no runs for %s or %s" % (control, cand)
    bad, lines = [], []
    for tool in tools:
        c, x = _by(rows, control, tool), _by(rows, cand, tool)
        reps = sorted({rep for _, rep in c} | {rep for _, rep in x})
        miss = ["%s %s" % (cond, m) for cond, runs in ((control, c), (cand, x)) for m in _missing(runs, reps)]
        if miss:
            bad.append(tool)
            lines.append("%s: %d run(s) missing: %s" % (tool, len(miss), miss[:5]))
            continue
        ct, xt = _totals(c, reps), _totals(x, reps)
        why = []
        if min(xt) <= max(ct):
            why.append("ranges overlap (control %s, candidate %s)" % (ct, xt))
        cd, xd = _dim_totals(c), _dim_totals(x)
        fell = [d for d in DIMENSIONS if xd[d] < cd[d]]
        if fell:
            why.append("dimension(s) fell: %s" % ", ".join("%s %d->%d" % (d, cd[d], xd[d]) for d in fell))
        ctok = st.median([r.get("tok_total") or 0 for r in c.values()])
        xtok = st.median([r.get("tok_total") or 0 for r in x.values()])
        if not ctok or not xtok:
            why.append("token counts missing")
        elif xtok > TOKEN_CEILING * ctok:
            why.append("token median %.0f > %.2fx control %.0f" % (xtok, TOKEN_CEILING, ctok))
        if why:
            bad.append(tool)
        lines.append("%s: control %s, candidate %s; %s" % (tool, ct, xt, "; ".join(why) or "GAIN"))
    return not bad, "\n".join(lines)


def baseline(rows, cond):
    tools = sorted({r["tool"] for r in rows if r["cond"] == cond})
    if not tools:
        return 1, "no runs for %s" % cond
    rc, lines = 0, []
    for tool in tools:
        runs = _by(rows, cond, tool)
        reps = sorted({rep for _, rep in runs})
        miss = _missing(runs, reps)
        if miss:
            rc = 1
            lines.append("%s: %d run(s) missing: %s" % (tool, len(miss), miss[:5]))
            continue
        graded = {d: sum(d in r["dims"] for r in runs.values()) for d in DIMENSIONS}
        passed = _dim_totals(runs)
        lines.append("%s: run totals %s; per dimension (passed/graded): %s" % (
            tool, _totals(runs, reps), ", ".join("%s %d/%d" % (d, passed[d], graded[d]) for d in DIMENSIONS)))
    return rc, "\n".join(lines)


def load(path):
    return [json.loads(ln) for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline")
    ap.add_argument("--cond")
    ap.add_argument("--gain")
    ap.add_argument("--control")
    ap.add_argument("--cand")
    a = ap.parse_args(argv)
    path = a.baseline or a.gain
    if path and not Path(path).is_file():
        print("no results file at %s: nothing was measured" % path)
        return 1
    if a.baseline and a.cond:
        rc, text = baseline(load(a.baseline), a.cond)
    elif a.gain and a.control and a.cand:
        ok, text = gain(load(a.gain), a.control, a.cand)
        rc = 0 if ok else 1
    else:
        ap.error("use --baseline FILE --cond C, or --gain FILE --control C --cand X")
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
