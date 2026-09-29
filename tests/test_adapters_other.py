"""Adapters for Copilot (VS Code), Cursor, Gemini CLI, AGENTS.md, Claude desktop, Jupyter AI.

Every test runs in a temp HOME; nothing here touches the real one.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_harness.adapters.base import KINDS, Ctx  # noqa: E402
from agent_harness.adapters.agents_md import AgentsMdAdapter  # noqa: E402
from agent_harness.adapters.claude_desktop import ClaudeDesktopAdapter  # noqa: E402
from agent_harness.adapters.copilot_vscode import CopilotVSCodeAdapter, strip_jsonc  # noqa: E402
from agent_harness.adapters.cursor import CursorAdapter  # noqa: E402
from agent_harness.adapters.gemini import GeminiAdapter  # noqa: E402
from agent_harness.adapters.jupyter_ai import JupyterAIAdapter  # noqa: E402

RULES = "# Rules\n\nBe careful.\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        hh = self.home / ".agent-harness"
        self.content = hh / "content"
        (self.content / "prompts").mkdir(parents=True)
        (self.content / "prompts" / "review.md").write_text("Review the diff.\n")
        self.cmd = ["python3", str(hh / "lib" / "agent_harness" / "mcp" / "server.py")]
        # detect() must only see our empty PATH, and platform is pinned per test.
        p1 = mock.patch.dict(os.environ, {"PATH": str(self.bin)})
        p2 = mock.patch.object(sys, "platform", "linux")
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        self.addCleanup(self._tmp.cleanup)

    def ctx(self, project=None):
        return Ctx(home=self.home, harness_home=self.home / ".agent-harness", content=self.content,
                   profile={}, rules=RULES, mcp_cmd=self.cmd,
                   scope="project" if project else "user", project=project)

    def by_path(self, changes):
        for c in changes:
            self.assertIn(c.kind, KINDS)
            self.assertTrue(c.path.is_absolute())
            self.assertTrue(str(c.path).startswith(str(self.root)), c.path)
            self.assertTrue(c.note)
        return {c.path: c for c in changes}

    def as_dict(self, change):
        return json.loads(change.content) if change.kind == "replace" else change.content

    def fake_tool(self, name):
        p = self.bin / name
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)


class TestJsonc(unittest.TestCase):
    def test_comments_and_trailing_commas(self):
        text = ('{\n // line\n "a": "http://x/*y*/", /* block */\n "b": [1, 2,],\n'
                ' "c": "q\\"//",\n}\n')
        self.assertEqual(json.loads(strip_jsonc(text)), {"a": "http://x/*y*/", "b": [1, 2], "c": 'q"//'})


class TestCopilot(Base):
    def test_plan_linux_user(self):
        a = CopilotVSCodeAdapter()
        ch = self.by_path(a.plan(self.ctx()))
        user = self.home / ".config" / "Code" / "User"
        rules = ch[self.home / ".copilot" / "instructions" / "harness.instructions.md"]
        self.assertEqual(rules.kind, "replace")
        self.assertTrue(rules.content.startswith('---\nname: harness\n'))
        self.assertIn('applyTo: "**"', rules.content)
        self.assertIn(RULES, rules.content)
        prompt = ch[self.home / ".agent-harness" / "content" / "copilot" / "prompts" / "review.prompt.md"]
        self.assertEqual(prompt.content, "Review the diff.\n")

        settings = ch[user / "settings.json"]
        self.assertEqual(settings.kind, "merge-json")
        s = settings.content
        for key in ("github.copilot.chat.codeGeneration.useInstructionFiles", "chat.useAgentsMdFile",
                    "chat.includeApplyingInstructions", "chat.promptFiles"):
            self.assertIs(s[key], True, key)
        self.assertTrue(s["chat.instructionsFilesLocations"]["~/.copilot/instructions"])
        self.assertTrue(s["chat.promptFilesLocations"]["~/.agent-harness/content/copilot/prompts"])

        # VS Code format: servers + type stdio. Portable format: mcpServers.
        self.assertEqual(ch[user / "mcp.json"].content,
                         {"servers": {"harness": {"type": "stdio", "command": "python3",
                                                  "args": self.cmd[1:]}}})
        portable = ch[self.home / ".copilot" / "mcp-config.json"].content
        self.assertEqual(list(portable), ["mcpServers"])
        self.assertEqual(portable["mcpServers"]["harness"]["command"], "python3")
        self.assertNotIn(self.home / ".github" / "copilot-instructions.md", ch)
        self.assertFalse(any("vscode-server" in str(p) for p in ch))

    def test_plan_macos_and_remote_ssh_and_project(self):
        (self.home / ".vscode-server").mkdir()
        proj = self.root / "proj"
        proj.mkdir()
        with mock.patch.object(sys, "platform", "darwin"):
            ch = self.by_path(CopilotVSCodeAdapter().plan(self.ctx(project=proj)))
        mac = self.home / "Library" / "Application Support" / "Code" / "User"
        machine = self.home / ".vscode-server" / "data" / "Machine"
        for p in (mac / "settings.json", mac / "mcp.json", machine / "settings.json", machine / "mcp.json"):
            self.assertIn(p, ch)
        self.assertIn("servers", ch[machine / "mcp.json"].content)
        self.assertEqual(ch[proj / ".github" / "copilot-instructions.md"].content, RULES)

    def test_jsonc_settings_merged_keeping_user_keys(self):
        user = self.home / ".config" / "Code" / "User"
        user.mkdir(parents=True)
        (user / "settings.json").write_text(
            '// my settings\n{\n  "editor.fontSize": 14, // big\n'
            '  "http.proxy": "http://proxy.invalid:8080",\n'
            '  /* keep mine */\n  "chat.instructionsFilesLocations": {"~/mine": true,},\n}\n')
        (user / "mcp.json").write_text('{"servers": {"other": {"type": "stdio", "command": "x"}}, '
                                       '"inputs": []}')
        ch = self.by_path(CopilotVSCodeAdapter().plan(self.ctx()))
        s = ch[user / "settings.json"]
        self.assertEqual(s.kind, "replace")
        self.assertIn("comments dropped", s.note)
        data = json.loads(s.content)
        self.assertEqual(data["editor.fontSize"], 14)
        self.assertEqual(data["http.proxy"], "http://proxy.invalid:8080")
        self.assertTrue(data["chat.instructionsFilesLocations"]["~/mine"])
        self.assertTrue(data["chat.instructionsFilesLocations"]["~/.copilot/instructions"])
        self.assertTrue(data["chat.useAgentsMdFile"])
        m = ch[user / "mcp.json"]
        self.assertEqual(m.kind, "merge-json")
        self.assertEqual(set(m.content["servers"]), {"other", "harness"})
        self.assertNotIn("inputs", m.content)  # untouched top-level keys are left to the merge

    def test_unparseable_settings_refused_not_overwritten(self):
        user = self.home / ".config" / "Code" / "User"
        user.mkdir(parents=True)
        bad = '{ "editor.fontSize": 14, "oops" }'
        (user / "settings.json").write_text(bad)
        a = CopilotVSCodeAdapter()
        ch = self.by_path(a.plan(self.ctx()))
        self.assertNotIn(user / "settings.json", ch)
        self.assertIn(user / "mcp.json", ch)
        notes = a.notes(self.ctx())
        self.assertTrue(any(n.startswith("refused") and "settings.json" in n for n in notes), notes)
        self.assertEqual((user / "settings.json").read_text(), bad)

    def test_non_object_json_refused(self):
        user = self.home / ".config" / "Code" / "User"
        user.mkdir(parents=True)
        (user / "mcp.json").write_text("[1, 2]")
        self.assertNotIn(user / "mcp.json", self.by_path(CopilotVSCodeAdapter().plan(self.ctx())))

    def test_detect(self):
        a = CopilotVSCodeAdapter()
        self.assertFalse(a.detect(self.ctx()))
        (self.home / ".config" / "Code").mkdir(parents=True)
        self.assertTrue(a.detect(self.ctx()))

    def test_detect_by_path_and_remote(self):
        a = CopilotVSCodeAdapter()
        self.fake_tool("code")
        self.assertTrue(a.detect(self.ctx()))
        (self.bin / "code").unlink()
        (self.home / ".vscode-server").mkdir()
        self.assertTrue(a.detect(self.ctx()))


class TestCursor(Base):
    def test_plan_user(self):
        a = CursorAdapter()
        ch = self.by_path(a.plan(self.ctx()))
        self.assertEqual(list(ch), [self.home / ".cursor" / "mcp.json"])
        self.assertEqual(ch[self.home / ".cursor" / "mcp.json"].content,
                         {"mcpServers": {"harness": {"type": "stdio", "command": "python3",
                                                     "args": self.cmd[1:]}}})
        self.assertTrue(any("Customize" in n and "AGENTS.md" in n for n in a.notes(self.ctx())))

    def test_plan_project_rule_and_keeps_servers(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text('{"mcpServers": {"gh": {"command": "gh"}}}')
        proj = self.root / "proj"
        proj.mkdir()
        ch = self.by_path(CursorAdapter().plan(self.ctx(project=proj)))
        self.assertEqual(set(ch[self.home / ".cursor" / "mcp.json"].content["mcpServers"]), {"gh", "harness"})
        rule = ch[proj / ".cursor" / "rules" / "harness.mdc"]
        self.assertTrue(rule.content.startswith("---\n"))
        self.assertIn("alwaysApply: true", rule.content)
        self.assertTrue(rule.content.endswith(RULES))

    def test_detect(self):
        a = CursorAdapter()
        self.assertFalse(a.detect(self.ctx()))
        self.fake_tool("cursor")
        self.assertTrue(a.detect(self.ctx()))
        (self.bin / "cursor").unlink()
        (self.home / ".cursor").mkdir()
        self.assertTrue(a.detect(self.ctx()))


class TestGemini(Base):
    def test_plan_fresh(self):
        ch = self.by_path(GeminiAdapter().plan(self.ctx()))
        self.assertEqual(ch[self.home / ".gemini" / "GEMINI.md"].content, RULES)
        s = ch[self.home / ".gemini" / "settings.json"].content
        self.assertEqual(s["mcpServers"]["harness"], {"command": "python3", "args": self.cmd[1:], "trust": True})
        self.assertEqual(s["context"]["fileName"], ["AGENTS.md", "GEMINI.md"])

    def test_merge_keeps_user_values(self):
        g = self.home / ".gemini"
        g.mkdir()
        (g / "settings.json").write_text(json.dumps({
            "theme": "dark", "mcpServers": {"x": {"command": "x"}},
            "context": {"fileName": "CONTEXT.md", "includeDirectories": ["/d"]}}))
        s = self.by_path(GeminiAdapter().plan(self.ctx()))[g / "settings.json"].content
        self.assertEqual(set(s["mcpServers"]), {"x", "harness"})
        self.assertEqual(s["context"], {"fileName": ["CONTEXT.md", "AGENTS.md", "GEMINI.md"],
                                        "includeDirectories": ["/d"]})

    def test_detect(self):
        a = GeminiAdapter()
        self.assertFalse(a.detect(self.ctx()))
        self.fake_tool("gemini")
        self.assertTrue(a.detect(self.ctx()))


class TestAgentsMd(Base):
    def test_user_scope_writes_nothing(self):
        a = AgentsMdAdapter()
        self.assertEqual(a.plan(self.ctx()), [])
        self.assertTrue(any("per-project" in n for n in a.notes(self.ctx())))

    def test_project_scope(self):
        proj = self.root / "proj"
        proj.mkdir()
        ch = self.by_path(AgentsMdAdapter().plan(self.ctx(project=proj)))
        c = ch[proj / "AGENTS.md"]
        self.assertEqual((c.kind, c.content), ("replace", RULES))

    def test_detect(self):
        self.assertTrue(AgentsMdAdapter().detect(self.ctx()))


class TestClaudeDesktop(Base):
    def test_macos_plan_keeps_servers(self):
        d = self.home / "Library" / "Application Support" / "Claude"
        d.mkdir(parents=True)
        (d / "claude_desktop_config.json").write_text(
            '{"mcpServers": {"fs": {"command": "npx"}}, "globalShortcut": ""}')
        a = ClaudeDesktopAdapter()
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertTrue(a.detect(self.ctx()))
            ch = self.by_path(a.plan(self.ctx()))
            notes = a.notes(self.ctx())
        c = ch[d / "claude_desktop_config.json"]
        self.assertEqual(c.content, {"mcpServers": {"fs": {"command": "npx"},
                                                    "harness": {"command": "python3",
                                                                "args": self.cmd[1:]}}})
        self.assertTrue(any("no global instructions" in n for n in notes))

    def test_windows_path(self):
        with mock.patch.object(sys, "platform", "win32"):
            ch = self.by_path(ClaudeDesktopAdapter().plan(self.ctx()))
        self.assertIn(self.home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json", ch)

    def test_linux_without_app(self):
        a = ClaudeDesktopAdapter()
        self.assertFalse(a.detect(self.ctx()))
        self.assertEqual(a.plan(self.ctx()), [])


class TestJupyterAI(Base):
    def test_plan_replaces_own_entry_keeps_others(self):
        j = self.home / ".jupyter"
        j.mkdir()
        (j / "mcp_settings.json").write_text(json.dumps({"mcp_servers": [
            {"name": "harness", "command": "old"}, {"name": "fs", "command": "npx"}]}))
        a = JupyterAIAdapter()
        self.assertTrue(a.detect(self.ctx()))
        c = self.by_path(a.plan(self.ctx()))[j / "mcp_settings.json"]
        self.assertEqual(c.content, {"mcp_servers": [
            {"name": "fs", "command": "npx"},
            {"name": "harness", "command": "python3", "args": self.cmd[1:]}]})

    def test_project_and_detect(self):
        a = JupyterAIAdapter()
        self.assertFalse(a.detect(self.ctx()))
        proj = self.root / "proj"
        proj.mkdir()
        self.assertIn(proj / ".jupyter" / "mcp_settings.json", self.by_path(a.plan(self.ctx(project=proj))))
        self.fake_tool("jupyter")
        self.assertTrue(a.detect(self.ctx()))


class TestRegistryNames(unittest.TestCase):
    def test_names_unique(self):
        names = [c.name for c in (CopilotVSCodeAdapter, CursorAdapter, GeminiAdapter, AgentsMdAdapter,
                                  ClaudeDesktopAdapter, JupyterAIAdapter)]
        self.assertEqual(len(set(names)), len(names))


if __name__ == "__main__":
    unittest.main()
