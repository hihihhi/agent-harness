"""Curated editor extensions: catalogue evidence, Cursor licence rule, install/uninstall bookkeeping.

A fake `code` / `cursor` CLI on PATH records its calls; nothing touches the real HOME or editors.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_harness import extensions as X  # noqa: E402
from agent_harness.adapters.base import Ctx  # noqa: E402

CATALOG = ROOT / "content" / "extensions.toml"

FAKE = """#!{py}
import sys, pathlib
d = pathlib.Path({d!r})
name = pathlib.Path(sys.argv[0]).name
with open(d / (name + ".calls"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
have = d / (name + ".have")
if sys.argv[1:2] == ["--list-extensions"]:
    print(have.read_text() if have.exists() else "", end="")
elif sys.argv[1:2] == ["--install-extension"] and sys.argv[2] == "fail.me":
    sys.exit(1)
"""


class TestCatalog(unittest.TestCase):
    def setUp(self):
        self.entries, self.settings = X.load_catalog(CATALOG)

    def test_parses_with_evidence(self):
        self.assertGreaterEqual(len(self.entries), 10)
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(ids), len(set(i.lower() for i in ids)))
        for e in self.entries:
            for f in X.REQUIRED_FIELDS:
                self.assertIn(f, e, (e.get("id"), f))
            self.assertIn(e["verified"], ("yes", "no"))
            self.assertTrue(e["source"].startswith("https://"), e["id"])
            self.assertIsInstance(e["size_mb"], int)
            self.assertTrue(set(e["targets"]) <= {"vscode", "cursor"} and e["targets"], e["id"])
            self.assertIn(e["where"], ("local", "remote", "both"))
            self.assertGreater(len(e["why"]), 10)
            self.assertIn(e.get("requires", "claude-code"), ("claude-code", "codex", "copilot-vscode"))

    def test_only_verified_publishers(self):
        self.assertEqual([e["id"] for e in self.entries if e["verified"] != "yes"], [])

    def test_cursor_never_gets_microsoft_restricted(self):
        bad = [e["id"] for e in self.entries if "cursor" in e["targets"] and X.ms_restricted(e["id"])]
        self.assertEqual(bad, [])
        self.assertTrue(X.ms_restricted("ms-vscode-remote.remote-ssh"))
        self.assertTrue(X.ms_restricted("MS-Python.VSCode-Pylance"))
        self.assertFalse(X.ms_restricted("ms-python.python"))

    def test_select_filters(self):
        cur = [e["id"] for e in X.select(self.entries, "cursor", ["codex"])]
        self.assertIn("anysphere.remote-ssh", cur)
        self.assertIn("openai.chatgpt", cur)
        self.assertNotIn("anthropic.claude-code", cur)
        vs = [e["id"] for e in X.select(self.entries, "vscode", [])]
        self.assertIn("ms-vscode-remote.remote-ssh", vs)
        self.assertNotIn("anysphere.cursorpyright", vs)
        self.assertNotIn("openai.chatgpt", vs)
        # A restricted id sneaked into a cursor target is still dropped.
        self.assertEqual(X.select([{"id": "ms-python.vscode-pylance", "targets": ["cursor"]}], "cursor", []), [])

    def test_settings(self):
        self.assertEqual(self.settings, {"telemetry.telemetryLevel": "off", "redhat.telemetry.enabled": False})


class TestInstall(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.hh = self.root / "home" / ".agent-harness"
        self.hh.mkdir(parents=True)
        p = mock.patch.dict(os.environ, {"PATH": str(self.bin)})
        p.start()
        self.addCleanup(p.stop)
        self.log = []

    def fake(self, name, have=()):
        f = self.bin / name
        f.write_text(FAKE.format(py=sys.executable, d=str(self.bin)))
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
        (self.bin / (name + ".have")).write_text("".join(i + "\n" for i in have))

    def calls(self, name):
        p = self.bin / (name + ".calls")
        return p.read_text().splitlines() if p.exists() else []

    def catalog(self, entries):
        lines = []
        for e in entries:
            lines.append("[[extension]]")
            for k, v in e.items():
                lines.append(f"{k} = {json.dumps(v)}")
        p = self.root / "cat.toml"
        p.write_text("\n".join(lines) + "\n")
        return p

    def entry(self, i, size=1, targets=("vscode", "cursor"), **kw):
        e = {"id": i, "publisher": "P", "verified": "yes", "source": "https://x", "why": "because it helps",
             "size_mb": size, "targets": list(targets), "where": "remote", "evidence": "e"}
        e.update(kw)
        return e

    def run_install(self, cat, state=None, **kw):
        kw.setdefault("yes", True)
        return X.install(self.hh, state if state is not None else {"tools": []}, catalog=cat,
                         log=self.log.append, **kw)

    def test_installs_missing_skips_present_records_only_added(self):
        self.fake("code", have=["a.one", "user.own"])
        cat = self.catalog([self.entry("a.one"), self.entry("b.two"), self.entry("c.three")])
        state = self.run_install(cat)
        installs = [c for c in self.calls("code") if c.startswith("--install-extension")]
        self.assertEqual(installs, ["--install-extension b.two", "--install-extension c.three"])
        self.assertEqual([r["id"] for r in state["extensions"]["code"]], ["b.two", "c.three"])

        # Uninstall removes exactly what we added: never a.one (pre-existing) or user.own.
        state = X.uninstall(state, log=self.log.append)
        removed = [c for c in self.calls("code") if c.startswith("--uninstall-extension")]
        self.assertEqual(removed, ["--uninstall-extension b.two", "--uninstall-extension c.three"])
        self.assertNotIn("extensions", state)

    def test_cursor_gets_its_targets_only(self):
        self.fake("cursor")
        cat = self.catalog([self.entry("v.only", targets=["vscode"]), self.entry("both.x"),
                            self.entry("ms-vscode-remote.remote-ssh", targets=["vscode", "cursor"])])
        state = self.run_install(cat)
        self.assertEqual([r["id"] for r in state["extensions"]["cursor"]], ["both.x"])

    def test_requires_tool(self):
        self.fake("code")
        cat = self.catalog([self.entry("ai.codex", requires="codex"), self.entry("ai.claude", requires="claude-code")])
        state = self.run_install(cat, tools=["codex"])
        self.assertEqual([r["id"] for r in state["extensions"]["code"]], ["ai.codex"])

    def test_budget_and_failure(self):
        self.fake("code")
        cat = self.catalog([self.entry("big.one", size=600), self.entry("fail.me"), self.entry("big.two", size=600)])
        state = self.run_install(cat, budget_bytes=1000 * X.MB)
        self.assertEqual([r["id"] for r in state["extensions"]["code"]], ["big.one"])
        self.assertTrue(any("fail.me: install failed" in m for m in self.log))
        self.assertTrue(any("big.two" in m and "1 GB" in m for m in self.log))
        self.assertNotIn("--install-extension big.two", self.calls("code"))

    def test_declined_and_no_tty(self):
        self.fake("code")
        cat = self.catalog([self.entry("a.one")])
        with mock.patch.object(X.I, "is_tty", return_value=True):
            state = self.run_install(cat, yes=False, ask=lambda q: False)
        self.assertNotIn("extensions", state)
        with mock.patch.object(X.I, "is_tty", return_value=False):
            state = self.run_install(cat, yes=False, ask=lambda q: True)
        self.assertNotIn("extensions", state)
        self.assertEqual([c for c in self.calls("code") if "--install" in c], [])

    def test_no_editor(self):
        cat = self.catalog([self.entry("a.one")])
        self.assertEqual(self.run_install(cat), {"tools": []})
        self.assertTrue(any("no `code`" in m for m in self.log))

    def test_settings_changes(self):
        home = self.root / "home"
        with mock.patch.object(sys, "platform", "linux"):
            (home / ".config" / "Code" / "User").mkdir(parents=True)
            ctx = Ctx(home=home, harness_home=self.hh, content=ROOT / "content", profile={}, rules="",
                      mcp_cmd=["python3"])
            changes, refused = X.settings_changes(ctx, CATALOG, tools=[])
        self.assertEqual(refused, [])
        self.assertEqual(len(changes), 1)  # Cursor not present -> not touched
        c = changes[0].content
        self.assertEqual(c["telemetry.telemetryLevel"], "off")
        self.assertIs(c["redhat.telemetry.enabled"], False)
        self.assertIn("ms-python.python", c["remote.SSH.defaultExtensions"])
        self.assertNotIn("ms-vscode-remote.remote-ssh", c["remote.SSH.defaultExtensions"])


if __name__ == "__main__":
    unittest.main()
