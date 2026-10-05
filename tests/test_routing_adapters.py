"""Routing through the installer: each tool gets one subagent per tier, with the resolved model and effort;
an update follows a changed override; uninstall puts the home back as it was."""
import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from agent_harness import installer as I  # noqa: E402
from test_installer import Base, snapshot  # noqa: E402
from test_routing import CACHE  # noqa: E402

TIERS = ("quick", "standard", "deep", "review")


def frontmatter(text):
    head = text.split("---")[1]
    return dict(line.split(": ", 1) for line in head.strip().splitlines())


class RoutingInstall(Base):
    def setUp(self):
        super().setUp()
        shutil.copy(ROOT / "content" / "routing.toml", self.src / "content" / "routing.toml")
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "models_cache.json").write_text(json.dumps(CACHE))

    def test_every_tier_for_both_tools(self):
        rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        for t in TIERS:
            fm = frontmatter((self.home / ".claude" / "agents" / f"harness-{t}.md").read_text())
            self.assertEqual(fm["name"], f"harness-{t}")
            self.assertIn("effort", fm)
            self.assertTrue(fm["description"].startswith('"Use for '))
            cx = I.load_toml((self.home / ".codex" / "agents" / f"harness-{t}.toml").read_text())
            self.assertEqual(cx["name"], f"harness-{t}")
            self.assertTrue(cx["developer_instructions"])
        quick = I.load_toml((self.home / ".codex" / "agents" / "harness-quick.toml").read_text())
        deep = I.load_toml((self.home / ".codex" / "agents" / "harness-deep.toml").read_text())
        self.assertEqual((quick["model"], quick["model_reasoning_effort"]), ("m-small", "low"))
        self.assertEqual((deep["model"], deep["model_reasoning_effort"]), ("m-flagship", "high"))
        self.assertEqual(frontmatter((self.home / ".claude" / "agents" / "harness-deep.md").read_text())["effort"], "high")

    def test_update_follows_a_user_override(self):
        self.install("--yes")
        (self.hh / "routing.toml").write_text('[tiers.deep]\neffort = "medium"\n')
        rc, out = self.install("--yes")
        self.assertEqual(rc, 0, out)
        deep = I.load_toml((self.home / ".codex" / "agents" / "harness-deep.toml").read_text())
        self.assertEqual(deep["model_reasoning_effort"], "medium")

    def test_uninstall_restores(self):
        (self.home / ".claude" / "agents").mkdir(parents=True)
        (self.home / ".claude" / "agents" / "mine.md").write_text("---\nname: mine\n---\nmine\n")
        before = snapshot(self.home)
        self.install("--yes")
        rc, out = self.install_cmd("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(snapshot(self.home), before)

    def install_cmd(self, *argv):
        from test_installer import run
        return run(self.home, *argv)

    def test_without_the_template_nothing_is_routed(self):
        (self.src / "content" / "routing.toml").unlink()
        self.install("--yes")
        self.assertFalse((self.home / ".codex" / "agents").exists())
        self.assertFalse((self.home / ".claude" / "agents").exists())


if __name__ == "__main__":
    unittest.main()
