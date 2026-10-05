"""plan run's routing: which tier a worker gets, and the Codex worker's command and verdict."""
import importlib.util
import json
import os
import tempfile

_spec = importlib.util.spec_from_file_location(
    "plan_mod", os.path.join(os.path.dirname(__file__), "..", "src", "agent_harness", "workgraph", "plan.py"))
plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan)


def node(notes=(), **fields):
    return {"fields": fields, "notes": list(notes)}


FAIL = "2026-10-06T01:00:00Z exit 1 in 3s [abcdef12] — FAIL x"
PASS = "2026-10-06T01:00:00Z exit 0 in 3s [abcdef12] — ok"


def test_first_attempt_is_standard():
    assert plan.tier_for(node()) == "standard"
    assert plan.tier_for(node([PASS])) == "standard"


def test_a_failed_gate_escalates_to_deep():
    assert plan.tier_for(node([FAIL])) == "deep"


def test_a_worker_written_line_is_not_an_attempt():
    # only lines the tool wrote count (anchored timestamp), so a gate printing "exit 1 —" cannot escalate
    assert plan.tier_for(node(["the gate printed exit 1 in 3s — something"])) == "standard"


def test_the_node_names_its_tier():
    assert plan.tier_for(node([FAIL], tier="quick")) == "quick"


def test_codex_cmd_reads_the_harness_tier_file():
    with tempfile.TemporaryDirectory() as h:
        os.makedirs(os.path.join(h, ".codex", "agents"))
        with open(os.path.join(h, ".codex", "agents", "harness-deep.toml"), "w") as f:
            f.write('name = "harness-deep"\nmodel = "m-x"\nmodel_reasoning_effort = "high"\n')
        cmd = plan.codex_cmd("P", "deep", home=h)
        assert cmd[:3] == ["codex", "exec", "--json"] and cmd[-1] == "P"
        assert cmd[cmd.index("-m") + 1] == "m-x"
        assert 'model_reasoning_effort="high"' in cmd
        # an explicit --model wins and no tier effort is forced on it
        cmd = plan.codex_cmd("P", "deep", model="m-y", home=h)
        assert cmd[cmd.index("-m") + 1] == "m-y" and not any("effort" in c for c in cmd)
        # no tier file: nothing pinned, the account's defaults
        assert "-m" not in plan.codex_cmd("P", "quick", home=h)


def test_codex_verdicts():
    out = "\n".join(json.dumps(e) for e in [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done: gate passes"}}])
    v = plan.classify_worker(plan.codex_info(0, out, ""))
    assert v["ok"] and not v["quota"]
    v = plan.classify_worker(plan.codex_info(1, "", "ERROR: You've hit your usage limit. Try again later."))
    assert v["quota"] and not v["ok"]
    v = plan.classify_worker(plan.codex_info(1, "", "Error: not logged in"))
    assert v["auth"]
    v = plan.classify_worker(plan.codex_info(2, "", ""))
    assert v["dispatch_failed"] and "codex exited 2" in v["why"]


def test_runner_choice(monkeypatch):
    monkeypatch.setenv("PLAN_RUNNER", "codex")
    assert plan.runner() == "codex"
    monkeypatch.setenv("PLAN_RUNNER", "")
    monkeypatch.setattr(plan.shutil, "which", lambda c: "/x/codex" if c == "codex" else None)
    assert plan.runner() == "codex"
    monkeypatch.setattr(plan.shutil, "which", lambda c: "/x/" + c)
    assert plan.runner() == "claude"
