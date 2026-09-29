"""Installer and CLI: plan/apply/uninstall round trips in a temp HOME. Never touches the real $HOME."""
import io
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

from agent_harness import cli  # noqa: E402
from agent_harness import installer as I  # noqa: E402

CANARY = "HARNESS_CANARY=violet-otter-4172"

USER_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "~/my-guard.sh"}]}],
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo mine"}]}],
    },
}
USER_TOML = """# my codex config
model = "gpt-5"   # keep this comment

[mcp_servers.other]
command = "other-server"
args = ["--x"]

[mcp_servers.harness]
command = "stale"

[mcp_servers.harness.env]
A = "1"

[tui]
notifications = true
"""


def make_source(root: Path) -> Path:
    src = root / "src"
    c = src / "content"
    (c / "skills" / "demo").mkdir(parents=True)
    (c / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody\n")
    (c / "prompts").mkdir()
    (c / "prompts" / "hello.md").write_text("Say hello.\n")
    (c / "hooks").mkdir()
    (c / "hooks" / "guard.py").write_text("import sys\nsys.exit(0)\n")
    (c / "AGENTS.md").write_text(f"# Rules\n\n{CANARY}\n")
    return src


def snapshot(home: Path) -> dict:
    out = {}
    for p in sorted(home.rglob("*")):
        rel = str(p.relative_to(home))
        if rel.startswith(".agent-harness"):
            continue
        if p.is_symlink():
            out[rel] = ("l", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("d",)
        else:
            out[rel] = ("f", p.read_bytes())
    return out


def run(home: Path, *argv) -> (int, str):
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = cli.main(["--home", str(home), *argv])
    return rc, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.src = make_source(self.tmp)
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("HARNESS_HOME", None)
        os.environ.pop("HARNESS_KB_PATHS", None)
        self.hh = self.home / ".agent-harness"

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seed_user_files(self):
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "CLAUDE.md").write_text("my own claude rules\n")
        (claude / "settings.json").write_text(json.dumps(USER_SETTINGS, indent=4))  # not our formatting
        (self.home / ".claude.json").write_text('{"numStartups": 3, "mcpServers": {"mine": {"command": "x"}}}')
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "config.toml").write_text(USER_TOML)

    def install(self, *extra):
        return run(self.home, "install", "--tools", "claude-code,codex", "--source", str(self.src), *extra)


