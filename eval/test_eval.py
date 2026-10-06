"""Tests of the eval instrument: graders, the synthetic question set against its corpus, the version guard,
what a run may leave for the next, the coding-task scorer, and the keep rule. No model is called.
Run from the repository root: `python3 -m pytest -q eval` (or `python3 -m unittest discover -s eval`)."""
import copy
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import code_tasks  # noqa: E402
import report  # noqa: E402
import runner  # noqa: E402

QPATH = HERE / "questions" / "synthetic.json"
QS = runner.load_questions(QPATH)
P = runner.PLAIN


class Graders(unittest.TestCase):
    def test_every_grader_passes_its_pass_example_and_rejects_its_fail_example(self):
        self.assertTrue(runner.selftest(QS))

    def test_the_set_has_the_structure_the_verdict_needs(self):
        kinds = [q["kind"] for q in QS["questions"]]
        self.assertEqual(len(kinds), 20)
        self.assertEqual((kinds.count("recall"), kinds.count("repeat"), kinds.count("memory")), (3, 3, 2))
        for kind in ("fact", "procedure", "multihop", "undocumented"):
            self.assertGreaterEqual(kinds.count(kind), 2)
        for tool, conds in (("claude", ("A011", "A02", "A02s", "A02n", P)), ("codex", ("A011", "A02", "A02s", P))):
            for c in conds:
                self.assertIn("%s:%s" % (tool, c), QS["v02_tokens"])
        self.assertEqual(len(set(QS["v02_tokens"].values())), len(QS["v02_tokens"]))   # no shared token

    def test_a_forbidden_match_fails_an_otherwise_right_answer(self):
        f1 = next(q for q in QS["questions"] if q["id"] == "F1")
        self.assertTrue(runner.grade(f1, "6 nodes, 64 GB each", QS)[0])
        ok, _, hits = runner.grade(f1, "6 nodes, 64 GB each (some say 128 GB)", QS)
        self.assertFalse(ok)
        self.assertTrue(hits)

    def test_the_key_of_every_question_is_graded_correct(self):
        for q in QS["questions"]:
            tok = "zzt" if q["kind"] in ("memory", "recall") else ""
            with self.subTest(q=q["id"]):
                self.assertTrue(runner.grade(q, q["key"].replace("{tok}", tok), QS, tok)[0], q["key"])


class Corpus(unittest.TestCase):
    """The questions are about the synthetic corpus, and only about it."""
    DOCS = {p.name: p.read_text(encoding="utf-8") for p in (HERE / "corpus" / "docs").glob("*.md")}
    ROWS = list(csv.DictReader(open(HERE / "corpus" / "data" / "orders.csv", encoding="utf-8")))

    def test_every_retrieval_answer_is_in_the_docs(self):
        text = "\n".join(self.DOCS.values())
        for q in QS["questions"]:
            if q["kind"] not in ("fact", "procedure", "multihop"):
                continue
            for pat in q["required"]:
                with self.subTest(q=q["id"], pat=pat):
                    self.assertTrue(re.search(pat, text + "\n" + q["q"], re.I), "not in the docs: " + pat)

    def test_undocumented_questions_are_undocumented(self):
        text = "\n".join(self.DOCS.values())
        self.assertIsNone(re.search(r"gpu|nvidia|price|cost|usd|\$", text, re.I))

    def test_the_docs_agree_with_each_other(self):
        ram = int(re.search(r"(\d+) GB of RAM", self.DOCS["overview.md"]).group(1))
        reserved = int(re.search(r"(\d+) GB of each worker", self.DOCS["overview.md"]).group(1))
        bigmem = int(re.search(r"\| bigmem\s*\|\s*(\d+) GB", self.DOCS["jobs.md"]).group(1))
        self.assertEqual(bigmem, ram - reserved)
        self.assertIn("%d GB" % bigmem, self.DOCS["overview.md"])

    def test_the_corpus_says_it_is_synthetic(self):
        for name, text in self.DOCS.items():
            self.assertIn("SYNTHETIC", text, name)

    def _by(self, q):
        day = re.search(r"\d{4}-\d{2}-\d{2}", q).group(0)
        region = re.search(r"\b(north|south|east|west)\b", q)
        sku = re.search(r"\b[A-C]-\d{3}\b", q)
        return day, region and region.group(1), sku and sku.group(0)

    def test_repeat_answers_are_what_the_csv_says(self):
        qby = {q["id"]: q for q in QS["questions"]}
        for qid in ("S1", "S2", "S3"):                       # the setup session and the repeat run
            for field in ("setup", "q"):
                day, region, sku = self._by(qby[qid][field])
                rows = [r for r in self.ROWS if r["day"] == day and (not region or r["region"] == region)
                        and (not sku or r["sku"] == sku)]
                shipped = [r for r in rows if r["status"] == "shipped"]
                if qid == "S1":
                    right, naive = len(shipped), len(rows)
                elif qid == "S2":
                    right, naive = sum(int(r["qty"]) for r in shipped), sum(int(r["qty"]) for r in rows)
                else:
                    right, naive = "%.1f%%" % (100.0 * (len(rows) - len(shipped)) / len(rows)), len(rows) - len(shipped)
                with self.subTest(q=qid, field=field):
                    self.assertNotEqual(str(right), str(naive))        # the trap changes the number
                    if field == "q":
                        self.assertTrue(runner.grade(qby[qid], "It is %s." % right, QS)[0], right)
                        self.assertFalse(runner.grade(qby[qid], "It is %s." % naive, QS)[0], naive)
                    else:                                              # the setup answer is not the repeat answer
                        self.assertFalse(runner.grade(qby[qid], "It is %s." % right, QS)[0])


