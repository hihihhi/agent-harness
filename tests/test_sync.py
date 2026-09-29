"""harness sync: two temp HARNESS_HOMEs, a path-form remote and an ssh-form remote via a fake ssh."""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_harness import cli  # noqa: E402
from agent_harness import sync as S  # noqa: E402

FAKE_SSH = "import subprocess, sys\nsys.exit(subprocess.run(['sh', '-c', sys.argv[2]]).returncode)\n"


class SyncCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.a = self.tmp / "laptop"
        self.b = self.tmp / "server"
        for h in (self.a, self.b):
            (h / "memory" / "user").mkdir(parents=True)
            (h / "lessons").mkdir()
        self.remote = str(self.b)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, home, rel, text, mtime=None):
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if mtime is not None:
            os.utime(p, (mtime, mtime))

    def sync(self, now=None):
        return S.run_sync(self.a, self.remote, now=now)

    def test_new_changed_conflict_deleted(self):
        t0 = time.time() - 1000
        self.put(self.a, "memory/user/f1.json", '{"text": "a1"}', t0)
        self.put(self.b, "lessons/l1.json", '{"fix": "b1"}', t0)
        self.put(self.a, "index.sqlite", "local index")
        self.sync()
        # new files travel both ways, indexes never do
        self.assertEqual((self.b / "memory/user/f1.json").read_text(), '{"text": "a1"}')
        self.assertEqual((self.a / "lessons/l1.json").read_text(), '{"fix": "b1"}')
        self.assertFalse((self.b / "index.sqlite").exists())
        # changed on one side only
        self.put(self.b, "memory/user/f1.json", '{"text": "b2"}', t0 + 10)
        self.sync()
        self.assertEqual((self.a / "memory/user/f1.json").read_text(), '{"text": "b2"}')
        # changed on both sides: newest wins, the loser is kept and reported
        self.put(self.a, "memory/user/f1.json", '{"text": "a3"}', t0 + 30)
        self.put(self.b, "memory/user/f1.json", '{"text": "b3"}', t0 + 20)
        report = self.sync()
        self.assertEqual((self.a / "memory/user/f1.json").read_text(), '{"text": "a3"}')
        self.assertEqual((self.b / "memory/user/f1.json").read_text(), '{"text": "a3"}')
        conflicts = sorted(p.name for p in (self.a / "memory/user").glob("f1.conflict-*.json"))
        self.assertEqual(len(conflicts), 1, conflicts)
        self.assertEqual((self.b / "memory/user" / conflicts[0]).read_text(), '{"text": "b3"}')
        self.assertTrue(any(r.startswith("conflict memory/user/f1.json") for r in report), report)
        # deleted on one side: a tombstone removes it on the other
        (self.a / "lessons/l1.json").unlink()
        self.sync()
        self.assertFalse((self.b / "lessons/l1.json").exists())
        self.assertTrue((self.a / "lessons/l1.deleted").exists())
        self.assertTrue((self.b / "lessons/l1.deleted").exists())
        self.sync()  # stable: nothing comes back
        self.assertFalse((self.a / "lessons/l1.json").exists())
        # tombstones expire after 30 days
        self.sync(now=time.time() + 31 * 86400)
        self.assertFalse((self.a / "lessons/l1.deleted").exists())
        self.assertFalse((self.b / "lessons/l1.deleted").exists())

    def test_edit_after_deletion_survives(self):
        t0 = time.time() - 1000
        self.put(self.a, "memory/user/f2.json", "v1", t0)
        self.sync(now=t0 + 1)
        (self.a / "memory/user/f2.json").unlink()
        self.sync(now=t0 + 2)  # tombstone at t0+2
        self.put(self.b, "memory/user/f2.json", "v2 revived", t0 + 5)
        self.sync(now=t0 + 6)
        self.assertEqual((self.a / "memory/user/f2.json").read_text(), "v2 revived")
        self.assertFalse((self.a / "memory/user/f2.deleted").exists())
        self.assertFalse((self.b / "memory/user/f2.deleted").exists())

    def test_session_logs_union_sorted(self):
        self.put(self.a, "sessions/p.log", "2026-09-01 a\n2026-09-03 c\n")
        self.put(self.b, "sessions/p.log", "2026-09-02 b\n2026-09-03 c\n")
        self.sync()
        want = "2026-09-01 a\n2026-09-02 b\n2026-09-03 c\n"
        self.assertEqual((self.a / "sessions/p.log").read_text(), want)
        self.assertEqual((self.b / "sessions/p.log").read_text(), want)

    def test_dry_run_writes_nothing(self):
        self.put(self.a, "memory/user/f1.json", "x")
        out = S.run_sync(self.a, self.remote, dry_run=True)
        self.assertIn("push 1", out[0])
        self.assertFalse((self.b / "memory/user/f1.json").exists())
        self.assertFalse((self.a / "sync-state.json").exists())

    def test_over_ssh_transport(self):
        ssh = self.tmp / "fakessh.py"
        ssh.write_text(FAKE_SSH)
        self.put(self.a, "memory/user/f1.json", "from laptop")
        self.put(self.b, "memory/user/g1.json", "from server")
        remote = f"box:{self.b}"
        out = S.run_sync(self.a, remote, ssh_cmd=f"{sys.executable} {ssh}")
        self.assertIn("pulled 1, pushed 1", out[0])
        self.assertEqual((self.b / "memory/user/f1.json").read_text(), "from laptop")
        self.assertEqual((self.a / "memory/user/g1.json").read_text(), "from server")
        (self.a / "memory/user/f1.json").unlink()
        S.run_sync(self.a, remote, ssh_cmd=f"{sys.executable} {ssh}")
        self.assertFalse((self.b / "memory/user/f1.json").exists())
        self.assertTrue((self.b / "memory/user/f1.deleted").exists())

    def test_cli_config_then_plain_sync(self):
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HARNESS_HOME": str(self.a)}):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(cli.main(["--home", str(home), "sync"]), 1)
                self.assertEqual(cli.main(["--home", str(home), "sync", "--config", "--remote", self.remote]), 0)
                self.put(self.a, "memory/user/f9.json", "nine")
                self.assertEqual(cli.main(["--home", str(home), "sync"]), 0)
        self.assertEqual(json.loads((self.a / "sync.json").read_text())["remote"], self.remote)
        self.assertEqual((self.b / "memory/user/f9.json").read_text(), "nine")


if __name__ == "__main__":
    unittest.main()
