"""The coding-quality eval (plan harness-coding): the instrument that decides whether a coding skill ships.
Written before the instrument. Two halves:
  instrument: every task's hidden graders can PASS (a known-good reference) and can FAIL (a known-bad reference
              per dimension); the untouched start fails something; a missing artefact or tool FAILS.
  gain_rule:  the candidate ships only on separated ranges, no dimension falling, tokens <= 1.15x, every run there.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import quality_grade as G  # noqa: E402
import quality_report as R  # noqa: E402
from quality_tasks import DIMENSIONS, TASKS  # noqa: E402


def _work(task, overlay=None):
    d = Path(tempfile.mkdtemp(prefix="q-"))
    G.materialise(task, d)
    for path, text in (overlay or {}).items():
        p = d / path
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


class TestInstrument(unittest.TestCase):
    """instrument: the graders are real before any agent is graded by them."""

    def test_instrument_has_twelve_tasks_covering_every_dimension_twice(self):
        self.assertGreaterEqual(len(TASKS), 12)
        self.assertEqual(len({t["id"] for t in TASKS}), len(TASKS), "task ids are unique")
        for dim in DIMENSIONS:
            n = sum(dim in t["dims"] for t in TASKS)
            self.assertGreaterEqual(n, 2, "%s is graded in only %d task(s)" % (dim, n))
        self.assertEqual(set(DIMENSIONS), {"hidden", "adequacy", "perf", "lint", "leak", "scope", "docs", "integrity"})

    def test_instrument_good_reference_passes_every_dimension(self):
        for t in TASKS:
            with self.subTest(task=t["id"]):
                d = _work(t, t["good"])
                try:
                    g = G.grade(t, d)
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                failed = {k: g["details"].get(k) for k in t["dims"] if not g[k]}
                self.assertEqual(failed, {}, "the good reference fails")

    def test_instrument_each_bad_reference_fails_its_dimension(self):
        for t in TASKS:
            for dim in t["dims"]:
                with self.subTest(task=t["id"], dim=dim):
                    self.assertIn(dim, t["bad"], "no bad reference proves the %s grader can fail" % dim)
                    d = _work(t, {**t["good"], **t["bad"][dim]})
                    try:
                        g = G.grade(t, d)
                    finally:
                        shutil.rmtree(d, ignore_errors=True)
                    self.assertFalse(g[dim], "the bad reference for %s passes it: %s" % (dim, g["details"].get(dim)))

    def test_instrument_untouched_start_is_not_already_solved(self):
        for t in TASKS:
            with self.subTest(task=t["id"]):
                d = _work(t)
                try:
                    g = G.grade(t, d)
                finally:
                    shutil.rmtree(d, ignore_errors=True)
                self.assertFalse(all(g[k] for k in t["dims"]), "doing nothing already scores full")

    def test_instrument_missing_implementation_fails_hidden_tests(self):
        t = next(t for t in TASKS if "hidden" in t["dims"])
        d = _work(t, {**t["good"], t["impl"]: None})
        try:
            self.assertFalse(G.grade(t, d)["hidden"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_instrument_no_visible_tests_fails_adequacy(self):
        t = next(t for t in TASKS if "adequacy" in t["dims"])
        overlay = dict(t["good"])
        for f in t["tests"]:
            overlay[f] = None
        d = _work(t, overlay)
        try:
            self.assertFalse(G.grade(t, d)["adequacy"], "no tests at all must not count as adequate")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_instrument_absent_linter_fails_lint_instead_of_passing(self):
        t = next(t for t in TASKS if "lint" in t["dims"])
        d = _work(t, t["good"])
        try:
            with mock.patch.object(G, "RUFF", ["no-such-linter-xyz"]):
                g = G.grade(t, d)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertFalse(g["lint"])
        self.assertIn("not found", g["details"]["lint"])

    def test_instrument_grading_does_not_change_the_work_tree(self):
        t = TASKS[0]
        d = _work(t, t["good"])
        try:
            before = subprocess.run(["git", "status", "--porcelain"], cwd=d, capture_output=True, text=True).stdout
            G.grade(t, d)
            after = subprocess.run(["git", "status", "--porcelain"], cwd=d, capture_output=True, text=True).stdout
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(before, after)


def _rows(cond, totals, tool="claude", tok=1000, dims=None):
    """One row per (task, rep) whose dimension passes sum to the given per-rep totals."""
    out = []
    for rep, total in enumerate(totals, 1):
        left = total
        for t in TASKS:
            ds = {}
            for dim in t["dims"]:
                ds[dim] = left > 0
                left -= ds[dim]
            if dims:
                ds.update(dims.get((t["id"], rep), {}))
            out.append({"tool": tool, "cond": cond, "task": t["id"], "rep": rep, "dims": ds, "tok_total": tok})
    return out


class TestGainRule(unittest.TestCase):
    """gain_rule: what lets a coding change ship."""

    def test_gain_rule_separated_ranges_pass(self):
        rows = _rows("C", [20, 21, 20]) + _rows("X", [24, 25, 24])
        ok, why = R.gain(rows, "C", "X")
        self.assertTrue(ok, why)

    def test_gain_rule_overlapping_ranges_fail(self):
        rows = _rows("C", [20, 23, 20]) + _rows("X", [23, 25, 26])
        ok, why = R.gain(rows, "C", "X")
        self.assertFalse(ok)
        self.assertIn("overlap", why)

    def test_gain_rule_a_falling_dimension_fails(self):
        t = next(t for t in TASKS if "leak" in t["dims"])
        rows = _rows("C", [20, 21, 20], dims={(t["id"], r): {"leak": True} for r in (1, 2, 3)})
        rows += _rows("X", [26, 27, 26], dims={(t["id"], r): {"leak": False} for r in (1, 2, 3)})
        ok, why = R.gain(rows, "C", "X")
        self.assertFalse(ok)
        self.assertIn("leak", why)

    def test_gain_rule_token_ceiling(self):
        rows = _rows("C", [20, 21, 20], tok=1000) + _rows("X", [24, 25, 24], tok=1200)
        ok, why = R.gain(rows, "C", "X")
        self.assertFalse(ok)
        self.assertIn("token", why)

    def test_gain_rule_missing_run_fails(self):
        rows = _rows("C", [20, 21, 20]) + _rows("X", [24, 25, 24])
        rows = [r for r in rows if not (r["cond"] == "X" and r["rep"] == 2 and r["task"] == TASKS[3]["id"])]
        ok, why = R.gain(rows, "C", "X")
        self.assertFalse(ok)
        self.assertIn("missing", why)

    def test_gain_rule_each_tool_must_gain(self):
        rows = (_rows("C", [20, 21, 20]) + _rows("X", [24, 25, 24])
                + _rows("C", [20, 21, 20], tool="codex") + _rows("X", [20, 21, 20], tool="codex"))
        ok, why = R.gain(rows, "C", "X")
        self.assertFalse(ok)
        self.assertIn("codex", why)

    def test_gain_rule_baseline_report_needs_every_run(self):
        full = _rows("C", [20, 21, 20]) + _rows("C", [20, 21, 20], tool="codex")
        self.assertEqual(R.baseline(full, "C")[0], 0)
        self.assertEqual(R.baseline(full[1:], "C")[0], 1)
        self.assertEqual(R.baseline([], "C")[0], 1)


if __name__ == "__main__":
    unittest.main()
