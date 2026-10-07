"""The eval history is public: its provenance notes may name only files that ship in this repository.

Found 2026-10-07: rows recorded the path of the generator inside a private repository. A note that names a
file path must name one that exists here; anything else says where private infrastructure lives.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "eval" / "results" / "history.jsonl"
PATH_RE = re.compile(r"(?<![\w.])((?:[\w.-]+/)+[\w.-]+\.(?:py|sh|md|json|jsonl|toml|ya?ml))\b")


def foreign_paths(text):
    return [p for p in PATH_RE.findall(text) if not (ROOT / p).exists()]


class TestHistoryProvenance(unittest.TestCase):
    def test_every_path_a_history_note_names_ships_in_this_repo(self):
        rows = [json.loads(ln) for ln in HISTORY.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertTrue(rows, "the history is empty")
        bad = {r["version"] + "/" + r["tool"]: foreign_paths(json.dumps(r)) for r in rows}
        self.assertEqual({k: v for k, v in bad.items() if v}, {})

    def test_the_check_catches_a_foreign_path_and_passes_a_local_one(self):
        self.assertEqual(foreign_paths("measured by some-private-repo/ops/eval/history.py"),
                         ["some-private-repo/ops/eval/history.py"])
        self.assertEqual(foreign_paths("graded by eval/quality_grade.py"), [])


if __name__ == "__main__":
    unittest.main()
