"""check_guard (D3): asks before an existing test/gate loses an assertion, gains a skip, or a tolerance moves."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "content" / "hooks" / "check_guard.py"
sys.path.insert(0, str(HOOK.parent))
import check_guard as cg  # noqa: E402

TEST = "def test_a():\n    assert f(1) == 2\n    assert f(2) == 3\n\ndef test_b():\n    assert g() == pytest.approx(1.0, rel=1e-6)\n"


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "tests").mkdir()
        self.t = self.tmp / "tests" / "test_x.py"
        self.t.write_text(TEST)
        (self.tmp / "src.py").write_text("def f(x):\n    assert x\n    return x + 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ask(self, tool, inp, env=None):
        data = {"tool_name": tool, "tool_input": inp, "cwd": str(self.tmp)}
        with mock.patch.dict(os.environ, dict({"HARNESS_ENABLE": "check_guard"}, **(env or {})), clear=False):
            return cg.decide(data)

    def test_removed_assertion_asks(self):
        out = self.ask("Edit", {"file_path": str(self.t), "old_string": "    assert f(2) == 3\n", "new_string": ""})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("removes 1 assertion", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_skip_and_tolerance_ask(self):
        out = self.ask("Edit", {"file_path": "tests/test_x.py", "old_string": "def test_a():",
                                "new_string": "@pytest.mark.skip\ndef test_a():"})
        self.assertIn("skip", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.ask("Edit", {"file_path": str(self.t), "old_string": "rel=1e-6", "new_string": "rel=1e-1"})
        self.assertIn("tolerance", out["hookSpecificOutput"]["permissionDecisionReason"])
        new = TEST.replace("    assert f(2) == 3\n", "")
        self.assertTrue(self.ask("Write", {"file_path": str(self.t), "content": new}))

    def test_changed_expected_value_asks(self):
        out = self.ask("Edit", {"file_path": str(self.t), "old_string": "assert f(2) == 3", "new_string": "assert f(2) == 4"})
        self.assertIn("changes what 1 assertion(s) expect", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_legit_edits_pass(self):
        # a rename inside assertions, a new assertion, a new test file, a non-test file losing an assert
        self.assertIsNone(self.ask("Edit", {"file_path": str(self.t), "old_string": "assert f(1) == 2",
                                            "new_string": "assert inc(1) == 2"}))
        self.assertIsNone(self.ask("Edit", {"file_path": str(self.t), "old_string": "    assert f(2) == 3\n",
                                            "new_string": "    assert f(2) == 3\n    assert f(3) == 4\n"}))
        self.assertIsNone(self.ask("Write", {"file_path": str(self.tmp / "tests" / "test_new.py"),
                                             "content": "def test_n():\n    pass\n"}))
        self.assertIsNone(self.ask("Edit", {"file_path": str(self.tmp / "src.py"), "old_string": "    assert x\n",
                                            "new_string": ""}))
        self.assertIsNone(self.ask("MultiEdit", {"file_path": str(self.t), "edits": [
            {"old_string": "    assert f(2) == 3\n", "new_string": ""},
            {"old_string": "def test_b():\n", "new_string": "def test_b():\n    assert f(2) == 3\n"}]}))

    def test_arm_switch_and_cli_never_fails(self):
        self.assertIsNone(self.ask("Edit", {"file_path": str(self.t), "old_string": "    assert f(2) == 3\n",
                                            "new_string": ""}, env={"HARNESS_DISABLE": "run_checks,check_guard"}))
        self.assertIsNone(self.ask("Edit", {"file_path": str(self.t), "old_string": "    assert f(2) == 3\n",
                                            "new_string": ""}, env={"HARNESS_ENABLE": ""}))   # off by default
        for stdin in ("not json", "{}", json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(self.t),
                      "old_string": "    assert f(2) == 3\n", "new_string": ""}, "cwd": str(self.tmp)})):
            p = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True,
                               env=dict(os.environ, HARNESS_ENABLE="check_guard"))
            self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")


if __name__ == "__main__":
    unittest.main()
