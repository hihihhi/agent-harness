"""Redaction: what the repo's own secret scan would refuse is never stored (unittest-style; pytest collects it)."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness.redact import MASK, redact  # noqa: E402

# Built by concatenation so this file itself never matches the scan.
SAMPLES = [
    "sk-" + "ant-" + "a1B2c3D4e5F6g7H8i9J0kLmN",
    "sk-" + "proj-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2",
    "gh" + "p_" + "A" * 36,
    "github_" + "pat_" + "B" * 40,
    "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "AI" + "za" + "C" * 35,
    "xo" + "xb-" + "1234567890-abcdef",
    "ey" + "J" + "a" * 20 + ".ey" + "J" + "b" * 20 + ".sig",
    "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
    "password" + ' = "hunter2hunter2"',
]


def scan_cred():
    """The CRED regex of scripts/secret-scan.sh, exactly as that script has it."""
    text = (ROOT / "scripts" / "secret-scan.sh").read_text()
    m = re.search(r"^CRED='(.*)'$", text, re.M)
    return m.group(1).replace("'\"'\"'", "'")


def grep_hits(text):
    p = subprocess.run(["grep", "-nEi", "--", scan_cred()], input=text, capture_output=True, text=True)
    return p.stdout.strip()


class RedactTest(unittest.TestCase):
    def test_every_scanned_credential_class_is_masked(self):
        for s in SAMPLES:
            line = "my key is %s ok" % s
            self.assertTrue(grep_hits(line), "control: the scan must catch the raw sample %r" % s[:12])
            out = redact(line)
            self.assertIn(MASK, out, s[:12])
            self.assertEqual(grep_hits(out), "", "the scan still finds %r after redaction: %r" % (s[:12], out))

    def test_unquoted_assignments_and_bearer(self):
        out = redact("export API_KEY=abcd1234efgh5678 and Authorization: Bearer " + "q" * 30)
        self.assertIn("API_KEY=" + MASK, out)
        self.assertNotIn("abcd1234efgh5678", out)
        self.assertNotIn("q" * 30, out)

    def test_conversation_shapes(self):
        for raw, secret in (("postgres://admin:S3cretPassw0rd@db/x", "S3cretPassw0rd"),
                            ("mysql -uroot -pS3cretPassw0rd db", "S3cretPassw0rd"),
                            ("login --password S3cretPassw0rd now", "S3cretPassw0rd"),
                            ("Authorization: Basic " + "YWxhZGRpbjpvcGVuc2VzYW1l", "YWxhZGRpbjpvcGVuc2VzYW1l"),
                            ('password = "correct horse battery staple"', "correct horse"),
                            ("sk_" + "live_" + "a1b2c3d4e5f6g7h8i9j0", "a1b2c3d4e5f6g7h8i9j0"),
                            ("glpat" + "-" + "abcdefghij0123456789xy", "abcdefghij0123456789xy"),
                            ("hf" + "_" + "A" * 34, "A" * 34)):
            self.assertNotIn(secret, redact(raw), raw)

    def test_ordinary_text_untouched(self):
        text = ("The token budget is 4096 and the password policy needs 12 characters; see docs/keys.md; "
                "run mkdir -p ~/acme-data and open https://example.org/a:b")
        self.assertEqual(redact(text), text)


if __name__ == "__main__":
    unittest.main()
