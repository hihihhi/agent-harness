"""quality_regrade.py RESULTS.jsonl OUT.jsonl CODE_DIR -- re-grade recorded quality runs with the current graders.

When a grader is corrected (a false positive found in a measured run), every arm is re-graded from the work
trees the runs left behind (CODE_DIR/<tool>-<cond>-<task>-quality-<rep>), so control and candidate are always
graded by the same instrument. The old verdicts are kept beside the new ones (`dims_before`). A missing work
tree is an error: no run is dropped or kept on its old grade.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quality_grade as G  # noqa: E402
from quality_tasks import TASKS  # noqa: E402

BY_ID = {t["id"]: t for t in TASKS}


def main(argv):
    src, dst, code = Path(argv[0]), Path(argv[1]), Path(argv[2])
    rows = [json.loads(ln) for ln in src.read_text(encoding="utf-8").splitlines() if ln.strip()]
    out, missing = [], []
    for r in rows:
        if r.get("kind") != "quality":
            out.append(r)
            continue
        work = code / r["key"].replace("|", "-")
        if not work.is_dir():
            missing.append(str(work))
            continue
        g = G.grade(BY_ID[r["task"]], work)
        details = g.pop("details")
        out.append({**r, "dims_before": r["dims"], "dims": g, "details": details, "changed": G.changed(work)})
    if missing:
        print("regrade: %d work tree(s) missing, nothing written: %s" % (len(missing), missing[:3]))
        return 1
    dst.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out), encoding="utf-8")
    flips = sum(r.get("dims_before", r["dims"]) != r["dims"] for r in out if r.get("kind") == "quality")
    print("regraded %d run(s); %d changed verdicts" % (sum(r.get("kind") == "quality" for r in out), flips))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
