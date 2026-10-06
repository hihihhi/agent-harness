"""Claude Code and Codex adapters, their hooks, and the live canary check (HARNESS_LIVE=1)."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from agent_harness import cli  # noqa: E402
from agent_harness import installer as I  # noqa: E402
from agent_harness.adapters import all_adapters  # noqa: E402
from agent_harness.adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from agent_harness.adapters.codex import CodexAdapter  # noqa: E402
from test_installer import CANARY, make_source, run  # noqa: E402


class Ctxd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._managed = mock.patch.dict(os.environ, {"HARNESS_CLAUDE_MANAGED": str(self.tmp / "no-managed")})
        self._managed.start()
        self.addCleanup(self._managed.stop)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.src = make_source(self.tmp)
        self.hh = self.home / ".agent-harness"
        self.ctx = I.make_ctx(self.home, self.hh, self.src / "content", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ClaudeCode(Ctxd):
    def test_registry_has_ours(self):
        self.assertIn("claude-code", all_adapters())
        self.assertIn("codex", all_adapters())

    def test_plan_shape(self):
        ch = {str(c.path.relative_to(self.home)): c for c in ClaudeCodeAdapter().plan(self.ctx)}
        self.assertEqual(ch[".claude/CLAUDE.md"].kind, "replace")
        self.assertIn(CANARY, ch[".claude/CLAUDE.md"].content)
        s = ch[".claude/settings.json"].content
        self.assertEqual(s["hooks"]["PreToolUse"][0]["matcher"], "Bash")
        self.assertEqual(s["hooks"]["PreToolUse"][1]["matcher"], "Edit|Write|MultiEdit")
        self.assertIn("check_guard.py", s["hooks"]["PreToolUse"][1]["hooks"][0]["command"])
        # $HOME-relative: the same settings.json works for any account it is synced to (no username written)
        self.assertEqual('python3 "${HARNESS_HOME:-$HOME/.agent-harness}"/content/hooks/guard.py',
                         s["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn("_hook-stop", s["hooks"]["Stop"][0]["hooks"][0]["command"])
        self.assertIn("_hook-prompt", s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
        self.assertIn("memory digest", s["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        self.assertEqual(len(s["hooks"]["SessionStart"]), 2)
        self.assertIn("state print", s["hooks"]["SessionStart"][1]["hooks"][0]["command"])
        self.assertEqual(s["hooks"]["SessionStart"][1]["matcher"], "startup|resume|compact")
        self.assertIn("state_save", s["hooks"]["PreCompact"][0]["hooks"][0]["command"])
        self.assertEqual(s["autoMemoryDirectory"], str(self.hh / "memory" / "claude-code"))
        mcp = ch[".claude.json"].content["mcpServers"]["harness"]
        self.assertIs(mcp["alwaysLoad"], True)
        self.assertEqual([mcp["command"]] + mcp["args"], I.mcp_cmd(self.hh))
        self.assertEqual(ch[".agents/skills/demo"].kind, "copy-dir")
        self.assertEqual(ch[".claude/skills/demo"].kind, "symlink")
        self.assertEqual(ch[".claude/commands/hello.md"].kind, "replace")
        post = ClaudeCodeAdapter().post_install(self.ctx)
        self.assertTrue(post[0].startswith("claude mcp add -s user harness -- python3 "))

    def test_preapproves_only_our_server(self):
        self.ctx.extra_mcp = {"fetch": ["uvx", "mcp-server-fetch"]}
        s = next(c for c in ClaudeCodeAdapter().plan(self.ctx) if c.path.name == "settings.json").content
        self.assertEqual(s["permissions"], {"allow": ["mcp__harness"]})
        mine = {"permissions": {"allow": ["Bash(ls:*)", "mcp__other"], "deny": ["Read(./.env)"]}}
        merged = I.json_merge(mine, s)
        self.assertEqual(merged["permissions"]["allow"], ["Bash(ls:*)", "mcp__other", "mcp__harness"])
        self.assertEqual(merged["permissions"]["deny"], ["Read(./.env)"])
        self.assertEqual(I.json_unmerge(merged, s, mine), mine)

    def test_user_hooks_survive_merge(self):
        mine = {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "me"}]}],
                          "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "mine2"}]}]},
                "env": {"A": "1"}}
        ours = next(c for c in ClaudeCodeAdapter().plan(self.ctx) if c.path.name == "settings.json").content
        merged = I.json_merge(mine, ours)
        self.assertEqual(merged["env"], {"A": "1"})
        self.assertEqual(merged["hooks"]["SessionStart"][0], mine["hooks"]["SessionStart"][0])
        self.assertEqual(len(merged["hooks"]["SessionStart"]), 3)
        self.assertEqual(merged["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"], "mine2")
        self.assertEqual(len(merged["hooks"]["UserPromptSubmit"]), 2)
        self.assertEqual(I.json_merge(merged, ours), merged)  # idempotent
        self.assertEqual(I.json_unmerge(merged, ours, mine), mine)

    def test_update_replaces_the_harness_entries(self):
        """v0.1.0 -> v0.1.1: the old SessionStart 'resume|compact' state hook must not stay beside the new one."""
        claude = self.home / ".claude"
        claude.mkdir()
        mine = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}, "model": "x"}
        (claude / "settings.json").write_text(json.dumps(mine))
        new = [c for c in ClaudeCodeAdapter().plan(self.ctx) if c.path.name == "settings.json"]
        old = I.FileChange(new[0].path, "merge-json", {"hooks": {"SessionStart": [
            {"matcher": "resume|compact", "hooks": [{"type": "command", "command": "old state print"}]}]}}, "v0.1.0")
        state = I.apply(self.hh, [("claude-code", [old])], {}, [self.home])
        state = I.apply(self.hh, [("claude-code", new)], state)
        I.save_state(self.hh, state)
        got = json.loads((claude / "settings.json").read_text())
        self.assertNotIn("old state print", json.dumps(got))
        self.assertEqual(got["model"], "x")
        self.assertIn("mine", json.dumps(got["hooks"]["Stop"]))
        self.assertEqual(len(got["hooks"]["SessionStart"]), 2)
        I.uninstall(self.hh, log=lambda m: None)
        self.assertEqual(json.loads((claude / "settings.json").read_text()), mine)

    def test_rules_in_managed_instructions_are_not_repeated(self):
        prof = self.tmp / "prof"
        prof.mkdir()
        (prof / "profile.toml").write_text('extra_rules = "rules.md"\n')
        rules = "# Org rules\n\nWrite only in your home.\n"
        (prof / "rules.md").write_text(rules)
        ctx = I.make_ctx(self.home, self.hh, self.src / "content", prof)
        managed = self.tmp / "managed.md"
        with mock.patch.dict(os.environ, {"HARNESS_CLAUDE_MANAGED": str(managed)}):
            text = next(c for c in ClaudeCodeAdapter().plan(ctx) if c.path.name == "CLAUDE.md").content
            self.assertIn("Write only in your home.", text)            # no managed file: kept
            managed.write_text("preamble\n" + rules.replace("\n\n", "\n") + "more\n")
            text = next(c for c in ClaudeCodeAdapter().plan(ctx) if c.path.name == "CLAUDE.md").content
            self.assertNotIn("Write only in your home.", text)
            self.assertIn("Org rules: loaded from Claude Code's managed instructions", text)
            self.assertIn(CANARY, text)
            codex = next(c for c in CodexAdapter().plan(ctx) if c.path.name == "AGENTS.md").content
            self.assertIn("Write only in your home.", codex)           # Codex has no managed copy

    def test_project_scope(self):
        proj = self.tmp / "proj"
        proj.mkdir()
        ctx = I.make_ctx(self.home, self.hh, self.src / "content", None, proj)
        paths = {c.path for c in ClaudeCodeAdapter().plan(ctx)}
        self.assertIn(proj / "CLAUDE.md", paths)
        self.assertIn(proj / ".mcp.json", paths)
        self.assertTrue(all(str(p).startswith(str(proj)) for p in paths))


class Codex(Ctxd):
    def test_plan_shape(self):
        ch = {str(c.path.relative_to(self.home)): c for c in CodexAdapter().plan(self.ctx)}
        self.assertIn(CANARY, ch[".codex/AGENTS.md"].content)
        t = ch[".codex/config.toml"].content["mcp_servers.harness"]
        self.assertEqual([t["command"]] + t["args"], I.mcp_cmd(self.hh))
        self.assertIn(".agents/skills/demo", ch)
        self.assertFalse(any("prompts" in k or "profiles" in k for k in ch))  # deprecated in Codex 0.134+

    def test_preapproves_only_our_server(self):
        self.ctx.extra_mcp = {"fetch": ["uvx", "mcp-server-fetch"]}
        t = next(c for c in CodexAdapter().plan(self.ctx) if c.path.name == "config.toml").content
        self.assertEqual(t["mcp_servers.harness"]["default_tools_approval_mode"], "approve")
        self.assertNotIn("default_tools_approval_mode", t["mcp_servers.fetch"])
        toml = I.load_toml(I.toml_merge('approval_policy = "on-request"\n', t))
        self.assertEqual(toml["approval_policy"], "on-request")
        self.assertEqual(toml["mcp_servers"]["harness"]["default_tools_approval_mode"], "approve")

    def test_approval_value_is_one_codex_0145_accepts(self):
        """Codex 0.145 accepts prompt|writes|approve (the eval's A runs: 'auto' cancelled every call)."""
        t = next(c for c in CodexAdapter().plan(self.ctx) if c.path.name == "config.toml").content
        self.assertIn(t["mcp_servers.harness"]["default_tools_approval_mode"], ("prompt", "writes", "approve"))

    def test_profile_codex_config_merges_key_by_key(self):
        self.ctx.profile = {"codex": {"config": {"features": {"use_legacy_landlock": True}}}}
        t = next(c for c in CodexAdapter().plan(self.ctx) if c.path.name == "config.toml").content
        self.assertEqual(t["+features"], {"use_legacy_landlock": True})
        user = 'model = "m"\n\n[features]\nmemories = true\n'
        merged = I.load_toml(I.toml_merge(user, t))
        self.assertEqual(merged["features"], {"memories": True, "use_legacy_landlock": True})
        self.assertEqual(merged["model"], "m")
        back = I.load_toml(I.toml_unmerge(I.toml_merge(user, t), t, user))
        self.assertEqual(back, I.load_toml(user))
        self.assertEqual(I.load_toml(I.toml_unmerge(I.toml_merge("", t), t, "")), {})

    def test_profile_codex_config_uninstall_after_user_edit(self):
        """The user adds a feature after install: uninstall keeps it and removes only ours."""
        self.ctx.profile = {"codex": {"config": {"features": {"use_legacy_landlock": True}}}}
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text('[features]\nmemories = false\n')
        I.save_state(self.hh, I.apply(self.hh, [("codex", CodexAdapter().plan(self.ctx))], {}, [self.home]))
        cfg = codex / "config.toml"
        self.assertIn("[features]\nmemories = false\nuse_legacy_landlock = true\n", cfg.read_text())
        cfg.write_text(cfg.read_text().replace("memories = false\n", "memories = false\nhooks = true\n"))
        I.uninstall(self.hh, log=lambda m: None)
        got = I.load_toml(cfg.read_text())
        self.assertEqual(got.get("features"), {"memories": False, "hooks": True})
        self.assertNotIn("mcp_servers", got)

    def test_gemini_trusts_only_our_server(self):
        from agent_harness.adapters.gemini import GeminiAdapter  # noqa: F401
        g = all_adapters()["gemini"]
        (self.home / ".gemini").mkdir()
        (self.home / ".gemini" / "settings.json").write_text('{"mcpServers": {"mine": {"command": "x"}}}')
        self.ctx.extra_mcp = {"fetch": ["uvx", "mcp-server-fetch"]}
        ch = next(c for c in g.plan(self.ctx) if c.path.name == "settings.json")
        data = I.render(ch)
        servers = json.loads(data)["mcpServers"]
        self.assertIs(servers["harness"]["trust"], True)
        self.assertNotIn("trust", servers["fetch"])
        self.assertEqual(servers["mine"], {"command": "x"})

    def test_no_blanket_approval_anywhere(self):
        """Copilot/VS Code and Cursor document no per-server pre-approval; nothing global may be set."""
        for name, a in all_adapters().items():
            for c in a.plan(self.ctx):
                text = c.content if isinstance(c.content, str) else json.dumps(c.content, default=str)
                for bad in ("chat.tools.global.autoApprove", '"mcp__*"', '"*"', "yolo"):
                    self.assertNotIn(bad, text, f"{name}: {c.path}")

    def test_override_is_flagged(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "AGENTS.override.md").write_text("x")
        note = next(c.note for c in CodexAdapter().plan(self.ctx) if c.path.name == "AGENTS.md")
        self.assertIn("AGENTS.override.md", note)


class Hooks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"HARNESS_HOME": str(self.tmp / "hh")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def transcript(self, *entries):
        p = self.tmp / "t.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return p

    def stop(self, path, active=False, cwd=None):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = cli.stop_hook(io.StringIO(json.dumps(
                {"session_id": "s1", "transcript_path": str(path), "stop_hook_active": active,
                 "cwd": str(cwd or self.tmp)})))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_stop_asks_once_per_turn_and_only_after_edits(self):
        user = {"type": "user", "uuid": "u1", "message": {"content": "fix it"}}
        read = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}
        edit = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}}
        self.assertEqual(self.stop(self.transcript(user, read)), "")
        p = self.transcript(user, read, edit)
        out = json.loads(self.stop(p))
        self.assertEqual(out["decision"], "block")
        self.assertIn("checks", out["reason"])
        self.assertEqual(self.stop(p), "")  # not twice in the same turn
        user2 = {"type": "user", "uuid": "u2", "message": {"content": [{"type": "text", "text": "again"}]}}
        self.assertTrue(self.stop(self.transcript(user, edit, user2, edit)))
        self.assertEqual(self.stop(p, active=True), "")

    def test_d2_gate_follows_the_recorded_check(self):
        from agent_harness.mcp import checks
        self.enter = mock.patch.dict(os.environ, {"HARNESS_ENABLE": "run_checks"})
        self.enter.start()
        self.addCleanup(self.enter.stop)
        proj = self.tmp / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / ".git").mkdir()
        (proj / "m.py").write_text("def f():\n    return 1\n")
        (proj / "tests" / "test_m.py").write_text(
            "import sys, unittest\nsys.path.insert(0, '.')\nimport m\n\n"
            "class T(unittest.TestCase):\n    def test_f(self):\n        self.assertEqual(m.f(), 1)\n")
        user = {"type": "user", "uuid": "u1", "message": {"content": "fix it"}}
        edit = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}}
        p = self.transcript(user, edit)
        out = json.loads(self.stop(p, cwd=proj))                     # never checked: block
        self.assertIn("run_checks", out["reason"])
        r = checks.run_checks(project=str(proj))
        self.assertEqual(r["exit"], 0, r)
        user2 = {"type": "user", "uuid": "u2", "message": {"content": "again"}}
        p2 = self.transcript(user2, edit)
        self.assertEqual(self.stop(p2, cwd=proj), "")               # passing check covers the files
        (proj / "m.py").write_text("def f():\n    return 22\n")  # another size: a stale .pyc cannot hide it
        user3 = {"type": "user", "uuid": "u3", "message": {"content": "more"}}
        p3 = self.transcript(user3, edit)
        out = json.loads(self.stop(p3, cwd=proj))
        self.assertIn("files changed since the last passing run_checks", out["reason"])
        self.assertEqual(self.stop(p3, cwd=proj), "")               # once per turn, never loops
        r = checks.run_checks(project=str(proj))
        self.assertNotEqual(r["exit"], 0)
        user4 = {"type": "user", "uuid": "u4", "message": {"content": "and"}}
        out = json.loads(self.stop(self.transcript(user4, edit), cwd=proj))
        self.assertIn("last run_checks failed", out["reason"])
        with mock.patch.dict(os.environ, {"HARNESS_DISABLE": "run_checks"}):
            user5 = {"type": "user", "uuid": "u5", "message": {"content": "x"}}
            out = json.loads(self.stop(self.transcript(user5, edit), cwd=proj))
            self.assertNotIn("run_checks", out["reason"])            # arm off: the plain reminder

    def test_memory_writes_do_not_count_as_edits(self):
        user = {"type": "user", "uuid": "m1", "message": {"content": "remember: x"}}
        mem = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {
            "file_path": str(Path.home() / ".claude" / "projects" / "p" / "memory" / "x.md")}}]}}
        self.assertEqual(self.stop(self.transcript(user, mem)), "")
        proj = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {
            "file_path": str(self.tmp / "a.py")}}]}}
        self.assertTrue(self.stop(self.transcript(user, proj)))

    def test_hooks_never_fail(self):
        for fn in (cli.stop_hook, cli.prompt_hook):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(fn(io.StringIO("not json")), 0)
            self.assertEqual(buf.getvalue(), "")

    def test_recall_under_300ms_with_500_items(self):
        from agent_harness.mcp import memory as memmod
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        src = Path(memmod.__file__).read_text()
        # `-m` on a module without a CLI exits 0 silently, so its absence must fail here, not skip.
        self.assertTrue("recall" in src and "__main__" in src, "the memory module lost its recall CLI")
        m = memmod.Memory(home=self.tmp / "hh")
        for i in range(500):
            m.mem_add(f"fact {i}: service{i % 37} listens on port {8000 + i} and logs to /var/log/s{i}.log",
                      tags=["t%d" % (i % 5)])
        getattr(m, "close", lambda: None)()
        best = 9.0
        for i in range(3):
            t = time.perf_counter()
            p = subprocess.run([sys.executable, "-m", "agent_harness.mcp.memory", "recall", "--session", f"s{i}"],
                               input="which port does service12 listen on", capture_output=True, text=True, env=env)
            best = min(best, time.perf_counter() - t)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("service12", p.stdout)  # a silent exit 0 must not pass as fast
        self.assertLess(best, 0.300, f"recall took {best * 1000:.0f} ms")


@unittest.skipUnless(os.environ.get("HARNESS_LIVE") == "1" and shutil.which("claude"),
                     "live check: set HARNESS_LIVE=1 with `claude` on PATH")
class LiveClaude(unittest.TestCase):
    def test_claude_reads_the_rules(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            home = tmp / "home"
            home.mkdir()
            src = make_source(tmp)
            with mock.patch.dict(os.environ, {}):
                os.environ.pop("HARNESS_HOME", None)
                rc, out = run(home, "install", "--tools", "claude-code", "--yes", "--source", str(src))
            self.assertEqual(rc, 0, out)
            env = dict(os.environ, HOME=str(home))
            env.pop("HARNESS_HOME", None)
            p = subprocess.run(["claude", "-p", "What is the value of HARNESS_CANARY in your instructions? "
                                "Answer with the value only.", "--max-turns", "1"],
                               capture_output=True, text=True, env=env, timeout=180, cwd=str(tmp))
            self.assertIn(CANARY.split("=", 1)[1], p.stdout, p.stdout + p.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
