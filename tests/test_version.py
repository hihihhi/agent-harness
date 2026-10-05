"""The package version is never behind the newest release tag in its own history (v0.3.0 was tagged while
__version__ still said 0.2.0, so the server's update notice and the eval both read the wrong release)."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_harness import __version__  # noqa: E402


def as_tuple(v):
    return tuple(int(x) for x in v.split("."))


class Version(unittest.TestCase):
    def test_version_not_behind_an_ancestor_tag(self):
        try:
            tags = subprocess.run(["git", "tag", "--merged", "HEAD", "--list", "v*"], cwd=ROOT, capture_output=True,
                                  text=True, timeout=20, check=True).stdout.split()
        except (OSError, subprocess.SubprocessError):
            self.skipTest("no git history here")
        rel = [t[1:] for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
        if not rel:
            self.skipTest("no release tag in this checkout")
        newest = max(rel, key=as_tuple)
        self.assertGreaterEqual(as_tuple(__version__), as_tuple(newest),
                                f"__version__ {__version__} is behind the release tag v{newest}")


if __name__ == "__main__":
    unittest.main()
