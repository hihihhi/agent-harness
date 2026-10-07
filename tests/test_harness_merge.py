"""One harness on every device (plan harness-merge): what makes a single install portable across accounts and
machines, and what keeps every device on the same release by itself."""
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_harness import cli  # noqa: E402


def run(home, *argv):
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf):
        rc = cli.main(["--home", str(home), *argv])
    return rc, buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="merge-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "HARNESS_HOME": str(self.home / ".agent-harness")})
        env.start()
        self.addCleanup(env.stop)

    def hh(self):
        return self.home / ".agent-harness"


class TestPortable(Base):
    """1: the settings a Claude Code install writes name no account: the same file works for every account."""

    def test_portable_hook_commands_name_no_home(self):
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code")[0], 0)
        s = (self.home / ".claude" / "settings.json").read_text()
        cmds = [h["command"] for groups in json.loads(s)["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertFalse([c for c in cmds if str(self.home) in c], "a hook command names this account's home")
        self.assertTrue(any('"${HARNESS_HOME:-$HOME/.agent-harness}"/' in c for c in cmds), cmds)

    def test_portable_hook_commands_run_from_any_home(self):
        """The written guard command, run by sh -c as Claude Code does, finds the harness through $HOME."""
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code")[0], 0)
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        guard = s["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        p = subprocess.run(["sh", "-c", guard], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                           capture_output=True, text=True, env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_portable_profile_can_keep_claude_codes_memory_folder(self):
        prof = self.tmp / "owner"
        prof.mkdir()
        (prof / "profile.toml").write_text('name = "owner"\nclaude_memory = "keep"\n')
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code", "--profile", str(prof))[0], 0)
        text = (self.home / ".claude" / "settings.json").read_text()
        self.assertNotIn("autoMemoryDirectory", json.loads(text))
        self.assertNotIn(str(self.home), text, "with the owner profile the whole settings.json names no account")

    def test_portable_launcher_runs_the_installed_copy(self):
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code")[0], 0)
        launcher = self.hh() / "bin" / "harness"
        self.assertNotIn(str(self.home), launcher.read_text())
        p = subprocess.run([str(launcher), "--help"], capture_output=True, text=True, env=dict(os.environ, HOME=str(self.home)))
        self.assertEqual(p.returncode, 0, p.stderr)


class TestAutoUpdate(Base):
    def test_self_update_hook_only_when_the_profile_asks(self):
        prof = self.tmp / "p"
        prof.mkdir()
        (prof / "profile.toml").write_text('name = "p"\nauto_update = true\n')
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code", "--profile", str(prof))[0], 0)
        cmds = [h["command"] for g in json.loads((self.home / ".claude" / "settings.json").read_text())["hooks"]
                ["SessionStart"] for h in g["hooks"]]
        hook = [c for c in cmds if "self-update" in c]
        self.assertEqual(len(hook), 1, cmds)
        self.assertIn("&", hook[0], "the update must not make a session wait")
        self.assertNotIn(str(self.home), hook[0])

    def test_no_self_update_hook_by_default(self):
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code")[0], 0)
        self.assertNotIn("self-update", (self.home / ".claude" / "settings.json").read_text())


class TestReinstallKeepsOtherTools(Base):
    def test_installing_for_one_tool_keeps_the_others_already_installed_working(self):
        """Found on the owner's Mac: VS Code's prompt files live under the harness's content folder, which a
        re-install for Claude Code alone replaced, so `harness doctor` failed for VS Code."""
        (self.home / "Library" / "Application Support" / "Code" / "User").mkdir(parents=True)
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "copilot-vscode")[0], 0)
        self.assertEqual(run(self.home, "doctor")[0], 0, "control: doctor passes after the first install")
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code")[0], 0)
        rc, out = run(self.home, "doctor")
        self.assertEqual(rc, 0, out)


class TestProfileSkills(Base):
    def test_a_profile_skill_takes_the_place_of_the_harness_skill_of_that_name(self):
        prof = self.tmp / "p"
        (prof / "skills" / "standards").mkdir(parents=True)
        (prof / "profile.toml").write_text('name = "p"\n')
        (prof / "skills" / "standards" / "SKILL.md").write_text("---\nname: standards\ndescription: mine\n---\nOWNER\n")
        self.assertEqual(run(self.home, "install", "--yes", "--tools", "claude-code", "--profile", str(prof))[0], 0)
        self.assertIn("OWNER", (self.home / ".claude" / "skills" / "standards" / "SKILL.md").read_text())
        self.assertTrue((self.home / ".claude" / "skills" / "work-loop" / "SKILL.md").is_file(), "others kept")


class TestGlob(Base):
    """2: kb_paths take a glob, so a profile names a folder whose exact name differs per account."""

    def test_glob_kb_paths_expand_to_every_match(self):
        from agent_harness.mcp.kb import expand_kb_paths
        for acct in ("-Users-a-x", "-Users-b-y"):
            d = self.home / ".claude" / "projects" / acct / "memory"
            d.mkdir(parents=True)
            (d / "m.md").write_text("# a memory\n")
        got = expand_kb_paths(["~/.claude/projects/*/memory"], None)
        self.assertEqual(sorted(p.parent.name for p in got), ["-Users-a-x", "-Users-b-y"])
        self.assertEqual(expand_kb_paths(["~/notes"], None), [self.home / "notes"])


TAGS = [{"name": "v0.4.1", "commit": {"sha": "a" * 40}}, {"name": "v0.4.2", "commit": {"sha": "b" * 40}},
        {"name": "not-a-release", "commit": {"sha": "c" * 40}}]


def release_tarball(version="0.4.2", evil=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        def add(name, data=b""):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        add("agent-harness-%s/src/agent_harness/__init__.py" % version, b'__version__ = "%s"\n' % version.encode())
        add("agent-harness-%s/bin/harness" % version, b"#!/bin/sh\n")
        if evil:
            add("../escape.txt", b"x")
    return buf.getvalue()


class TestSelfUpdate(Base):
    """3: the newest release whose checks all passed; never a failed or running one, a branch, or a downgrade."""

    def install_version(self, v):
        f = self.hh() / "lib" / "agent_harness" / "__init__.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text('__version__ = "%s"\n' % v)

    def fake_get(self, checks="success", tar=None):
        def get(url):
            if url.endswith("/tags?per_page=100"):
                return json.dumps(TAGS).encode()
            if "/check-runs" in url:
                return json.dumps({"check_runs": [{"name": "ci", "status": "completed", "conclusion": checks},
                                                  {"name": "lint", "status": "completed", "conclusion": "success"}]}).encode()
            if "codeload.github.com" in url:
                return tar if tar is not None else release_tarball()
            raise AssertionError(url)
        return get

    def go(self, **kw):
        from agent_harness import selfupdate
        calls = []
        kw.setdefault("get", self.fake_get())
        rc, msg = selfupdate.run(self.hh(), self.home, now=True, update_cmd=["true"], doctor_cmd=["true"], **kw)
        return rc, msg, calls

    def test_self_update_takes_the_newest_release_that_passed(self):
        self.install_version("0.4.1")
        rc, msg, _ = self.go()
        self.assertEqual(rc, 0, msg)
        self.assertIn("0.4.1 -> v0.4.2", msg)
        self.assertEqual(json.loads((self.hh() / "state" / "self-update.json").read_text())["result"], "updated")

    def test_self_update_holds_a_release_whose_checks_failed(self):
        self.install_version("0.4.1")
        rc, msg, _ = self.go(get=self.fake_get(checks="failure"))
        self.assertEqual(rc, 0)
        self.assertIn("not taken", msg)
        self.assertEqual(json.loads((self.hh() / "state" / "self-update.json").read_text())["result"], "held")

    def test_self_update_never_downgrades_and_reports_current(self):
        self.install_version("0.4.2")
        self.assertIn("is current", self.go()[1])
        self.install_version("0.9.0")
        self.assertIn("is current", self.go()[1])

    def test_self_update_refuses_an_archive_that_escapes(self):
        self.install_version("0.4.1")
        rc, msg, _ = self.go(get=self.fake_get(tar=release_tarball(evil=True)))
        self.assertNotEqual(rc, 0)
        self.assertIn("refused", msg)
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_self_update_without_network_changes_nothing(self):
        from agent_harness import selfupdate
        self.install_version("0.4.1")

        def down(url):
            raise OSError("network is unreachable")
        rc, msg = selfupdate.run(self.hh(), self.home, now=True, get=down, update_cmd=["false"])
        self.assertEqual(rc, 0)
        self.assertIn("no update", msg)

    def test_self_update_runs_at_most_daily(self):
        from agent_harness import selfupdate
        self.install_version("0.4.1")
        (self.hh() / "state").mkdir(parents=True)
        (self.hh() / "state" / "self-update.json").write_text(json.dumps({"epoch": time.time()}))
        rc, msg = selfupdate.run(self.hh(), self.home, get=self.fake_get(), update_cmd=["false"])
        self.assertIn("within the last day", msg)

    def test_self_update_check_changes_nothing(self):
        from agent_harness import selfupdate
        self.install_version("0.4.1")
        rc, msg = selfupdate.run(self.hh(), self.home, check_only=True, get=self.fake_get(), update_cmd=["false"])
        self.assertIn("would update", msg)
        self.assertFalse((self.hh() / "state" / "self-update.json").exists())


class TestStrict(Base):
    """4: the release trend: a single run is strict; repeated runs allow one run in 54 below the BEST accepted
    release (the owner, 2026-10-07: "it is ok to iterate not to give up updating"), never compounding."""

    def check(self, rows):
        f = self.tmp / "h.jsonl"
        f.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return run(self.home, "improve", "--trend", str(f), "--release", rows[-1]["version"])[0]

    def r(self, v, p, n):
        return {"version": v, "tool": "claude-code", "pass": p, "n": n, "tokens": 1000}

    def test_strict_single_run_one_question_below_is_refused(self):
        self.assertNotEqual(self.check([self.r("0.3.3", 17, 18), self.r("0.4.1", 16, 18)]), 0)
        self.assertEqual(self.check([self.r("0.3.3", 17, 18), self.r("0.4.2", 17, 18)]), 0)

    def test_strict_three_runs_allow_one_run_below_but_not_two(self):
        self.assertEqual(self.check([self.r("0.3.3", 51, 54), self.r("0.4.3", 50, 54)]), 0)
        self.assertNotEqual(self.check([self.r("0.3.3", 51, 54), self.r("0.4.3", 49, 54)]), 0)
        self.assertEqual(self.check([self.r("0.3.3", 102, 108), self.r("0.4.3", 100, 108)]), 0)
        self.assertNotEqual(self.check([self.r("0.3.3", 102, 108), self.r("0.4.3", 99, 108)]), 0)

    def test_strict_allowance_never_compounds(self):
        """Each release is held to the BEST accepted one, so one-run slips cannot add up release after release."""
        rows = [self.r("0.3.3", 51, 54), self.r("0.4.2", 50, 54), self.r("0.4.3", 49, 54)]
        self.assertNotEqual(self.check(rows), 0)

    def test_strict_is_the_documented_owner_decision(self):
        from agent_harness import improve
        self.assertEqual(improve.RUNS_PER_ALLOWED_MISS, 54)
        self.assertIn("one run in 54", (ROOT / "docs" / "design-decisions.md").read_text())


if __name__ == "__main__":
    unittest.main()
