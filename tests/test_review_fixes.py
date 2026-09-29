"""Regression tests for the v0.1.1 review: shared files between tools, backups with comments, shared TOML
tables, profile tables naming ours, check_guard paths, the run_checks fingerprint outside a project."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "content" / "hooks"))

from agent_harness import installer as I  # noqa: E402
from agent_harness.adapters.base import FileChange  # noqa: E402
from agent_harness.adapters.codex import CodexAdapter  # noqa: E402
from agent_harness.mcp import checks  # noqa: E402
import check_guard as cg  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.hh = self.tmp / "hh"
        self.home.mkdir()
        self.hh.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class SharedFiles(Tmp):
    def test_two_tools_in_one_file_keep_each_others_keys(self):
        s = self.home / "settings.json"
        s.write_text(json.dumps({"editor.fontSize": 14}))
        pc, pe = {"chat.useAgentsMdFile": True}, {"telemetry.telemetryLevel": "off"}
        st = I.apply(self.hh, [("copilot-vscode", [FileChange(s, "merge-json", pc, "")])], {}, [self.home])
        st = I.apply(self.hh, [("editor-settings", [FileChange(s, "merge-json", pe, "")])], st)
        st = I.apply(self.hh, [("copilot-vscode", [FileChange(s, "merge-json", pc, "")])], st)   # an update
        self.assertEqual(json.loads(s.read_text()), {"editor.fontSize": 14, **pc, **pe})
        I.save_state(self.hh, st)
        I.uninstall(self.hh, log=lambda m: None)
        self.assertEqual(json.loads(s.read_text()), {"editor.fontSize": 14})

    def test_update_after_a_backup_with_comments(self):
        s = self.home / "settings.json"
        s.write_text('{\n  // mine\n  "a": 1\n}\n')
        st = I.apply(self.hh, [("x", [FileChange(s, "replace", '{"a": 1, "b": 2}\n', "")])], {}, [self.home])
        st = I.apply(self.hh, [("x", [FileChange(s, "merge-json", {"b": 2}, "")])], st)          # must not raise
        st = I.apply(self.hh, [("x", [FileChange(s, "merge-json", {"b": 3}, "")])], st)
        self.assertEqual(json.loads(s.read_text()), {"a": 1, "b": 3})


class SharedToml(unittest.TestCase):
    USER = ('model = "o3"\n\n[shell_environment_policy]\n# keep my PATH\ninherit = "all"\n'
            'set = { PATH = "/opt/bin" }\n\n[shell_environment_policy.extra]\nx = 1\n')

    def test_only_our_keys_change_and_come_back(self):
        t = {"+shell_environment_policy": {"inherit": "core"}}
        out = I.toml_merge(self.USER, t)
        self.assertIn("# keep my PATH\ninherit = \"core\"\nset = { PATH = \"/opt/bin\" }", out)
        self.assertIn("[shell_environment_policy.extra]\nx = 1", out)
        self.assertEqual(I.toml_unmerge(out, t, self.USER), self.USER)
        self.assertEqual(I.toml_merge(out, t), out)                       # idempotent: no growth

    def test_a_table_without_its_header_is_refused(self):
        for user in ('features.web_search = true\n', 'features = { web_search = true }\n'):
            with self.assertRaises(I.InstallError):
                I.toml_merge(user, {"+features": {"use_legacy_landlock": True}})

    def test_created_table_goes_on_uninstall(self):
        t = {"+features": {"use_legacy_landlock": True}}
        out = I.toml_merge('model = "o3"\n', t)
        self.assertEqual(I.load_toml(I.toml_unmerge(out, t, 'model = "o3"\n')), {"model": "o3"})


class ProfileTables(Tmp):
    def test_a_profile_table_naming_ours_extends_it(self):
        src = self.tmp / "src"
        (src / "content").mkdir(parents=True)
        (src / "content" / "AGENTS.md").write_text("# R\n")
        ctx = I.make_ctx(self.home, self.hh, src / "content", None)
        ctx.profile = {"codex": {"config": {"mcp_servers": {"harness": {"startup_timeout_sec": 30}}}}}
        t = next(c for c in CodexAdapter().plan(ctx) if c.path.name == "config.toml").content
        self.assertEqual(t["mcp_servers.harness"]["startup_timeout_sec"], 30)
        self.assertIn("command", t["mcp_servers.harness"])
        self.assertNotIn("+mcp_servers.harness", t)


class GuardPaths(Tmp):
    def test_folders_named_test_above_the_project_do_not_count(self):
        proj = self.tmp / "test" / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / ".git").mkdir()
        f = proj / "src" / "app.py"
        f.write_text("def f(x):\n    assert x\n    return x\n")
        g = proj / "gateway.py"
        g.write_text("assert True\n")
        for path, old in ((f, "    assert x\n"), (g, "assert True\n")):
            self.assertIsNone(cg.decide({"tool_name": "Edit", "cwd": str(proj), "tool_input": {
                "file_path": str(path), "old_string": old, "new_string": ""}}))
        t = proj / "tests" / "test_a.py"
        t.parent.mkdir()
        t.write_text("assert 1\n")
        self.assertTrue(cg.decide({"tool_name": "Edit", "cwd": str(proj), "tool_input": {
            "file_path": str(t), "old_string": "assert 1\n", "new_string": ""}}))


class CheckRoots(Tmp):
    def test_no_fingerprint_outside_a_git_project_or_of_home(self):
        loose = self.tmp / "loose"
        loose.mkdir()
        self.assertEqual(checks.fingerprint(loose), "")
        self.assertEqual(checks.unchecked_changes(loose), (False, ""))
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            (self.home / ".git").mkdir()
            self.assertFalse(checks.gated(self.home))
        proj = self.tmp / "p"
        (proj / ".git").mkdir(parents=True)
        (proj / "a.py").write_text("x = 1\n")
        self.assertTrue(checks.fingerprint(proj))
        with mock.patch.object(checks, "MAX_FILES", 0):
            self.assertEqual(checks.fingerprint(proj), "")


if __name__ == "__main__":
    unittest.main()
