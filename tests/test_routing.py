"""routing.py: the template resolved against an account's own model list, with every lowering rule."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agent_harness import routing as R  # noqa: E402

CONTENT = ROOT / "content"


def levels(*names):
    return [{"effort": n, "description": n} for n in names]


# shaped like a real Codex cache (2026-10-06); slugs invented, so no test depends on a vendor's names
CACHE = {"models": [
    {"slug": "m-hidden-fast", "description": "Fast and affordable", "visibility": "hide", "priority": 1,
     "supported_reasoning_levels": levels("low", "medium", "high")},
    {"slug": "m-flagship", "description": "Frontier model for complex work", "visibility": "list", "priority": 2,
     "supported_reasoning_levels": levels("low", "medium", "high", "xhigh", "max")},
    {"slug": "m-small", "description": "Older fast and efficient model", "visibility": "list", "priority": 9,
     "supported_reasoning_levels": levels("low", "medium")},
]}


class Routing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cache(self, data=CACHE):
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex" / "models_cache.json").write_text(json.dumps(data))

    def test_template_resolves_from_the_accounts_models(self):
        self.cache()
        r = R.resolve(CONTENT, self.home)
        self.assertEqual(set(r), {"quick", "standard", "deep", "review"})
        self.assertEqual(r["quick"]["codex"]["model"], "m-small")          # visible + described as fast
        self.assertEqual(r["quick"]["codex"]["effort"], "low")
        self.assertEqual(r["deep"]["codex"]["model"], "m-flagship")        # first in the picker
        self.assertEqual(r["deep"]["codex"]["effort"], "high")
        self.assertTrue(all(t["when"] for t in r.values()))

    def test_a_hidden_model_is_never_chosen(self):
        self.cache()
        r = R.resolve(CONTENT, self.home)
        self.assertNotIn("m-hidden-fast", {t["codex"]["model"] for t in r.values()})

    def test_config_model_is_the_default(self):
        self.cache()
        (self.home / ".codex" / "config.toml").write_text('model = "m-small"\n')
        self.assertEqual(R.resolve(CONTENT, self.home)["deep"]["codex"]["model"], "m-small")

    def test_effort_lowered_to_what_the_model_offers(self):
        self.cache()
        (self.home / ".codex" / "config.toml").write_text('model = "m-small"\n')   # offers low, medium only
        self.assertEqual(R.resolve(CONTENT, self.home)["deep"]["codex"]["effort"], "medium")

    def test_ceiling_holds_unless_raised(self):
        self.cache()
        self.assertEqual(R.resolve(CONTENT, self.home, {"routing": {"tiers": {"deep": {"effort": "max"}}}})
                         ["deep"]["codex"]["effort"], "high")
        r = R.resolve(CONTENT, self.home, {"routing": {"ceiling": "max", "tiers": {"deep": {"effort": "max"}}}})
        self.assertEqual(r["deep"]["codex"]["effort"], "max")
        self.assertEqual(r["deep"]["claude"]["effort"], "max")

    def test_user_overrides_profile_overrides_template(self):
        self.cache()
        prof = {"routing": {"tiers": {"quick": {"effort": "medium"}}}}
        self.assertEqual(R.resolve(CONTENT, self.home, prof)["quick"]["codex"]["effort"], "medium")
        (self.home / ".agent-harness").mkdir()
        (self.home / ".agent-harness" / "routing.toml").write_text('[tiers.quick]\neffort = "low"\ncodex = "m-flagship"\n')
        r = R.resolve(CONTENT, self.home, prof)
        self.assertEqual((r["quick"]["codex"]["model"], r["quick"]["codex"]["effort"]), ("m-flagship", "low"))

    def test_a_named_model_the_account_lacks_falls_back(self):
        self.cache()
        r = R.resolve(CONTENT, self.home, {"routing": {"tiers": {"deep": {"codex": "no-such-model"}}}})
        self.assertEqual(r["deep"]["codex"]["model"], "m-flagship")
        self.assertIn("not in your model list", r["deep"]["codex"]["why"])

    def test_no_model_list_pins_nothing(self):
        r = R.resolve(CONTENT, self.home)
        self.assertIsNone(r["deep"]["codex"]["model"])
        self.assertEqual(r["deep"]["codex"]["effort"], "high")
        self.assertIn("no model list", r["deep"]["codex"]["why"])

    def test_claude_ladder(self):
        r = R.resolve(CONTENT, self.home)
        ladder = R.load(CONTENT, self.home, None)["claude"]["ladder"]
        self.assertEqual(r["quick"]["claude"]["model"], ladder[0])
        self.assertEqual(r["deep"]["claude"]["model"], ladder[-1])
        r = R.resolve(CONTENT, self.home, {"routing": {"tiers": {"deep": {"claude": "inherit"}}}})
        self.assertIsNone(r["deep"]["claude"]["model"])

    def test_lowering_rules(self):
        lad = ["minimal", "low", "medium", "high", "xhigh", "max"]
        self.assertEqual(R.lower_effort("max", None, lad, "high"), "high")
        self.assertEqual(R.lower_effort("high", ["low", "medium"], lad, "high"), "medium")
        self.assertEqual(R.lower_effort("minimal", ["low", "medium"], lad, "high"), "low")   # nothing lower offered
        self.assertEqual(R.lower_effort("bogus", None, lad, "high"), "medium")


if __name__ == "__main__":
    unittest.main()
