"""run_checks (D2): discovery, only-the-failures output, the record the Stop gate reads, the arm switch."""
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

from agent_harness.mcp import checks  # noqa: E402
from agent_harness.mcp.server import Server, tool_list  # noqa: E402

TEST = ("import sys, unittest\nsys.path.insert(0, '.')\nimport m\n\n"
        "class T(unittest.TestCase):\n{}")


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"HARNESS_HOME": str(self.tmp / "hh"), "HARNESS_ENABLE": "run_checks"})
        self.env.start()
        self.proj = self.tmp / "proj"
        (self.proj / "tests").mkdir(parents=True)
        (self.proj / ".git").mkdir()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovery(self):
        d = self.tmp / "d"
        d.mkdir()
        self.assertIsNone(checks.discover(d))
        (d / "Makefile").write_text("all:\n\techo\ntest:\n\techo ok\n")
        self.assertEqual(checks.discover(d), ["make", "test"])
        (d / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        self.assertEqual(checks.discover(d), ["npm", "test", "--silent"])
        (d / "pytest.ini").write_text("[pytest]\n")
        self.assertEqual(checks.discover(d)[1:], ["-m", "pytest", "-q"])

    def test_only_failures_under_2kb_and_recorded(self):
        (self.proj / "m.py").write_text("def f():\n    return 2\n")
        body = "".join("    def test_ok%d(self):\n        print('noise ' * 50)\n        self.assertTrue(True)\n" % i
                       for i in range(60))
        body += "    def test_f(self):\n        self.assertEqual(m.f(), 1)\n"
        (self.proj / "tests" / "test_m.py").write_text(TEST.format(body))
        srv = Server(home=self.tmp / "hh")
        with mock.patch("os.getcwd", return_value=str(self.proj)):
            text = srv.render("run_checks", srv.call_tool("run_checks", {}))
        self.assertTrue(text.startswith("exit 1: "), text)
        self.assertIn("test_f", text)
        self.assertLessEqual(len(text.encode()), checks.CAP + 200)
        self.assertNotIn("noise noise noise noise noise noise noise noise noise noise noise noise", text)
        rec = json.loads(checks.record_path(self.proj).read_text())
        self.assertEqual(rec["exit"], 1)
        self.assertEqual(checks.unchecked_changes(self.proj)[0], True)

    def test_pass_then_change(self):
        (self.proj / "m.py").write_text("def f():\n    return 1\n")
        (self.proj / "tests" / "test_m.py").write_text(TEST.format(
            "    def test_f(self):\n        self.assertEqual(m.f(), 1)\n"))
        r = checks.run_checks(project=str(self.proj))
        self.assertEqual(r["exit"], 0, r)
        self.assertTrue(r["output"].startswith("passed"))
        self.assertEqual(checks.unchecked_changes(self.proj), (False, ""))
        (self.proj / "new.py").write_text("x = 1\n")
        self.assertTrue(checks.unchecked_changes(self.proj)[0])

    def test_no_checks_and_cmd(self):
        empty = self.tmp / "empty"
        (empty / ".git").mkdir(parents=True)
        r = checks.run_checks(project=str(empty))
        self.assertIsNone(r["exit"])
        self.assertIn("No checks found", r["output"])
        r = checks.run_checks(cmd="echo hello; exit 3", project=str(empty))
        self.assertEqual(r["exit"], 3)

    def test_arm_switch_hides_the_tool(self):
        self.assertIn("run_checks", [t["name"] for t in tool_list()])
        with mock.patch.dict(os.environ, {"HARNESS_ENABLE": ""}):
            self.assertNotIn("run_checks", [t["name"] for t in tool_list()])        # off by default
        with mock.patch.dict(os.environ, {"HARNESS_DISABLE": "check_guard,run_checks"}):
            self.assertNotIn("run_checks", [t["name"] for t in tool_list()])
            r = Server(home=self.tmp / "hh").handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_checks"}})
            self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