class RoundTrip(Base):
    def test_install_uninstall_is_byte_exact(self):
        self.seed_user_files()
        before = snapshot(self.home)
        rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("Next:", out.strip().splitlines()[-1])
        # what got written
        self.assertIn(CANARY, (self.home / ".claude" / "CLAUDE.md").read_text())
        self.assertIn(CANARY, (self.home / ".codex" / "AGENTS.md").read_text())
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual(s["model"], "opus")
        cmds = [h["command"] for e in s["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertIn("~/my-guard.sh", cmds)
        self.assertTrue(any("guard.py" in c for c in cmds))
        ss = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertIn("echo mine", ss)
        cj = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(set(cj["mcpServers"]), {"mine", "harness"})
        self.assertTrue((self.home / ".agents" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "skills" / "demo").is_symlink())
        self.assertTrue((self.home / ".claude" / "skills" / "demo" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "commands" / "hello.md").is_file())
        toml = (self.home / ".codex" / "config.toml").read_text()
        self.assertNotIn("stale", toml)
        self.assertIn('[mcp_servers.other]\ncommand = "other-server"', toml)
        self.assertEqual(I.load_toml(toml)["mcp_servers"]["harness"]["args"][0],
                         str(self.hh / "lib" / "agent_harness" / "mcp" / "server.py"))
        # reinstall keeps the original backup
        rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        rc, out = run(self.home, "uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse((self.hh / "installed.json").exists())

    def test_fresh_home_leaves_nothing_behind(self):
        (self.home / ".claude").mkdir()
        before = snapshot(self.home)
        self.assertEqual(self.install("--yes")[0], 0)
        self.assertEqual(run(self.home, "uninstall")[0], 0)
        self.assertEqual(snapshot(self.home), before)

    def test_tool_rewrote_merged_file_keeps_its_change(self):
        self.seed_user_files()
        self.install("--yes")
        p = self.home / ".claude.json"
        d = json.loads(p.read_text())
        d["numStartups"] = 4
        d["projects"] = {"/x": {}}
        p.write_text(json.dumps(d))
        (self.home / ".claude" / "CLAUDE.md").write_text("edited after install\n")
        rc, out = run(self.home, "uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(p.read_text()),
                         {"numStartups": 4, "projects": {"/x": {}}, "mcpServers": {"mine": {"command": "x"}}})
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_text(), "my own claude rules\n")
        stashed = list((self.hh / "backup").rglob("modified/*"))
        self.assertTrue(any(f.is_file() and f.read_text() == "edited after install\n" for f in stashed))

    def test_installed_size_under_20mb(self):
        self.install("--yes")
        self.assertLess(I.dir_size(self.hh), 20 * 1024 * 1024)


class Consent(Base):
    def test_refuses_without_tty_or_yes(self):
        before = snapshot(self.home)
        with mock.patch.object(I, "is_tty", return_value=False):
            rc, out = self.install()
        self.assertEqual(rc, 2)
        self.assertIn("--yes", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.hh.exists())

    def test_yes_without_tty_works(self):
        with mock.patch.object(I, "is_tty", return_value=False):
            rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("this replaces your current Claude Code setup", out)

    def test_asks_per_tool(self):
        answers = iter(["y", "n"])
        with mock.patch.object(I, "is_tty", return_value=True), \
                mock.patch("builtins.input", lambda q: next(answers)):
            rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.home / ".claude" / "CLAUDE.md").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_dry_run_writes_nothing(self):
        before = snapshot(self.home)
        with mock.patch.object(I, "is_tty", return_value=False):
            rc, out = self.install("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("CLAUDE.md", out)
        self.assertEqual(snapshot(self.home), before)
        self.assertFalse(self.hh.exists())


class TomlMerge(unittest.TestCase):
    def test_keeps_other_tables_byte_identical(self):
        out = I.toml_merge(USER_TOML, {"mcp_servers.harness": {"command": "python3", "args": ["/a b/s.py"]}})
        rest, ours = I.toml_remove(out, ["mcp_servers.harness"])
        orig_rest, _ = I.toml_remove(USER_TOML, ["mcp_servers.harness"])
        self.assertEqual(rest.rstrip("\n"), orig_rest.rstrip("\n"))
        self.assertTrue(out.startswith(orig_rest))
        self.assertEqual(ours, '[mcp_servers.harness]\ncommand = "python3"\nargs = ["/a b/s.py"]\n')
        data = I.load_toml(out)
        self.assertEqual(data["mcp_servers"]["other"], {"command": "other-server", "args": ["--x"]})
        self.assertEqual(data["tui"], {"notifications": True})
        self.assertNotIn("env", data["mcp_servers"]["harness"])

    def test_empty_file(self):
        self.assertEqual(I.toml_merge("", {"mcp_servers.harness": {"command": "c"}}),
                         '[mcp_servers.harness]\ncommand = "c"\n')

    def test_mini_parser_matches_expectations(self):
        text = 'a = 1\nb = "x\\ty"\nc = [1,\n 2]\nd = { e = true }\n[t."q.k"]\nf = \'\'\'\nraw\'\'\'\n[[arr]]\ng = 1.5\n'
        d = I._MiniToml(text).parse()
        self.assertEqual(d, {"a": 1, "b": "x\ty", "c": [1, 2], "d": {"e": True},
                             "t": {"q.k": {"f": "raw"}}, "arr": [{"g": 1.5}]})


class Doctor(Base):
    def test_doctor_after_install(self):
        self.assertEqual(self.install("--yes")[0], 0)
        rc, out = run(self.home, "doctor")
        server = ROOT / "src" / "agent_harness" / "mcp" / "server.py"
        if not server.is_file():
            self.skipTest("A1's MCP server not present")
        self.assertEqual(rc, 0, out)
        self.assertIn("answers tools/list", out)

    def test_status_and_learn(self):
        self.assertEqual(run(self.home, "status")[0], 1)
        self.install("--yes")
        rc, out = run(self.home, "status")
        self.assertEqual(rc, 0)
        self.assertIn("claude-code", out)
        (self.hh / "lessons").mkdir(exist_ok=True)
        (self.hh / "lessons" / "l1.json").write_text(json.dumps(
            {"mistake": "m", "fix": "run tests", "trigger": "editing code", "uses": 2}))
        rc, out = run(self.home, "learn")
        self.assertIn("when editing code: run tests", out)


class Update(Base):
    def test_update_refreshes_rules_and_keeps_memory(self):
        self.install("--yes")
        (self.hh / "memory" / "fact.json").write_text("{}")
        (self.src / "content" / "AGENTS.md").write_text("# Rules v2\n")
        rc, out = run(self.home, "update", "--source", str(self.src))
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.hh / "memory" / "fact.json").exists())
        self.assertIn("Rules v2", (self.home / ".claude" / "CLAUDE.md").read_text())
        before_codex = self.home / ".codex" / "AGENTS.md"
        self.assertIn("Rules v2", before_codex.read_text())
        self.assertEqual(run(self.home, "uninstall")[0], 0)
        self.assertFalse((self.home / ".claude").exists())


