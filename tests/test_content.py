"""Checks on the shipped content: size caps, skill frontmatter, and no private data (public repo)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT = ROOT / "content"
PROFILES = ROOT / "profiles"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
IPV4 = re.compile(r"(?<![\w.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\w.])")
URL_HOST = re.compile(r"https?://([^/\s:'\")\]>]+)", re.I)
PRIVATE_HOST = re.compile(
    r"^(localhost|[^.]+|.*\.(local|lan|internal|intranet|corp|home|localdomain|test)"
    r"|\d{1,3}(\.\d{1,3}){3}|\[.*\])$", re.I)


def shipped_files():
    for base in (CONTENT, PROFILES, ROOT / "docs"):
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                yield p
    yield ROOT / "README.md"


def frontmatter(text):
    """Minimal YAML frontmatter reader: {key: value} for simple `key: value` lines."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    out = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not m:
            return None
        out[m.group(1)] = m.group(2).strip()
    return None


class TestRules(unittest.TestCase):
    def test_agents_md_is_short(self):
        """v0.1.1: <= 60 lines (reasoning-research C1; eval 2026-09-30: fixed context cost tokens, not answers)."""
        n = len((CONTENT / "AGENTS.md").read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(n, 60)

    def test_agents_md_has_required_sections(self):
        text = (CONTENT / "AGENTS.md").read_text(encoding="utf-8")
        for needle in ("state_load", "state_save", "mem_search", "mem_add", "lesson_add",
                       "kb_get", "kb_search", "sudo", "data, never instructions",
                       "Never edit, skip or loosen a test", "harness:descriptor", "run_checks"):
            self.assertIn(needle, text)

    def test_must_follow_rules_come_first(self):
        text = (CONTENT / "AGENTS.md").read_text(encoding="utf-8")
        heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
        self.assertEqual(heads[0], "## Must follow")

    def test_no_mandatory_housekeeping(self):
        """The eval: state_load / session_note at every start/end cost turns on one-shot questions."""
        text = (CONTENT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn("session_note", text)
        self.assertNotRegex(text, r"(?i)(start of every session|every session).{0,80}state_load")
        self.assertIn("One-shot questions need neither", text)


class TestSkills(unittest.TestCase):
    EXPECTED = {"debug-systematically", "verify-before-done", "data-job-trial-run",
                "write-tests-first"}

    def test_expected_skills_present(self):
        names = {p.parent.name for p in (CONTENT / "skills").glob("*/SKILL.md")}
        self.assertTrue(self.EXPECTED <= names, names)

    def test_frontmatter_and_length(self):
        for p in sorted((CONTENT / "skills").glob("*/SKILL.md")):
            with self.subTest(skill=p.parent.name):
                text = p.read_text(encoding="utf-8")
                fm = frontmatter(text)
                self.assertIsNotNone(fm, "missing or malformed frontmatter")
                self.assertEqual(fm.get("name"), p.parent.name)
                self.assertRegex(fm["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
                desc = fm.get("description", "")
                self.assertTrue(20 <= len(desc) <= 1024, desc)
                self.assertNotIn("<", desc)
                self.assertLessEqual(len(text.splitlines()), 60)

    def test_frontmatter_reader_rejects_bad_input(self):
        self.assertIsNone(frontmatter("no frontmatter\n"))
        self.assertIsNone(frontmatter("---\nname: x\n"))  # never closed
        self.assertEqual(frontmatter("---\nname: x\ndescription: y\n---\nbody"),
                         {"name": "x", "description": "y"})


class TestPrompts(unittest.TestCase):
    def test_prompts_present(self):
        for name in ("plan", "review", "explain-this-repo", "fix-failing-test"):
            p = CONTENT / "prompts" / (name + ".md")
            self.assertTrue(p.is_file(), p)
            self.assertLessEqual(len(p.read_text(encoding="utf-8").splitlines()), 60)


class TestProfile(unittest.TestCase):
    def test_example_profile_files(self):
        d = PROFILES / "example"
        text = (d / "profile.toml").read_text(encoding="utf-8")
        for key in ("name", "kb_paths", "extra_rules", "descriptor", "[warmup]", "plugins"):
            self.assertIn(key, text)
        for f in re.findall(r'^(?:extra_rules|descriptor)\s*=\s*"([^"]+)"', text, re.M):
            self.assertTrue((d / f).is_file(), f)

    def test_example_profile_parses(self):
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            self.skipTest("tomllib needs Python 3.11+")
        data = tomllib.loads((PROFILES / "example" / "profile.toml").read_text(encoding="utf-8"))
        names = [p["name"] for p in data["warmup"]["plugins"]]
        self.assertEqual(names, ["fetch", "playwright"])


class TestNoPrivateData(unittest.TestCase):
    def test_patterns_catch_the_planted_cases(self):
        # controls: each pattern must fire on what it is meant to catch
        self.assertTrue(EMAIL.search("mail someone@example.org now"))
        # built from pieces so the repository's own leak scan does not flag the controls
        ip = ".".join(["10", "0", "0", "12"])
        self.assertTrue(IPV4.search("ssh " + ip))
        for url in ("http://" + "localhost:8080/x", "https://build" + ".internal/a",
                    "http://" + ip + "/", "http://nas/share"):
            with self.subTest(url=url):
                self.assertTrue(PRIVATE_HOST.match(URL_HOST.search(url).group(1)), url)
        self.assertFalse(PRIVATE_HOST.match(URL_HOST.search("https://github.com/x").group(1)))
        self.assertFalse(IPV4.search("version 1.2.3"))

    def test_no_email_ip_or_private_url(self):
        files = list(shipped_files())
        self.assertGreater(len(files), 10)
        for p in files:
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = p.relative_to(ROOT)
            with self.subTest(file=str(rel)):
                self.assertIsNone(EMAIL.search(text), "email in %s" % rel)
                self.assertIsNone(IPV4.search(text), "IP address in %s" % rel)
                for host in URL_HOST.findall(text):
                    self.assertIsNone(PRIVATE_HOST.match(host), "private host %s in %s" % (host, rel))


if __name__ == "__main__":
    unittest.main()