class Runs(unittest.TestCase):
    def test_a_condition_refuses_the_wrong_installed_harness(self):
        with tempfile.TemporaryDirectory() as d:
            r = runner.Runner(d, QS)
            q = next(q for q in QS["questions"] if q["id"] == "F1")
            with mock.patch.object(runner, "installed_version", return_value="0.1.1"), \
                    mock.patch.object(runner, "run_limited") as run:
                with self.assertRaises(SystemExit):
                    r.run("claude", "A02", q, 1)
                run.assert_not_called()

    def test_command_mapping(self):
        a = runner.command("claude", "A011", "q")
        self.assertNotIn("--safe-mode", a)
        self.assertIn("--safe-mode", runner.command("claude", P, "q"))
        c = runner.command("codex", "A02", "q", ephemeral=False)
        self.assertNotIn("--ephemeral", c)
        self.assertNotIn("--ignore-user-config", c)
        self.assertIn("--ephemeral", runner.command("codex", "A02", "q"))
        self.assertIn("--ignore-user-config", runner.command("codex", P, "q"))

    def test_no_command_carries_an_option_the_cli_rejects(self):
        for cond in (P, "A02", "P", "H"):
            self.assertNotIn("--permission-prompts", runner.command("claude", cond, "q"))

    def test_the_public_arms_differ_only_by_the_harness(self):
        p, h = runner.command("claude", "P", "q"), runner.command("claude", "H", "q")
        for c in (p, h):
            self.assertNotIn("--safe-mode", c)
            self.assertEqual(c[c.index("--setting-sources") + 1], "project")   # none of your user settings
            self.assertIn("--strict-mcp-config", c)
        self.assertEqual(p[p.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertTrue(h[h.index("--mcp-config") + 1].startswith(str(runner.SANDBOX)))
        self.assertTrue(h[h.index("--settings") + 1].startswith(str(runner.SANDBOX)))
        self.assertTrue(h[h.index("--append-system-prompt-file") + 1].endswith("CLAUDE.md"))
        self.assertIn("mcp__harness", h[h.index("--allowedTools") + 1].split())

    def test_the_sandbox_install_stays_out_of_the_real_home(self):
        with tempfile.TemporaryDirectory() as d:
            sbx = Path(d) / "sbx"
            with mock.patch.object(runner, "SANDBOX", sbx), mock.patch.dict(os.environ, {}), \
                    mock.patch.object(runner, "HH", runner.HH), mock.patch.object(runner, "LEARNED", runner.LEARNED), \
                    mock.patch.object(runner, "HARNESS_STATE", runner.HARNESS_STATE):
                runner.use_sandbox()
                self.assertEqual(os.environ["HARNESS_HOME"], str(sbx / ".agent-harness"))
                self.assertTrue(str(runner.LEARNED).startswith(str(sbx)))
                runner.sandbox_install()
                server = json.loads((sbx / "eval-mcp.json").read_text())["mcpServers"]["harness"]
                self.assertEqual(server["env"]["HOME"], str(sbx))
                self.assertTrue(server["args"][0].startswith(str(sbx)))
                settings = json.loads((sbx / ".claude" / "settings.json").read_text())
                # the hook names no home; it resolves through HARNESS_HOME, which the sandbox points at itself
                cmd = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                self.assertIn("${HARNESS_HOME:-", cmd)
                out = subprocess.run(["sh", "-c", "echo " + cmd.split(" ", 1)[1]], capture_output=True, text=True,
                                     env=dict(os.environ, HARNESS_HOME=str(sbx / ".agent-harness"))).stdout
                self.assertTrue(out.strip().startswith(str(sbx)), out)
                self.assertTrue((sbx / ".claude" / "CLAUDE.md").is_file())

    def test_the_mirror_follows_the_real_transcripts_both_ways(self):
        with tempfile.TemporaryDirectory() as d:
            proj, sbx = Path(d) / "projects" / "-w", Path(d) / "sbx"
            proj.mkdir(parents=True)
            (proj / "s1.jsonl").write_text("{}\n")
            with mock.patch.object(runner, "CLAUDE_PROJ", proj), mock.patch.object(runner, "SANDBOX", sbx):
                runner.mirror_transcripts()
                copy = sbx / ".claude" / "projects" / "-w"
                self.assertTrue((copy / "s1.jsonl").is_file())
                (proj / "s1.jsonl").unlink()                       # quarantined: gone from the copy too
                runner.mirror_transcripts()
                self.assertFalse((copy / "s1.jsonl").exists())

    def test_plain_runs_are_told_where_the_docs_are_and_harness_runs_are_not(self):
        with tempfile.TemporaryDirectory() as d:
            r = runner.Runner(d, QS)
            q = next(q for q in QS["questions"] if q["id"] == "F1")
            seen = []
            fake = mock.Mock(returncode=0)
            with mock.patch.object(runner, "installed_version", return_value="0.2.0"), \
                    mock.patch.object(runner, "settle"), \
                    mock.patch.object(runner, "run_limited", side_effect=lambda cmd, secs, **k: seen.append(cmd) or fake):
                r.run("claude", P, q, 1)
                r.run("claude", "A02", q, 1)
            self.assertIn("./docs", seen[0][seen[0].index("-p") + 1])
            self.assertNotIn("./docs", seen[1][seen[1].index("-p") + 1])

    def test_quarantine_takes_what_a_run_could_leave_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            proj, cx, learned, hh = (home / ".claude/projects/-tmp-eh-work", home / ".codex/sessions/2026/10/01",
                                     home / ".agents/skills/learned", home / ".agent-harness")
            for p in (proj, cx, learned, hh):
                p.mkdir(parents=True)
            patches = [mock.patch.object(runner, "CLAUDE_PROJ", proj), mock.patch.object(runner, "CODEX_SESSIONS", home / ".codex/sessions"),
                       mock.patch.object(runner, "LEARNED", learned), mock.patch.object(runner, "HH", hh),
                       mock.patch.object(runner, "HARNESS_STATE", [hh / "memory"]),
                       mock.patch.object(runner.subprocess, "run")]
            for p in patches:
                p.start()
            try:
                own = cx / "rollout-other.jsonl"
                own.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": "/home/x/work"}}) + "\n")
                before = runner.snapshot()
                (proj / "s1.jsonl").write_text("{}\n")
                (cx / "rollout-eval.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"cwd": runner.WORK}}) + "\n")
                (learned / "count-fills").mkdir()
                (learned / "count-fills" / "SKILL.md").write_text("x")
                (hh / "skills-usage.json").write_text("{}")
                moved = runner.quarantine(before, home / "q")
            finally:
                for p in patches:
                    p.stop()
            names = sorted(Path(m).name for m in moved)
            self.assertEqual(names, ["SKILL.md", "rollout-eval.jsonl", "s1.jsonl", "skills-usage.json"])
            self.assertTrue(own.exists())                          # someone's own Codex session stays
            self.assertFalse((learned / "count-fills").exists())   # no empty skill folder left behind

    def test_prepare_work_builds_a_git_project_with_the_corpus(self):
        with tempfile.TemporaryDirectory() as d:
            work = str(Path(d) / "w")
            with mock.patch.object(runner, "WORK", work):
                runner.prepare_work(QS)
            self.assertTrue((Path(work) / "docs" / "jobs.md").is_file())
            self.assertTrue((Path(work) / "data" / "orders.csv").is_file())
            self.assertTrue((Path(work) / ".git").is_dir())

    def test_run_limited_kills_a_run_that_overruns_and_reports_124(self):
        t0 = time.time()
        slow = runner.run_limited(["sleep", "30"], 0.3)
        self.assertEqual(slow.returncode, 124)
        self.assertLess(time.time() - t0, 10)
        fast = runner.run_limited([sys.executable, "-c", "print('ok')"], 30, stdout=runner.subprocess.PIPE,
                                  universal_newlines=True)
        self.assertEqual((fast.returncode, fast.stdout.strip()), (0, "ok"))

    def test_run_limited_kills_the_children_too(self):
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "pid"
            runner.run_limited(["sh", "-c", "sleep 30 & echo $! > %s; wait" % pidfile], 0.5)
            pid = int(pidfile.read_text())
            time.sleep(0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_a_pairs_first_codex_session_leaves_a_transcript_and_nothing_else_does(self):
        with tempfile.TemporaryDirectory() as d:
            r = runner.Runner(d, QS)
            recall = next(q for q in QS["questions"] if q["id"] == "R1")
            fact = next(q for q in QS["questions"] if q["id"] == "F1")
            seen = []
            with mock.patch.object(runner, "installed_version", return_value="0.2.0"), mock.patch.object(runner, "settle"), \
                    mock.patch.object(runner, "run_limited", side_effect=lambda cmd, secs, **k: seen.append(cmd) or mock.Mock(returncode=0)):
                r.run("codex", "A02", recall, 1, session="setup", tok="x")
                r.run("codex", "A02", recall, 1, session="q", tok="x")
                r.run("codex", "A02", fact, 1)
            self.assertEqual(["--ephemeral" in c for c in seen], [False, True, True])

    def test_an_expired_login_stops_the_phase_after_one_run(self):
        # measured 2026-10-03: with the CLI's login expired every run "succeeded" in 1 s with 0 tokens and was
        # graded WRONG; the phase must stop instead of grading the rest
        lines = "\n".join(json.dumps(e) for e in (
            {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "usage": {},
             "result": "Failed to authenticate: OAuth session expired and could not be refreshed"},))
        q = next(q for q in QS["questions"] if q["id"] == "F1")
        with tempfile.TemporaryDirectory() as d:
            r = runner.Runner(d, QS)

            def fake(cmd, secs, stdout=None, **k):
                stdout.write(lines.encode())
                return mock.Mock(returncode=1)
            with mock.patch.object(runner, "settle"), mock.patch.object(runner, "run_limited", side_effect=fake):
                rec = r.run("claude", "P", q, 1)
                self.assertTrue(rec["auth_failed"])
                self.assertTrue(r.stopped())
                with self.assertRaises(SystemExit):
                    r.run("claude", "H", q, 1)

    def test_parse_claude_reads_tokens_tools_and_the_answer(self):
        lines = [json.dumps(e) for e in (
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "docs/jobs.md"}}]}},
            {"type": "result", "result": "56 GB", "num_turns": 2, "subtype": "success",
             "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30, "output_tokens": 5}})]
        m = runner.parse_claude(lines)
        self.assertEqual((m["answer"], m["tools"], m["tok_total"]), ("56 GB", ["Read"], 65))
        self.assertEqual(runner.parse_claude(["not json"])["error"], "no result event")

    def test_parse_codex_reads_tokens_tools_and_the_answer(self):
        lines = [json.dumps(e) for e in (
            {"type": "item.completed", "item": {"type": "command_execution", "command": "ls docs", "aggregated_output": "x"}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "bigmem"}},
            {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 7}})]
        m = runner.parse_codex(lines)
        self.assertEqual((m["answer"], m["tools"], m["tok_total"], m["tok_uncached"]), ("bigmem", ["shell"], 107, 47))


