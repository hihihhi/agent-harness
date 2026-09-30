"""skill_manage: procedural skills learned from experience (unittest-style; pytest collects it).
Every test uses a fake home in a temp dir."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness.mcp import skills as K  # noqa: E402
from agent_harness.mcp.memory import Memory  # noqa: E402
from agent_harness.mcp.server import Server  # noqa: E402

BODY = """## When to Use
Counting executed trades of a Shenzhen stock on one day in the cleansed A-share trades.

## Procedure
1. `ic.load("A-Stock/trades", symbols=[sym], start=day, end=day)`.
2. Keep `event == "fill"`: Shenzhen rows include cancellations.

## Pitfalls
- A day the vendor never delivered raises; pick another day.

## Verification
Fills + cancels = all rows.
"""
DESC = "Count executed trades (fills, not cancellations) of a Shenzhen stock on one day from cleansed trades."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)                      # the fake user home
        self.hh = self.home / ".agent-harness"
        self.sk = K.Skills(home=self.hh, user_home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def installed(self, name, desc):
        d = self.home / ".agents" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: %s\ndescription: %s\n---\n\nbody\n" % (name, desc))


class CreateTest(Base):
    def test_create_writes_agentskills_format(self):
        res = self.sk.create("count-sz-fills", DESC, BODY)
        f = self.home / ".agents" / "skills" / "learned" / "count-sz-fills" / "SKILL.md"
        self.assertEqual(res["path"], str(f))
        text = f.read_text()
        self.assertTrue(text.startswith("---\nname: count-sz-fills\ndescription: Count executed trades"))
        fm = K._front(text)
        self.assertEqual((fm["name"], fm["version"]), ("count-sz-fills", "1"))
        self.assertIn("origin: learned", text)
        self.assertIn("## Pitfalls", text)

    def test_bad_names_and_missing_sections_are_refused(self):
        for bad in ("Count Fills", "../escape", "a/b", "x" * 65, "-lead", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.sk.create(bad, DESC, BODY)
        with self.assertRaises(ValueError) as e:
            self.sk.create("no-pitfalls", DESC, BODY.replace("## Pitfalls", "## Notes"))
        self.assertIn("## Pitfalls", str(e.exception))
        with self.assertRaises(ValueError):
            self.sk.create("empty-desc", " ", BODY)
        self.assertFalse((self.home / ".agents" / "skills" / "learned").exists() and
                         any((self.home / ".agents" / "skills" / "learned").iterdir()))

    def test_size_cap_is_an_error_and_long_is_a_warning(self):
        with self.assertRaises(ValueError) as e:
            self.sk.create("too-long", DESC, BODY + "x" * K.BODY_MAX)
        self.assertIn("max %d" % K.BODY_MAX, str(e.exception))
        res = self.sk.create("longish", DESC, BODY + "y " * (K.BODY_WARN // 2))
        self.assertIn("long", res["warnings"][0])

    def test_near_duplicates_are_refused_with_the_name_to_update(self):
        self.installed("fill-counter", DESC)
        with self.assertRaises(ValueError) as e:
            self.sk.create("count-sz-fills", DESC, BODY)
        self.assertIn("fill-counter", str(e.exception))
        self.sk.create("vwap-continuous", "VWAP of a stock over the continuous session only.", BODY)   # control
        with self.assertRaises(ValueError):
            self.sk.create("vwap-continuous", "something else entirely different words", BODY)   # same name

    def test_secrets_are_redacted_before_writing(self):
        key = "sk-" + "ant-" + "Z" * 30
        self.sk.create("with-key", DESC, BODY + "\nexport API_KEY=%s\n" % key)
        text = (self.home / ".agents" / "skills" / "learned" / "with-key" / "SKILL.md").read_text()
        self.assertNotIn(key, text)
        self.assertIn("[REDACTED]", text)


class UpdateViewListTest(Base):
    def test_update_by_passage_bumps_version_and_usage(self):
        self.sk.create("count-sz-fills", DESC, BODY)
        res = self.sk.update("count-sz-fills", old="Fills + cancels = all rows.", new="Fills + cancels = rows; "
                                                                                          "check both counts.")
        self.assertEqual(res["version"], 2)
        text = self.sk.view("count-sz-fills")
        self.assertIn("check both counts", text)
        self.assertIn('version: "2"', text)
        self.assertEqual(json.loads((self.hh / "skills-usage.json").read_text())["count-sz-fills"]["uses"], 2)
        with self.assertRaises(ValueError):
            self.sk.update("count-sz-fills", old="not in the body", new="x")
        with self.assertRaises(ValueError):
            self.sk.update("count-sz-fills", body="no sections at all")
        with self.assertRaises(ValueError):
            self.sk.update("never-made", body=BODY)

    def test_list_shows_installed_and_learned_once(self):
        self.installed("work-loop", "The default loop.")
        link = self.home / ".claude" / "skills"
        link.mkdir(parents=True)
        os.symlink(str(self.home / ".agents" / "skills" / "work-loop"), str(link / "work-loop"))
        self.sk.create("count-sz-fills", DESC, BODY)
        names = [(r["name"], r["origin"]) for r in self.sk.list()]
        self.assertEqual(sorted(names), [("count-sz-fills", "learned"), ("work-loop", "installed")])
        self.assertIn("The default loop.", self.sk.view("work-loop"))


class CapArchiveTest(Base):
    def test_least_used_is_archived_never_deleted(self):
        sk = K.Skills(home=self.hh, user_home=self.home, active_max=3)
        topics = ["Rebuild the widget cache after a deploy.", "Handle bravo gadgets when the queue stalls.",
                  "Rotate charlie sprocket credentials monthly."]
        for i, t in enumerate(topics):
            sk.create("skill-%d" % i, t, BODY)
        sk.view("skill-0")
        sk.view("skill-2")
        old = time.time() - 10 * 86400                     # skill-1: never used, not read natively either
        f1 = self.home / ".agents" / "skills" / "learned" / "skill-1" / "SKILL.md"
        os.utime(str(f1), (old, old))
        res = sk.create("skill-3", "Export delta cog reports to the archive.", BODY)
        self.assertEqual(res["archived"], ["skill-1"])
        self.assertFalse(f1.exists())
        kept = list((self.hh / "skills-archive").glob("skill-1-*/SKILL.md"))
        self.assertEqual(len(kept), 1)
        self.assertIn("Handle bravo gadgets", kept[0].read_text())
        self.assertEqual(sorted(r["name"] for r in sk.list()), ["skill-0", "skill-2", "skill-3"])

    def test_archive_on_request(self):
        self.sk.create("count-sz-fills", DESC, BODY)
        self.sk.archive("count-sz-fills")
        self.assertEqual(self.sk.list(), [])
        self.assertEqual(len(list((self.hh / "skills-archive").glob("count-sz-fills-*/ARCHIVED"))), 1)


class RecallTest(Base):
    def test_relevant_learned_skill_is_named_in_recall_once(self):
        self.sk.create("count-sz-fills", DESC, BODY)
        self.installed("trade-reader", "Count executed trades in the cleansed trades.")   # listed natively: not recalled
        m = Memory(home=self.hh)
        self.addCleanup(m.close)
        out = m.recall("How many executed trades did 000002.SZ have on 2021-07-07 in the cleansed trades?", "s1")
        self.assertIn("learned skill count-sz-fills", out)
        self.assertIn("skill_manage view count-sz-fills", out)
        self.assertNotIn("trade-reader", out)
        self.assertEqual(m.recall("How many executed trades did 000651.SZ have, cleansed trades?", "s1"), "")
        self.assertEqual(m.recall("write a haiku about autumn leaves", "s2"), "")     # unrelated: nothing

    def test_skill_manage_through_the_server(self):
        srv = Server(home=self.hh)
        srv._skills = K.Skills(home=self.hh, user_home=self.home)
        call = lambda args: srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",   # noqa: E731
                                        "params": {"name": "skill_manage", "arguments": args}})["result"]
        r = call({"action": "create", "name": "count-sz-fills", "description": DESC, "body": BODY})
        self.assertFalse(r["isError"], r)
        self.assertIn("count-sz-fills (learned, 0 uses)", call({"action": "list"})["content"][0]["text"])
        self.assertIn("## Procedure", call({"action": "view", "name": "count-sz-fills"})["content"][0]["text"])
        bad = call({"action": "create", "name": "x", "description": DESC, "body": "no sections"})
        self.assertTrue(bad["isError"])
        self.assertTrue(call({"action": "view"})["isError"])


if __name__ == "__main__":
    unittest.main()
