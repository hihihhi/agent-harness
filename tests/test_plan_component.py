"""The work graph (src/agent_harness/workgraph), merged in from agentic-os in v0.3.

Before v0.3 the work loop was a skill: advice the model could follow or not, with stages recorded
in free text by state_save. The graph makes it enforced: every node has a gate command, and a node
is done only when that command exits 0, run by the engine and not reported by the model.

Two kinds of test here:
  * the engine's own suites, vendored from agentic-os as tests/workgraph/*.test.sh, run against
    the vendored engine (they carried 553 assertions there and were adversarially certified);
  * what changed on the way in: gates call `plan`/`phase-check` by name, every gate goes through
    the guard first, and one MCP tool exposes the graph to every tool the harness installs into.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WG = os.path.join(ROOT, "src", "agent_harness", "workgraph")
PLAN = os.path.join(WG, "bin", "plan")
SUITES = os.path.join(HERE, "workgraph")

# Every suite that does not need the author's machine (a session hook, `srt`, or a live model).
PORTED = ["plan", "orch", "stress", "strict", "signature", "cert", "cert2", "gatelock", "phase",
          "recall-context", "progress", "resume", "default-on", "doctor"]


def _env(**extra):
    env = dict(os.environ, PLAN=PLAN, QUIET_TESTS="1", PLAN_QUIET="1")
    env.pop("PLAN_DIR", None)
    env.update(extra)
    return env


@pytest.mark.parametrize("suite", PORTED)
def test_vendored_suite(suite):
    path = os.path.join(SUITES, suite + ".test.sh")
    r = subprocess.run(["bash", path], capture_output=True, text=True, env=_env(), timeout=900)
    lines = (r.stdout + r.stderr).strip().splitlines()
    # Every FAIL line and the line after it, not just the tail: on CI a 25-line tail hid which assertions
    # actually failed, twice.
    fails = [ln for i, ln in enumerate(lines) if "FAIL " in ln or (i and "FAIL " in lines[i - 1])]
    assert r.returncode == 0 and "FAIL=0" in r.stdout, "\n".join(fails[:60] + lines[-3:])


def test_every_ported_suite_exists_and_none_is_silently_skipped():
    have = sorted(f[:-8] for f in os.listdir(SUITES) if f.endswith(".test.sh"))
    assert sorted(PORTED) == have


def _graph(tmp, body):
    os.makedirs(os.path.join(tmp, "plan"), exist_ok=True)
    with open(os.path.join(tmp, "plan", "g.md"), "w") as f:
        f.write("## goal: g\n" + body)
    subprocess.run(["git", "init", "-q", tmp], check=True)


def _plan(tmp, *args):
    # HOME is a throwaway directory. The guard test below gates on a destructive command on purpose; if the
    # guard check ever regressed, that gate would RUN, and it must only be able to delete this sandbox.
    sandbox = os.path.join(tmp, "home")
    os.makedirs(sandbox, exist_ok=True)
    return subprocess.run([PLAN, *args], cwd=tmp, capture_output=True, text=True, env=_env(HOME=sandbox),
                          timeout=120)


def _mark(tmp, nid):
    for line in open(os.path.join(tmp, "plan", "g.md")):
        if line.startswith("- [") and (" %d. " % nid) in line:
            return line[3]
    return None


def test_a_gate_is_judged_by_the_guard_before_it_runs():
    """A gate is a shell command the engine runs itself, so it never passes the PreToolUse hook. In
    agentic-os that meant `gate: rm -rf ~` plus `plan gate` was arbitrary execution with no guard at
    all. Now the guard judges every gate first; a refused gate is never run and the node stays pending."""
    with tempfile.TemporaryDirectory() as tmp:
        canary = os.path.join(tmp, "ran")
        _graph(tmp, "- [ ] 1. bad | gate: touch %s && rm -rf ~\n" % canary)
        r = _plan(tmp, "gate", "g", "1")
        assert r.returncode != 0
        assert "guard" in (r.stdout + r.stderr).lower()
        assert not os.path.exists(canary), "the refused gate ran anyway"
        assert _mark(tmp, 1) == " "


def test_an_ordinary_gate_still_runs():
    with tempfile.TemporaryDirectory() as tmp:
        _graph(tmp, "- [ ] 1. ok | gate: test 1 = 1\n- [ ] 2. fails | gate: test 1 = 2\n")
        assert _plan(tmp, "gate", "g", "1").returncode == 0 and _mark(tmp, 1) == "x"
        assert _plan(tmp, "gate", "g", "2").returncode != 0 and _mark(tmp, 2) == " "


def test_gates_can_call_plan_and_phase_check_by_name():
    """The scaffold's gates say `phase-check <slug> analysis`; that must resolve wherever the
    harness is installed, without anyone putting it on PATH."""
    with tempfile.TemporaryDirectory() as tmp:
        _graph(tmp, "- [ ] 1. a | gate: command -v plan && command -v phase-check\n")
        r = _plan(tmp, "gate", "g", "1")
        assert r.returncode == 0, r.stdout + r.stderr


def test_the_scaffold_gates_on_substance_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True)
        assert _plan(tmp, "new", "s", "ship it").returncode == 0
        text = open(os.path.join(tmp, "plan", "s.md")).read()
        assert "gate: phase-check s analysis" in text
        with open(os.path.join(tmp, "plan", "s.md"), "a") as f:
            f.write("\n## analysis\nx\n")
        assert _plan(tmp, "gate", "s", "1").returncode != 0, "a heading alone passed the analysis gate"
        with open(os.path.join(tmp, "plan", "s.md"), "a") as f:
            f.write("Asked: ship it. done when: `test -f out.txt` exits 0.\n")
        assert _plan(tmp, "gate", "s", "1").returncode == 0


# ---------------------------------------------------------------- the MCP tool

sys.path.insert(0, os.path.join(ROOT, "src"))


def _call_plan_tool(cwd, args):
    from agent_harness.mcp import server
    return server.Server().call_tool("plan", {"args": args, "project": cwd})


def test_one_mcp_tool_exposes_the_graph_and_it_is_on_by_default(monkeypatch):
    """On by default since v0.3.3 (the owner: the graph is how goals and checks survive context loss); exactly one
    tool, and HARNESS_DISABLE=plan takes it away."""
    from agent_harness.mcp import server
    monkeypatch.delenv("HARNESS_ENABLE", raising=False)
    monkeypatch.delenv("HARNESS_DISABLE", raising=False)
    assert [t["name"] for t in server.tool_list()].count("plan") == 1
    monkeypatch.setenv("HARNESS_DISABLE", "plan")
    assert "plan" not in [t["name"] for t in server.tool_list()]


def test_the_mcp_tool_runs_a_command_in_the_project():
    with tempfile.TemporaryDirectory() as tmp:
        _graph(tmp, "- [ ] 1. a | gate: true\n")
        out = json.dumps(_call_plan_tool(tmp, "status g"))
        assert "0/1" in out


def test_the_mcp_tool_will_not_start_the_orchestrator():
    """`plan run` dispatches worker sessions for as long as the graph takes; that belongs in a
    terminal the human can watch and stop, not inside one tool call with a timeout."""
    with tempfile.TemporaryDirectory() as tmp:
        _graph(tmp, "- [ ] 1. a | gate: true\n")
        out = json.dumps(_call_plan_tool(tmp, "run g"))
        assert "terminal" in out.lower()
        assert _mark(tmp, 1) == " "