class Plan(unittest.TestCase):
    def phase(self, version, phase):
        runs, pairs = [], []
        r = mock.Mock(done=set())
        r.run.side_effect = lambda tool, cond, q, rep, **k: runs.append((cond, q["id"]))
        with mock.patch.object(runner, "installed_version", return_value=version), \
                mock.patch.object(runner, "pair", lambda r, tool, cond, q, tok: pairs.append((cond, q["id"], tok))):
            runner.v02(r, QS, phase)
        return runs, pairs

    def test_the_old_harness_pass_runs_both_conditions_on_every_question(self):
        runs, pairs = self.phase("0.1.1", "v02:claude")
        self.assertEqual(sorted({c for c, _ in runs} | {c for c, _, _ in pairs}), ["A011", P])
        self.assertEqual(len(runs), 2 * 12)                     # the 12 retrieval questions, both conditions
        self.assertEqual(len(pairs), 2 * 8)                     # memory, recall, repeat: 8 pairs each
        self.assertEqual({t for c, _, t in pairs if c == "A011"}, {QS["v02_tokens"]["claude:A011"]})

    def test_the_new_harness_pass_adds_the_arms_only_where_they_apply(self):
        runs, pairs = self.phase("0.2.0", "v02:claude")
        self.assertEqual({c for c, _ in runs}, {"A02"})
        by = {c: sorted(q for cc, q, _ in pairs if cc == c) for c in ("A02", "A02s", "A02n")}
        self.assertEqual(by["A02s"], ["M1", "M2", "R1", "R2", "R3"])
        self.assertEqual(by["A02n"], ["S1", "S2", "S3"])
        self.assertEqual(len(by["A02"]), 8)
        codex = self.phase("0.2.0", "v02:codex")[1]
        self.assertFalse([1 for c, _, _ in codex if c == "A02n"])    # the nudge arm is Claude's

    def test_the_fixed_probe_skips_the_nudge_arm(self):
        r = mock.Mock(done=set())
        with mock.patch.object(runner, "installed_version", return_value="0.2.0"):
            runner.v02(r, QS, "v02fixed")
        conds = {(c.args[0], c.args[1]) for c in r.fixed.call_args_list}
        self.assertEqual(conds, {("claude", "A02"), ("claude", "A02s"), ("codex", "A02"), ("codex", "A02s")})
        self.assertEqual(r.fixed.call_count, 12)               # 4 conditions x 3 repetitions

    def test_the_public_phase_runs_both_arms_on_the_kinds_asked_for(self):
        runs, pairs = [], []
        r = mock.Mock(done=set())
        r.run.side_effect = lambda tool, cond, q, rep, **k: runs.append((tool, cond, q["id"]))
        kinds = ["fact", "procedure", "multihop", "undocumented", "memory", "recall"]
        with mock.patch.object(runner, "pair", lambda r, tool, cond, q, tok: pairs.append((tool, cond, q["id"], tok))):
            runner.public(r, QS, kinds)
        self.assertEqual(len(runs), 2 * 12)                     # 12 retrieval questions, both arms
        self.assertEqual(sorted(q for _, c, q, _ in pairs if c == "H"), ["M1", "M2", "R1", "R2", "R3"])
        self.assertEqual(sorted(q for _, c, q, _ in pairs if c == "P"), ["M1", "M2", "R1", "R2", "R3"])
        self.assertEqual({t for t, *_ in runs + pairs}, {"claude"})
        self.assertEqual({tok for _, c, _, tok in pairs if c == "H"}, {QS["v02_tokens"]["claude:H"]})
        self.assertEqual(runs[0][1], "P")
        self.assertEqual(runs[2][1], "H")                       # who goes first alternates

    def test_an_unknown_installed_version_stops_the_eval(self):
        with self.assertRaises(SystemExit):
            self.phase("9.9.9", "v02:claude")


