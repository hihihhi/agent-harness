"""The two v0.2 eval arms, both off by default: memory_snapshot (saved facts frozen into the MCP instructions at
session start) and skill_nudge (a Stop-hook reminder after a long turn with no skill saved).
unittest-style; pytest collects it. HARNESS_HOME is a temp dir in every test."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness import cli  # noqa: E402
from agent_harness.mcp.memory import SNAPSHOT_CHARS, Memory  # noqa: E402
from agent_harness.mcp.server import Server  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hh = self.tmp / "hh"
        env = mock.patch.dict(os.environ, {"HARNESS_HOME": str(self.hh), "HARNESS_ENABLE": "", "HARNESS_DISABLE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def enable(self, name):
        p = mock.patch.dict(os.environ, {"HARNESS_ENABLE": name})
        p.start()
        self.addCleanup(p.stop)


class SnapshotTest(Base):
    def init_text(self):
        out = Server(home=self.hh).handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        return out["result"]["instructions"]

    def test_off_by_default_on_when_enabled_and_capped(self):
        m = Memory(home=self.hh)
        m.mem_add("my scratch outputs live in ~/acme-data/kestrel-scratch")
        m.mem_search("scratch outputs kestrel")                         # used once: ranks first
        for i in range(80):
            m.mem_add("fact number %d about widget %s" % (i, "q" * i))
        m.close()
        self.assertNotIn("kestrel-scratch", self.init_text())          # default: relevance-only recall
        self.enable("memory_snapshot")
        text = self.init_text()
        self.assertIn("What you remember", text)
        self.assertIn("kestrel-scratch", text)
        snap = text.split("What you remember", 1)[1]
        self.assertLessEqual(len(snap), SNAPSHOT_CHARS + 80)
        self.assertIn("fact number 79", snap)                           # newest next
        self.assertNotIn("fact number 0 ", snap)                        # the oldest unused: cut at the cap


class NudgeTest(Base):
    def transcript(self, *entries):
        p = self.tmp / "t.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in entries))
        return p

    def stop(self, path):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            cli.stop_hook(io.StringIO(json.dumps({"session_id": "s1", "transcript_path": str(path),
                                                  "stop_hook_active": False, "cwd": str(self.tmp)})))
        return buf.getvalue()

    user = {"type": "user", "uuid": "u1", "message": {"content": "count the fills"}}

    @staticmethod
    def calls(n, name="Bash"):
        return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}] * n

    def test_off_by_default(self):
        self.assertEqual(self.stop(self.transcript(self.user, *self.calls(30))), "")

    def test_long_turn_without_a_skill_is_nudged_once(self):
        self.enable("skill_nudge")
        self.assertEqual(self.stop(self.transcript(self.user, *self.calls(cli.NUDGE_TOOLS - 1))), "")   # short
        p = self.transcript(self.user, *self.calls(cli.NUDGE_TOOLS))
        out = json.loads(self.stop(p))
        self.assertEqual(out["decision"], "block")
        self.assertIn("skill_manage", out["reason"])
        self.assertEqual(self.stop(p), "")                                   # once per turn
        saved = self.transcript(dict(self.user, uuid="u2"), *self.calls(cli.NUDGE_TOOLS),
                                *self.calls(1, "mcp__harness__skill_manage"))
        self.assertEqual(self.stop(saved), "")                               # a skill was saved: nothing


if __name__ == "__main__":
    unittest.main()
