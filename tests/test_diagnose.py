"""`harness explain`: is a session genuinely using the harness? (plan harness-v05, R10)

A receipt separates installed / loaded / selected / invoked / verified, from the files on disk and the session's
own transcript. A signal the client does not record is NOT_OBSERVABLE, never a guessed pass. Written before the code.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_harness import diagnose as D  # noqa: E402

MARK = "# Working rules\n\nFor every session and project."


def jsonl(path, events):
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def claude_events(skill=True, check_exit=None):
    ev = [{"type": "user", "message": {"role": "user", "content": "fix the bug"}}]
    content = []
    if skill:
        content.append({"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "production-code"}})
    content += [{"type": "tool_use", "id": "m1", "name": "mcp__harness__mem_search", "input": {"query": "deploy"}},
                {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "python3 -m pytest -q tests"}},
                {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"subagent_type": "harness-review"}}]
    ev.append({"type": "assistant", "message": {"id": "msg1", "role": "assistant", "content": content,
                                                "usage": {"input_tokens": 100, "output_tokens": 20,
                                                          "cache_read_input_tokens": 900,
                                                          "cache_creation_input_tokens": 50}}})
    out = "3 passed" if check_exit is None else "Exit code %d\n1 failed" % check_exit
    ev.append({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "b1", "content": out, "is_error": check_exit is not None}]}})
    ev.append({"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "summary"}})
    return ev


def codex_events(rules=MARK, skill_read=True):
    ev = [{"type": "session_meta", "payload": {"session_id": "x"}},
          {"type": "world_state", "payload": {"state": {"agents_md": {"text": rules},
                                                        "host_skills": [{"name": "production-code"}]}}}]
    cmd = "cat ~/.agents/skills/production-code/SKILL.md; python3 -m pytest -q" if skill_read else "ls"
    ev.append({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1",
                                                    "input": "tools.exec_command({cmd:%s})" % json.dumps(cmd)}})
    ev.append({"type": "response_item", "payload": {"type": "function_call", "namespace": "harness",
                                                    "name": "kb_get", "arguments": "{\"id\": \"MACHINE.md#gpu\"}"}})
    ev.append({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
        "input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 300, "reasoning_output_tokens": 120}}}})
    return ev


class TestExplain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="diag-"))
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude" / "CLAUDE.md").write_text(MARK + "\n", encoding="utf-8")
        (self.home / ".codex" / "AGENTS.md").write_text(MARK + "\n", encoding="utf-8")

    def test_claude_session_shows_invoked_skill_memory_check_worker_tokens(self):
        t = jsonl(self.tmp / "s.jsonl", claude_events())
        r = D.explain("claude-code", t, self.home)
        self.assertEqual(r["instructions"]["installed"], "yes")
        self.assertEqual(r["instructions"]["loaded_in_session"], D.NOT_OBSERVABLE)   # Claude does not log it
        self.assertEqual(r["skills"]["invoked"], ["production-code"])
        self.assertEqual([c["tool"] for c in r["memory"]], ["mem_search"])
        self.assertEqual(r["checks"][0]["exit"], 0)
        self.assertEqual(r["workers"], ["Agent:harness-review"])
        self.assertEqual(r["compactions"], 1)
        self.assertEqual(r["tokens"]["input"], 100)
        self.assertEqual(r["tokens"]["cache_read"], 900)

    def test_negative_control_no_skill_and_failed_check_are_reported_not_hidden(self):
        t = jsonl(self.tmp / "s.jsonl", claude_events(skill=False, check_exit=1))
        r = D.explain("claude-code", t, self.home)
        self.assertEqual(r["skills"]["invoked"], [])
        self.assertEqual(r["checks"][0]["exit"], 1)
        self.assertIn("no skill invoked", " ".join(r["omissions"]))
        self.assertIn("1 of 1 checks failed", " ".join(r["omissions"]))

    def test_negative_control_missing_rules_file_is_detected(self):
        (self.home / ".claude" / "CLAUDE.md").unlink()
        r = D.explain("claude-code", jsonl(self.tmp / "s.jsonl", claude_events()), self.home)
        self.assertEqual(r["instructions"]["installed"], "MISSING")
        self.assertIn("rules file missing", " ".join(r["omissions"]))

    def test_codex_loaded_rules_are_verified_from_its_transcript(self):
        r = D.explain("codex", jsonl(self.tmp / "r.jsonl", codex_events()), self.home)
        self.assertEqual(r["instructions"]["loaded_in_session"], "yes")
        self.assertEqual(r["skills"]["available"], ["production-code"])
        self.assertEqual(r["skills"]["invoked"], ["production-code"])
        self.assertEqual([c["tool"] for c in r["memory"]], ["kb_get"])
        self.assertEqual(r["tokens"]["reasoning"], 120)

    def test_codex_skill_list_in_its_current_markdown_form(self):
        ev = codex_events()
        ev[1]["payload"]["state"]["host_skills"] = {"body": "## Skills\n- `r0` = `/x/skills`\n- imagegen: Make images\n"
                                                            "- production-code: Production bar\n"}
        r = D.explain("codex", jsonl(self.tmp / "r.jsonl", ev), self.home)
        self.assertEqual(r["skills"]["available"], ["imagegen", "production-code"])

    def test_long_session_renders_counts_not_every_check(self):
        ev = claude_events()
        for i in range(300):
            ev.append({"type": "assistant", "message": {"id": "m%d" % i, "content": [
                {"type": "tool_use", "id": "b%d" % i, "name": "Bash", "input": {"command": "python3 -m pytest -q"}}]}})
            ev.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "b%d" % i,
                                                                 "content": "ok"}]}})
        out = D.render(D.explain("claude-code", jsonl(self.tmp / "s.jsonl", ev), self.home))
        self.assertLess(max(len(x) for x in out.splitlines()), 200)
        self.assertIn("301 passed", out)

    def test_negative_control_codex_session_without_the_harness_rules(self):
        r = D.explain("codex", jsonl(self.tmp / "r.jsonl", codex_events(rules="# Some other rules")), self.home)
        self.assertEqual(r["instructions"]["loaded_in_session"], "no")
        self.assertIn("harness rules not loaded", " ".join(r["omissions"]))

    def test_unknown_usage_is_not_observable_not_zero(self):
        ev = [e for e in claude_events() if e["type"] != "assistant"]
        r = D.explain("claude-code", jsonl(self.tmp / "s.jsonl", ev), self.home)
        self.assertEqual(r["tokens"], D.NOT_OBSERVABLE)

    def test_no_secret_or_message_text_is_copied(self):
        ev = claude_events()
        ev[0]["message"]["content"] = "my token is sk-test-SECRET123"
        r = json.dumps(D.explain("claude-code", jsonl(self.tmp / "s.jsonl", ev), self.home))
        self.assertNotIn("SECRET123", r)

    def test_render_is_compact(self):
        r = D.explain("claude-code", jsonl(self.tmp / "s.jsonl", claude_events()), self.home)
        self.assertLessEqual(len(D.render(r).splitlines()), 15)


if __name__ == "__main__":
    unittest.main()