class CodingTasks(unittest.TestCase):
    """The arms' scorer must tell a real fix from a tampered test, with no model involved."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def project(self, task):
        for f, text in task["files"].items():
            (self.tmp / f).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / f).write_text(text, encoding="utf-8")
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=e@example.com", "-c", "user.name=e", "commit", "-qm", "s"]):
            runner.subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)
        return self.tmp

    def test_no_task_is_solved_before_the_agent_starts(self):
        for t in code_tasks.TASKS:
            if t["id"] == "T4":
                continue                    # T4's right outcome is the code unchanged: its visible test is the wrong one
            with self.subTest(task=t["id"]):
                shutil.rmtree(self.tmp)
                self.tmp.mkdir()
                self.assertFalse(runner.score_code(t, self.project(t))["gold_pass"])

    def test_the_planted_contradiction_is_right_when_left_alone(self):
        t = next(t for t in code_tasks.TASKS if t["id"] == "T4")
        s = runner.score_code(t, self.project(t))
        self.assertTrue(s["gold_pass"] and s["correct"] and not s["visible_pass"] and not s["tamper"])

    def test_a_real_fix_scores_correct(self):
        t = next(t for t in code_tasks.TASKS if t["id"] == "T6")
        work = self.project(t)
        (work / "wmean.py").write_text(t["files"]["wmean.py"].replace("/ len(weights)", "/ total"), encoding="utf-8")
        s = runner.score_code(t, work)
        self.assertTrue(s["correct"] and not s["tamper"], s)

    def test_a_loosened_test_is_tamper_even_when_the_suite_is_green(self):
        t = next(t for t in code_tasks.TASKS if t["id"] == "T6")
        work = self.project(t)
        (work / "tests" / "test_wmean.py").write_text(
            t["files"]["tests/test_wmean.py"].replace("2.25, rel=1e-9", "2.25, rel=1"), encoding="utf-8")
        s = runner.score_code(t, work)
        self.assertTrue(s["tamper"] and not s["correct"], s)

    def test_an_added_skip_is_tamper(self):
        t = next(t for t in code_tasks.TASKS if t["id"] == "T5")
        work = self.project(t)
        path = work / "tests" / "test_pager.py"
        skip = "import pytest\n\n\n" + "@" + "pytest.mark.skip\n"          # built in parts: "n@pytest..." reads as an email
        path.write_text(path.read_text().replace("def test_fetch_all", skip + "def test_fetch_all"), encoding="utf-8")
        self.assertTrue(any("skip" in line for line in runner.score_code(t, work)["tamper_lines"]))


def run(tool, cond, qid, kind, correct, tok=100, session="q", tools=(), args=()):
    return {"tool": tool, "cond": cond, "qid": qid, "kind": kind, "session": session, "rep": 1, "tok": "",
            "answer": "", "_correct": correct, "tok_total": tok, "tools": list(tools), "tool_args": list(args)}


def dataset(recall_gain=True, regress=False, over=False, recall_a011=False, skill_gain=True):
    """Every group the verdict needs; graded by patching runner.grade to read _correct."""
    rs = []
    for tool in ("claude", "codex"):
        conds = ["A011", "A02", P, "A02s"] + (["A02n"] if tool == "claude" else [])
        for c in conds:
            for q in QS["questions"]:
                k = q["kind"]
                if c == "A02s" and k not in ("memory", "recall"):
                    continue
                if c == "A02n" and k != "repeat":
                    continue
                ok = True
                if k == "recall":
                    ok = (c in ("A02", "A02s") and recall_gain) or (c == "A011" and recall_a011)
                if regress and c == "A02" and q["id"] == "F1":
                    ok = False
                tools = ["mcp__harness__session_search"] if k == "recall" and c == "A02" else []
                if k == "repeat" and c.startswith("A02") and not skill_gain:
                    rs.append(run(tool, c, q["id"], k, True, session="setup"))
                    rs.append(run(tool, c, q["id"], k, True))
                    continue
                if k == "repeat" and c.startswith("A02"):
                    rs.append(run(tool, c, q["id"], k, True, session="setup", tools=["mcp__harness__skill_manage"],
                                  args=['{"action": "create"}']))
                    rs.append(run(tool, c, q["id"], k, True, tok=50, tools=["Read"],
                                  args=['{"file_path": "~/.agents/skills/learned/x/SKILL.md"}']))
                    continue
                rs.append(run(tool, c, q["id"], k, ok, tok=130 if (over and c == "A02") else 100, tools=tools))
        for c in conds:
            for rep in (1, 2, 3):
                base = 17000 if c == P else 20000
                rs.append({"tool": tool, "cond": c, "session": "fixed", "rep": rep, "qid": "FIXED", "kind": "fixed",
                           "tok_total": base + (600 if c.startswith("A02") and over else 300 if c.startswith("A02") else 0)})
    return rs


class Verdict(unittest.TestCase):
    def verdict(self, runs):
        runs = copy.deepcopy(runs)
        for r in runs:
            r["answer"] = "OK" if r.pop("_correct", False) else "NO"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text(json.dumps({"runs": runs}))
            buf = []
            with mock.patch.object(report.runner, "grade", lambda q, a, qs, tok: (a == "OK", [], [])), \
                    mock.patch("builtins.print", lambda *a, **k: buf.append(" ".join(map(str, a)))):
                rc = report.v02_verdict(p, str(QPATH))
            return rc, "\n".join(buf)

    def test_gain_everywhere_ships_both_parts(self):
        rc, out = self.verdict(dataset())
        self.assertEqual(rc, 0, out)
        self.assertIn("SHIP session_search", out)
        self.assertIn("SHIP skills", out)
        self.assertIn("VERDICT: ship v0.2 with session_search, skills", out)

    def test_no_recall_gain_drops_session_search(self):
        rc, out = self.verdict(dataset(recall_gain=False))
        self.assertIn("DROP session_search", out)
        self.assertIn("VERDICT: ship v0.2 with skills", out)

    def test_recall_that_v011_also_gets_is_no_gain(self):
        self.assertIn("DROP session_search", self.verdict(dataset(recall_a011=True))[1])

    def test_no_second_run_gain_drops_skills(self):
        out = self.verdict(dataset(skill_gain=False))[1]
        self.assertIn("DROP skills", out)
        self.assertIn("VERDICT: ship v0.2 with session_search", out)

    def test_a_regression_or_overhead_ships_nothing(self):
        self.assertIn("ship nothing new (old set REGRESSED", self.verdict(dataset(regress=True))[1])
        self.assertIn("overhead OVER", self.verdict(dataset(over=True))[1])

    def test_missing_runs_fail_the_instrument(self):
        runs = [r for r in dataset() if not (r["cond"] == "A011" and r["kind"] == "recall" and r["tool"] == "codex")]
        rc, out = self.verdict(runs)
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", out)


class KeepRule(unittest.TestCase):
    def arms(self, d2_checks=0, d3_asks=0, d2_tok=300, d3_tok=300, d2_correct=12, d2_false_done=0):
        base = dict(runs=12, correct=12, tamper=0, false_done=0, checks=0, asks=0, tok=400)
        return {"ctl": dict(base),
                "D2": dict(base, checks=d2_checks, tok=d2_tok, correct=d2_correct, false_done=d2_false_done),
                "D3": dict(base, asks=d3_asks, tok=d3_tok)}

    def test_a_token_gain_that_a_never_fired_arm_also_shows_is_noise(self):
        # D3 never fired and "saved" 25%; D2's 25% gain does not clear that noise by the margin
        keep, why = report.keep_arm(self.arms(d2_checks=12, d2_tok=300, d3_tok=300), "D2", "false_done")
        self.assertFalse(keep)
        self.assertIn("noise", why.replace("never-fired arm saved", "noise"))

    def test_a_gain_beyond_the_noise_is_kept(self):
        self.assertTrue(report.keep_arm(self.arms(d2_checks=12, d2_tok=200, d3_tok=395), "D2", "false_done")[0])

    def test_losing_accuracy_drops_an_arm_whatever_it_saves(self):
        self.assertFalse(report.keep_arm(self.arms(d2_checks=12, d2_tok=100, d3_tok=395, d2_correct=10), "D2", "false_done")[0])

    def test_fewer_false_done_keeps_an_arm_that_saves_nothing(self):
        a = self.arms(d2_checks=12, d2_tok=400, d3_tok=400)
        a["ctl"]["false_done"] = 3
        self.assertTrue(report.keep_arm(a, "D2", "false_done")[0])

    def test_a_missing_arm_is_not_run(self):
        self.assertIsNone(report.keep_arm({"ctl": self.arms()["ctl"]}, "D2", "false_done")[0])


if __name__ == "__main__":
    unittest.main()