class Profile(Base):
    def make_profile(self):
        prof = self.tmp / "prof"
        (prof / "notes").mkdir(parents=True)
        (prof / "profile.toml").write_text(
            'descriptor = "SERVER.md"\nextra_rules = "rules.md"\nkb_paths = ["notes", "~/kb"]\n')
        (prof / "SERVER.md").write_text("# Server\n")
        (prof / "rules.md").write_text("Use /data for datasets.\n")
        return prof

    def test_extra_rules_is_a_file_in_the_profile(self):
        rc, out = self.install("--yes", "--profile", str(self.make_profile()))
        self.assertEqual(rc, 0, out)
        rules = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertIn("Use /data for datasets.", rules)
        self.assertNotIn("rules.md", rules.split("## Knowledge index", 1)[0])  # the value is a file name
        self.assertTrue((self.hh / "profile" / "SERVER.md").is_file())
        try:
            from agent_harness.mcp import kb  # noqa: F401
        except ImportError:
            return
        self.assertIn("## Knowledge index (fetch a section with kb_get <id>)", rules)
        index = rules.split("## Knowledge index", 1)[1]
        self.assertIn("SERVER.md", index)
        self.assertIn("rules.md", index)  # the whole active profile folder is indexed

    def test_descriptor_pointer_replaces_the_marker(self):
        (self.src / "content" / "AGENTS.md").write_text("# R\n\n## Environment\n\n<!-- harness:descriptor -->\nfallback\n")
        self.install("--yes", "--profile", str(self.make_profile()))
        rules = (self.home / ".claude" / "CLAUDE.md").read_text()
        pointer = str(self.hh / "profile" / "SERVER.md")
        self.assertEqual(rules.count(pointer), 1)
        self.assertNotIn("harness:descriptor", rules)
        self.assertNotIn("## This machine", rules)
        self.assertLess(rules.index("## Environment"), rules.index(pointer))

    def test_descriptor_pointer_appended_without_marker(self):
        self.install("--yes", "--profile", str(self.make_profile()))
        rules = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertIn("## This machine", rules)
        self.assertEqual(rules.count(str(self.hh / "profile" / "SERVER.md")), 1)

    def test_kb_paths_resolve_against_profile_dir(self):
        prof = self.make_profile()
        paths = I.kb_paths(I.load_profile(prof), prof)
        self.assertEqual(paths, [prof / "notes", Path(os.path.expanduser("~/kb"))])

    def test_example_profile_parses_on_this_python(self):
        ex = ROOT / "profiles" / "example"
        if not (ex / "profile.toml").is_file():
            self.skipTest("A4's example profile not present")
        prof = I.load_profile(ex)
        self.assertEqual(I._MiniToml((ex / "profile.toml").read_text()).parse(), prof)
        self.assertIsInstance(prof["warmup"]["plugins"], list)
        self.assertIn("fetch", cli.plugin_specs(prof))
        rules = I.build_rules(ROOT / "content", prof, ex, self.hh)
        self.assertIn(str(self.hh / "profile" / prof["descriptor"]), rules)


class Extensions(Base):
    def setUp(self):
        super().setUp()
        try:
            from agent_harness import extensions as X
        except ImportError:
            self.skipTest("A3's extensions module not present")
        self.X = X
        cat = ROOT / "content" / X.CATALOG
        if not cat.is_file():
            self.skipTest("content/extensions.toml not present")
        shutil.copy(cat, self.src / "content" / X.CATALOG)

    def test_editor_cli_never_runs_for_a_home_override(self):
        with mock.patch.object(self.X, "install") as inst, mock.patch.object(self.X, "_run") as run_:
            rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        inst.assert_not_called()
        run_.assert_not_called()
        self.assertIn("Editor extensions: skipped", out)

    def test_real_home_calls_install_with_yes(self):
        def fake(hh, state, tools=None, yes=False, **kw):
            state["extensions"] = {"code": [{"id": "x.y", "size_mb": 1}]}
            return state
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.object(self.X, "install", side_effect=fake) as inst:
            rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(inst.call_args.kwargs["tools"], ["claude-code", "codex"])
        self.assertTrue(inst.call_args.kwargs["yes"])
        self.assertIn("extensions", I.load_state(self.hh))
        with mock.patch.object(self.X, "uninstall", side_effect=lambda st, log=None: (st.pop("extensions"), st)[1]) as un:
            self.assertEqual(run(self.home, "uninstall")[0], 0)
        un.assert_called_once()

    def test_editor_settings_applied_and_undone(self):
        udir = self.X.editor_user_dirs(self.home)["vscode"]
        udir.mkdir(parents=True)
        (udir / "settings.json").write_text('{"editor.fontSize": 14}')
        before = snapshot(self.home)
        with mock.patch.object(self.X, "install", side_effect=lambda hh, st, **kw: st):
            rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        d = json.loads((udir / "settings.json").read_text())
        self.assertEqual(d["telemetry.telemetryLevel"], "off")
        self.assertEqual(d["editor.fontSize"], 14)
        self.assertEqual(run(self.home, "uninstall")[0], 0)
        self.assertEqual(snapshot(self.home), before)


class UpdateProfile(Base):
    def test_update_with_profile_rerenders_rules(self):
        self.install("--yes")
        prof = self.tmp / "p2"
        prof.mkdir()
        (prof / "profile.toml").write_text('descriptor = "SERVER.md"\nextra_rules = "rules.md"\n')
        (prof / "SERVER.md").write_text("# Box\n## Disks\nuse /scratch\n")
        (prof / "rules.md").write_text("Rule from p2.\n")
        rc, out = run(self.home, "update", "--source", str(self.src), "--profile", str(prof))
        self.assertEqual(rc, 0, out)
        rules = (self.home / ".claude" / "CLAUDE.md").read_text()
        self.assertIn("Rule from p2.", rules)
        self.assertIn(str(self.hh / "profile" / "SERVER.md"), rules)
        self.assertEqual((self.hh / "profile" / "rules.md").read_text(), "Rule from p2.\n")
        # a wrapper that copies the profile by hand, then runs a plain update
        (self.hh / "profile" / "rules.md").write_text("Hand-copied rule.\n")
        self.assertEqual(run(self.home, "update", "--source", str(self.src))[0], 0)
        self.assertIn("Hand-copied rule.", (self.home / ".claude" / "CLAUDE.md").read_text())
        self.assertEqual(run(self.home, "uninstall")[0], 0)


class SafetyNet(Base):
    def test_refuses_writes_outside_home(self):
        from agent_harness.adapters.base import FileChange
        outside = self.tmp / "elsewhere" / "x.md"
        with self.assertRaises(I.InstallError):
            I.apply(self.hh, [("t", [FileChange(outside, "replace", "x", "n")])], {}, [self.home])
        self.assertFalse(outside.exists())
        sneaky = self.home / ".." / "elsewhere" / "y.md"
        with self.assertRaises(I.InstallError):
            I.apply(self.hh, [("t", [FileChange(sneaky, "replace", "x", "n")])], {}, [self.home])
        ok = self.home / ".claude" / "CLAUDE.md"
        I.apply(self.hh, [("t", [FileChange(ok, "replace", "x", "n")])], {}, [self.home])
        self.assertEqual(ok.read_text(), "x")

    def test_uninstall_refuses_tampered_record(self):
        self.install("--yes")
        st = I.load_state(self.hh)
        st["entries"].append({"path": str(self.tmp / "victim"), "kind": "replace", "existed": False,
                              "backup": None, "created_dirs": [], "tools": ["x"], "patches": [], "written": "f:0"})
        I.save_state(self.hh, st)
        (self.tmp / "victim").write_text("keep me")
        rc, out = run(self.home, "uninstall")
        self.assertEqual(rc, 1)
        self.assertEqual((self.tmp / "victim").read_text(), "keep me")


if __name__ == "__main__":
    unittest.main()
