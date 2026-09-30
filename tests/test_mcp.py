"""Tests for the harness MCP server, knowledge index and memory (unittest-style; pytest collects them too)."""
import json
import re
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness.mcp import kb as kbmod  # noqa: E402
from agent_harness.mcp.kb import KB, split_sections  # noqa: E402
from agent_harness.mcp import state as statemod  # noqa: E402
from agent_harness.mcp.memory import Memory  # noqa: E402
from agent_harness.mcp.state import State, project_key, project_root  # noqa: E402
from agent_harness.mcp.server import TOOLS, Server  # noqa: E402

SERVER = SRC / "agent_harness" / "mcp" / "server.py"

_SYL = "ba be bi bo bu da de di do du ka ke ki ko ku la le li lo lu ma me mi mo mu na ne ni no nu ra re ri ro ru".split()
_R = random.Random(99)
VOCAB = ("service config deploy cluster node queue worker cache request response latency metric alert "
         "session user group policy rule network route proxy gateway storage volume disk "
         "schedule job retry timeout limit quota build test release branch commit review merge image "
         "container runtime package module import export trace sample report dashboard owner team "
         "access audit archive index search").split() + ["".join(_R.choice(_SYL) for _ in range(3))
                                                         for _ in range(400)]
# Sections that share words with the question without answering it.
DISTRACTORS = [
    "## Backup schedule\n\nThe backup job runs nightly and writes a snapshot to cold storage.\n",
    "## API key storage\n\nStore each API key in the secret manager; never commit a key.\n",
    "## Log rotation\n\nLogs rotate daily; rotated files are compressed and kept for a week.\n",
    "## TLS certificates\n\nCertificates renew automatically; encryption at rest uses the platform default.\n",
]
TOPICS = ["networking", "storage", "deploys", "monitoring", "access", "builds", "queues", "caching",
          "databases", "releases"]
TARGET_ID = "corpus/ops/backups.md#rotating-the-backup-encryption-key"
QUESTION = "How do I rotate the backup encryption key?"


def _prose(rng, words):
    out = []
    for _ in range(words // 12):
        s = " ".join(rng.choice(VOCAB) for _ in range(12))
        out.append(s.capitalize() + ".")
    return " ".join(out)


def make_corpus(base: Path) -> Path:
    """~40 Markdown files with #/##/### headings; one section answers QUESTION; many distractors."""
    rng = random.Random(1234)
    root = base / "corpus"
    for i in range(39):
        topic = TOPICS[i % len(TOPICS)]
        lines = ["# %s guide %d\n\n%s\n" % (topic.title(), i, _prose(rng, 60))]
        for j in range(3):
            lines.append("## %s part %d\n\n%s\n" % (topic.title(), j, _prose(rng, 150)))
            lines.append("### Details %d.%d\n\n%s\n" % (i, j, _prose(rng, 90)))
        if i % 3 == 0:
            lines.append(DISTRACTORS[(i // 3) % len(DISTRACTORS)] + _prose(rng, 60) + "\n")
        p = root / topic / ("guide-%02d.md" % i)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines), encoding="utf-8")
    target = (
        "# Backups\n\nNightly backups run from the scheduler. %s\n\n"
        "## Restoring a snapshot\n\n%s\n\n"
        "## Rotating the backup encryption key\n\n"
        "Rotate the backup encryption key every 90 days: generate a new key with `backupctl key new`, "
        "re-encrypt the latest snapshot with `backupctl rekey --latest`, then retire the old key with "
        "`backupctl key retire OLD_ID`. Keep the old key until the first restore test with the new key passes.\n\n"
        "## Backup retention\n\n%s\n" % (_prose(rng, 80), _prose(rng, 150), _prose(rng, 150)))
    (root / "ops").mkdir(parents=True, exist_ok=True)
    (root / "ops" / "backups.md").write_text(target, encoding="utf-8")
    return root


def index_ids(index: str):
    """Full section ids from the compact index: `<page>: #slug #slug ...` lines."""
    ids = []
    for line in index.splitlines():
        m = re.match(r"^(\S+): (#\S+(?: #\S+)*)$", line)
        if m:
            ids += [m.group(1) + slug for slug in m.group(2).split()]
    return ids


def corpus_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*.md"))


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.home = self.tmp / "harness-home"

    def tearDown(self):
        self._td.cleanup()


class SplitTests(unittest.TestCase):
    def test_ids_stable_with_collision_suffix_and_parts(self):
        text = "intro\n# Title\nA\n## Setup\nx\n```\n# not a heading\n```\n## Setup\ny\n### Deep\nz\n#### h4 stays\n"
        text += "## Long\n" + ("word " * 200 + "\n\n") * 10
        secs = split_sections("d/f.md", text)
        ids = [s["id"] for s in secs]
        self.assertEqual(ids[:5], ["d/f.md#top", "d/f.md#title", "d/f.md#setup", "d/f.md#setup-1", "d/f.md#deep"])
        self.assertIn("# not a heading", secs[2]["body"])
        self.assertIn("#### h4 stays", secs[4]["body"])
        longs = [s for s in secs if s["id"].startswith("d/f.md#long")]
        self.assertGreater(len(longs), 1)
        self.assertEqual(longs[1]["id"], "d/f.md#long-p2")
        self.assertTrue(all(s["bytes"] <= kbmod.PART_BYTES for s in longs))
        self.assertEqual(split_sections("d/f.md", text), secs)  # deterministic


class ToolsInProcessTests(TmpCase):
    """(a) every tool called in-process through Server.call_tool / handle."""

    def test_each_tool(self):
        docs = self.tmp / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("# Alpha\nintro\n## Install\nrun the installer with --yes\n## Usage\nuse it\n")
        srv = Server(home=self.home, kb_paths=[docs])
        self.assertEqual({t["name"] for t in TOOLS},
                         {"kb_search", "kb_get", "kb_toc", "mem_add", "mem_search", "mem_forget",
                          "lesson_add", "lesson_search", "state_save", "state_load", "session_note",
                          "session_recent", "session_search", "skill_manage", "run_checks"})
        from agent_harness.mcp.server import tool_list
        self.assertNotIn("run_checks", [t["name"] for t in tool_list()])   # off by default (v0.1.1 eval)
        for t in TOOLS:
            self.assertTrue(t["description"] and "\n" not in t["description"])

        idx = srv.kb.build_index()
        self.assertIn("\ndocs/a.md: #install #usage\n", idx)
        self.assertEqual(index_ids(idx), ["docs/a.md#install", "docs/a.md#usage"])
        page = srv.call_tool("kb_get", {"id": "docs/a.md"})       # a page name lists its sections
        self.assertIn("docs/a.md#install", page["text"])
        self.assertIn("docs/a.md#usage", page["text"])
        self.assertNotIn("run the installer", page["text"])
        top = srv.call_tool("kb_get", {"id": "docs/a.md#alpha"})  # the page's first section: body + list
        self.assertIn("intro", top["text"])
        self.assertIn("docs/a.md#install", top["text"])
        hits = srv.call_tool("kb_search", {"query": "installer"})
        self.assertEqual(hits[0]["id"], "docs/a.md#install")
        self.assertEqual(set(hits[0]), {"id", "title", "path", "snippet", "bytes", "score"})
        self.assertLessEqual(len(hits[0]["snippet"]), 200)
        got = srv.call_tool("kb_get", {"id": "docs/a.md#install"})
        self.assertIn("run the installer", got["text"])
        self.assertEqual(got["bytes"], len(got["text"].encode()))
        toc = srv.call_tool("kb_toc", {"path": "docs/a.md"})
        self.assertEqual(toc[0]["headings"][0]["id"], "docs/a.md#alpha")
        self.assertEqual([c["id"] for c in toc[0]["headings"][0]["children"]],
                         ["docs/a.md#install", "docs/a.md#usage"])
        self.assertNotIn("text", json.dumps(toc))
        self.assertEqual(srv.call_tool("kb_toc", {})[0]["path"], "docs/a.md")

        m = srv.call_tool("mem_add", {"text": "The user prefers tabs over spaces", "tags": ["style"]})
        self.assertFalse(m["duplicate"])
        found = srv.call_tool("mem_search", {"query": "tabs"})
        self.assertEqual(found[0]["id"], m["id"])
        self.assertEqual(set(found[0]), {"id", "text", "tags", "uses"})
        self.assertEqual(srv.call_tool("mem_forget", {"id": m["id"]}), {"ok": True})
        self.assertEqual(srv.call_tool("mem_search", {"query": "tabs"}), [])
        l = srv.call_tool("lesson_add", {"mistake": "ran tests against real HOME", "fix": "pass a temp home",
                                         "trigger": "writing installer tests"})
        self.assertTrue(l["id"].startswith("l-"))
        ls = srv.call_tool("lesson_search", {"query": "installer tests"})
        self.assertEqual(ls[0]["fix"], "pass a temp home")

        srv._state = State(home=self.home, project=self.tmp)
        self.assertEqual(srv.call_tool("state_load", {}), "no saved state")
        self.assertTrue(srv.call_tool("state_save", {"text": "## Goal\nship it\n"})["ok"])
        self.assertIn("ship it", srv.call_tool("state_load", {}))
        srv.call_tool("session_note", {"summary": "built the MCP server"})
        self.assertEqual(srv.call_tool("session_recent", {"k": 1})[0]["summary"], "built the MCP server")
        self.assertEqual(srv.call_tool("mem_search", {"query": "installer tests", "min_score": 0.99}), [])

        # JSON-RPC surface, in process
        r = srv.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                        "params": {"name": "kb_get", "arguments": {"id": "nope#x"}}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("kb_search", r["result"]["content"][0]["text"])
        r = srv.handle({"jsonrpc": "2.0", "id": 10, "method": "bogus"})
        self.assertEqual(r["error"]["code"], -32601)
        self.assertIsNone(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(srv.handle({"jsonrpc": "2.0", "id": 11, "method": "ping"})["result"], {})


class SubprocessProtocolTests(TmpCase):
    """(b) speak newline-delimited JSON-RPC to server.py over stdin/stdout."""

    def _run(self, messages):
        docs = self.tmp / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "guide.md").write_text("# Guide\n## Proxy settings\nSet HTTPS_PROXY before running warmup.\n")
        env = dict(os.environ, HARNESS_HOME=str(self.home), HARNESS_KB_PATHS=str(docs))
        p = subprocess.run([sys.executable, str(SERVER)], input="".join(json.dumps(m) + "\n" for m in messages),
                           capture_output=True, text=True, env=env, cwd=str(self.tmp), timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return [json.loads(l) for l in p.stdout.splitlines()]

    def test_initialize_list_search_get(self):
        out = self._run([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "kb_search", "arguments": {"query": "proxy warmup"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "kb_get", "arguments": {"id": "docs/guide.md#proxy-settings"}}},
        ])
        self.assertEqual([m["id"] for m in out], [1, 2, 3, 4])  # the notification got no reply
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "harness")
        self.assertIn("tools", out[0]["result"]["capabilities"])
        self.assertIn("kb_get", [t["name"] for t in out[1]["result"]["tools"]])
        hits = json.loads(out[2]["result"]["content"][0]["text"])
        self.assertEqual(hits[0]["id"], "docs/guide.md#proxy-settings")
        self.assertIn("HTTPS_PROXY", out[3]["result"]["content"][0]["text"])

    def test_old_protocol_version_and_parse_error(self):
        env = dict(os.environ, HARNESS_HOME=str(self.home), HARNESS_KB_PATHS=str(self.tmp))
        p = subprocess.run([sys.executable, str(SERVER)],
                           input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":'
                                 '"2024-11-05"}}\nnot json\n', capture_output=True, text=True, env=env, timeout=60)
        a, b = [json.loads(l) for l in p.stdout.splitlines()]
        self.assertEqual(a["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(b["error"]["code"], -32700)


class PartialRetrievalTests(TmpCase):
    """(c) answering a question reads <5% of the corpus, with the right section in the top 3."""

    def _measure(self, fts):
        root = make_corpus(self.tmp)
        total = corpus_bytes(root)
        kb = KB(home=self.home, paths=[root], fts=fts, db_path=self.tmp / ("i%s.sqlite" % fts))
        hits = kb.search(QUESTION, k=5)
        top3 = [h["id"] for h in hits[:3]]
        self.assertIn(TARGET_ID, top3)
        read = len(Server.render("kb_search", hits).encode())
        # Conservative: the agent fetches every one of the top 3, not just the right one.
        for sid in top3:
            read += len(Server.render("kb_get", kb.get(sid)).encode())
        self.assertIn("backupctl rekey", kb.get(TARGET_ID)["text"])
        kb.close()
        return read, total

    def test_bytes_read_under_5_percent_fts5(self):
        read, total = self._measure(None)
        self.assertGreater(total, 150_000)
        pct = 100.0 * read / total
        print("\n[partial retrieval, FTS5] read %d of %d bytes = %.2f%%" % (read, total, pct))
        self.assertLess(pct, 5.0)

    def test_bytes_read_under_5_percent_python_bm25(self):
        read, total = self._measure(False)
        pct = 100.0 * read / total
        print("\n[partial retrieval, pure-Python BM25] read %d of %d bytes = %.2f%%" % (read, total, pct))
        self.assertLess(pct, 5.0)

    def test_index_fits_cap_and_every_id_resolves(self):
        root = make_corpus(self.tmp)
        kb = KB(home=self.home, paths=[root])
        full = kb.build_index(10 ** 7)
        ids = index_ids(full)
        lvl12 = kb.con.execute("SELECT COUNT(*) FROM kb_sections WHERE level = 2").fetchone()[0]
        self.assertEqual(len(ids), lvl12)                     # every level-2 section, no ### ones
        self.assertFalse(any("details" in i for i in ids))
        self.assertIn(TARGET_ID, ids)
        for sid in ids:
            self.assertEqual(kb.get(sid)["id"], sid)
        self.assertEqual(kb.get("#rotating-the-backup-encryption-key")["id"], TARGET_ID)  # bare unique slug
        det = kb.build_index(10 ** 7, detail=["corpus/ops/*"])
        self.assertEqual({i.split("#")[0] for i in index_ids(det)}, {"corpus/ops/backups.md"})
        self.assertIn("Other pages: ", det)
        self.assertEqual(det.count(".md"), 40 + 1 - 1)         # every page named once
        trimmed = kb.build_index(3000)
        self.assertLessEqual(len(trimmed.encode()), 3000)
        for sid in index_ids(trimmed):
            self.assertEqual(kb.get(sid)["id"], sid)
        tiny = kb.build_index(600)
        self.assertLessEqual(len(tiny.encode()), 600)
        self.assertIn("more: kb_toc", tiny)
        print("\n[index] %d bytes full, %d with 1 detailed page, %.2f%% of corpus" % (
            len(full.encode()), len(det.encode()), 100.0 * len(det.encode()) / corpus_bytes(root)))
        kb.close()

    def test_index_lists_a_duplicate_page_once_and_excludes(self):
        a, b = self.tmp / "p", self.tmp / "w"
        a.mkdir()
        b.mkdir()
        body = "# Server\n## GPUs\nfour\n"
        (a / "SERVER.md").write_text(body)
        (b / "SERVER.md").write_text(body)
        (a / "rules.md").write_text("# Rules\n## One\nx\n")
        kb = KB(home=self.home, paths=[a / "SERVER.md", a / "rules.md", b])
        idx = kb.build_index(exclude=["rules.md"])
        self.assertEqual(index_ids(idx), ["SERVER.md#gpus"])
        self.assertNotIn("w/SERVER.md", idx)
        self.assertNotIn("rules.md", idx)
        kb.close()


class MemoryTests(TmpCase):
    """(d) dedupe, caps + archive, lessons round-trip."""

    def test_dedupe_bumps_uses(self):
        m = Memory(home=self.home, project=self.tmp)
        a = m.mem_add("The staging database lives on the second server, port 5433.")
        b = m.mem_add("the staging database lives on the second server port 5433")
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(b["uses"], 1)
        c = m.mem_add("The production database lives on the first server.")
        self.assertFalse(c["duplicate"])
        self.assertEqual(len(list((self.home / "memory" / "user").glob("*.json"))), 2)

    def test_search_increments_uses_and_file_is_readable(self):
        m = Memory(home=self.home, project=self.tmp)
        a = m.mem_add("Deploys need the release tag first", tags=["deploy"])
        self.assertEqual(m.mem_search("release tag")[0]["uses"], 1)
        self.assertEqual(m.mem_search("deploy")[0]["uses"], 2)  # tags are searchable
        item = json.loads((self.home / "memory" / "user" / (a["id"] + ".json")).read_text())
        self.assertEqual(item["uses"], 2)
        self.assertEqual(item["tags"], ["deploy"])

    def test_caps_archive_least_used_oldest(self):
        m = Memory(home=self.home, project=self.tmp, caps={"memory": 3, "lessons": 2})
        ids = [m.mem_add("fact number %s about topic %s" % (w, w))["id"] for w in ("alpha", "bravo", "charlie")]
        m.mem_search("alpha")  # alpha now used -> survives
        r = m.mem_add("fact number delta about topic delta")
        self.assertEqual(r["archived"], [ids[1]])  # bravo: unused and oldest
        r = m.mem_add("fact number echo about topic echo")
        self.assertEqual(r["archived"], [ids[2]])
        live = {p.stem for p in (self.home / "memory" / "user").glob("*.json")}
        self.assertEqual(len(live), 3)
        self.assertIn(ids[0], live)
        archived = {p.stem for p in (self.home / "memory" / "user" / "archive").glob("*.json")}
        self.assertEqual(archived, {ids[1], ids[2]})  # archived, not deleted
        self.assertEqual(m.mem_search("bravo"), [])
        # lessons cap
        for i in range(3):
            m.lesson_add("mistake %s" % "xyz"[i] * 3, "fix %d" % i, "trigger %d" % i)
        self.assertEqual(len(list((self.home / "lessons").glob("*.json"))), 2)
        self.assertEqual(len(list((self.home / "lessons" / "archive").glob("*.json"))), 1)

    def test_lessons_round_trip_and_new_process_sees_them(self):
        m = Memory(home=self.home, project=self.tmp)
        lid = m.lesson_add(mistake="edited config and checked in two commands",
                           fix="edit and check in one command", trigger="changing a config file")["id"]
        dup = m.lesson_add(mistake="edited config and checked in two commands",
                           fix="edit and check in one command", trigger="changing a config file")
        self.assertEqual(dup["id"], lid)
        m.close()
        m2 = Memory(home=self.home, project=self.tmp, fts=False)  # fresh instance, fallback search
        got = m2.lesson_search("config file change")
        self.assertEqual(got[0]["id"], lid)
        self.assertEqual(got[0]["fix"], "edit and check in one command")
        self.assertEqual(json.loads((self.home / "lessons" / (lid + ".json")).read_text())["mistake"],
                         "edited config and checked in two commands")

    def test_project_scope_only_visible_in_that_project(self):
        a = Memory(home=self.home, project=self.tmp / "p1")
        a.mem_add("this repo uses poetry for builds", scope="project")
        self.assertEqual(len(a.mem_search("poetry")), 1)
        b = Memory(home=self.home, project=self.tmp / "p2")
        self.assertEqual(b.mem_search("poetry"), [])

    def test_hand_edited_file_is_reindexed(self):
        m = Memory(home=self.home, project=self.tmp)
        a = m.mem_add("original wording about kettles")
        f = self.home / "memory" / "user" / (a["id"] + ".json")
        item = json.loads(f.read_text())
        item["text"] = "revised wording about teapots"
        f.write_text(json.dumps(item))
        os.utime(f, (time.time() + 5, time.time() + 5))
        self.assertEqual(m.mem_search("teapots")[0]["id"], a["id"])
        self.assertEqual(m.mem_search("kettles"), [])


MEM_FACTS = [("The staging database lives on the second server, port 5433.", []),
             ("The user prefers tabs over spaces in Python files.", ["style"]),
             ("Deploys need the release tag first", ["deploy"]),
             ("Never run the test suite against the real HOME directory; pass a temp home.", ["tests"]),
             ("The VPN interface must never be modified; read network state only.", ["network"]),
             ("Kaggle credentials live in an access_token file, not kaggle.json.", [])]


class RelevanceThresholdTests(TmpCase):
    """Weak matches return nothing; paraphrases still hit (calibration in kb.py / memory.py)."""

    def test_kb_threshold(self):
        root = make_corpus(self.tmp)
        for fts in (None, False):
            kb = KB(home=self.home, paths=[root], fts=fts, db_path=self.tmp / ("t%s.sqlite" % fts))
            for q in ("write a haiku about autumn leaves", "what is the capital of France",
                      "kubernetes pod eviction policy", "the second test failed"):
                self.assertEqual(kb.search(q), [], q)
            for q in ("how do I change the key used to encrypt backups",
                      "rotating encryption keys for backup snapshots"):
                self.assertEqual(kb.search(q)[0]["id"], TARGET_ID, q)
            self.assertEqual(kb.search(QUESTION, min_score=0.99), [])
            kb.close()

    def test_memory_threshold(self):
        for fts in (None, False):
            m = Memory(home=self.tmp / ("h%s" % fts), project=self.tmp, fts=fts)
            for text, tags in MEM_FACTS:
                m.mem_add(text, tags=tags)
            m.lesson_add("edited config and checked it in two commands", "edit and check in one command",
                         "changing a config file")
            for q in ("write a haiku about autumn leaves", "what is the capital of France",
                      "the second test failed"):
                self.assertEqual(m.mem_search(q), [], q)
                self.assertEqual(m.lesson_search(q), [], q)
            self.assertIn("5433", m.mem_search("which port does the staging db listen on?")[0]["text"])
            self.assertIn("access_token", m.mem_search("set up kaggle auth token")[0]["text"])
            self.assertEqual(m.lesson_search("I need to update the config file")[0]["fix"],
                             "edit and check in one command")
            m.close()


class StateTests(TmpCase):
    def _git_project(self):
        proj = self.tmp / "proj"
        (proj / ".git").mkdir(parents=True)
        (proj / "src" / "deep").mkdir(parents=True)
        return proj

    def test_round_trip_cap_versions(self):
        proj = self._git_project()
        st = State(home=self.home, project=proj)
        self.assertEqual(st.state_load(), "no saved state")
        self.assertIn("Goal", statemod.TEMPLATE)
        st.state_save("## Goal\nv0\n")
        self.assertEqual(st.state_load(), "## Goal\nv0\n")
        with self.assertRaises(ValueError) as cm:
            st.state_save("x" * 4097)
        self.assertIn("shorter", str(cm.exception))
        self.assertEqual(st.state_load(), "## Goal\nv0\n")  # refused save changed nothing
        for i in range(1, 14):
            st.state_save("## Goal\nv%d\n" % i)
        self.assertEqual(st.state_load(), "## Goal\nv13\n")
        vers = st.versions()
        self.assertEqual(len(vers), 10)
        self.assertEqual(vers[0].read_text(), "## Goal\nv12\n")  # newest previous version
        self.assertEqual(vers[-1].read_text(), "## Goal\nv3\n")

    def test_project_key_from_git_root(self):
        proj = self._git_project()
        self.assertEqual(project_root(proj / "src" / "deep"), proj.resolve())
        a = State(home=self.home, project=proj / "src" / "deep")
        a.state_save("from a subdir")
        self.assertEqual(State(home=self.home, project=proj).state_load(), "from a subdir")
        self.assertTrue((self.home / "state" / project_key(proj.resolve()) / "STATE.md").is_file())
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertEqual(project_root(plain), plain.resolve())
        self.assertNotEqual(project_key(plain.resolve()), project_key(proj.resolve()))

    def test_print_cli(self):
        proj = self._git_project()
        env = dict(os.environ, HARNESS_HOME=str(self.home), PYTHONPATH=str(SRC))
        cmd = [sys.executable, "-m", "agent_harness.mcp.state", "print", "--project", str(proj / "src")]
        empty = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((empty.returncode, empty.stdout), (0, ""))
        State(home=self.home, project=proj).state_save("## Next\nwrite the docs\n")
        out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("write the docs", out.stdout)

    def test_session_log_rotates(self):
        st = State(home=self.home, project=self.tmp)
        for i in range(205):
            st.session_note("note %d %s" % (i, "x" * 400))
        log = self.home / "sessions" / (project_key(project_root(self.tmp)) + ".log")
        lines = log.read_text().splitlines()
        self.assertEqual(len(lines), 200)
        self.assertIn("note 5 ", lines[0])
        recent = st.session_recent(3)
        self.assertEqual([r["summary"].split()[1] for r in recent], ["202", "203", "204"])
        self.assertLessEqual(len(recent[0]["summary"]), 300)


class DigestAndRecallTests(TmpCase):
    def _mem(self, **kw):
        return Memory(home=self.home, project=self.tmp, **kw)

    def test_empty_store_prints_nothing(self):
        self.assertEqual(self._mem().digest(), "")
        self.assertEqual(self._mem().recall("anything at all", "s1"), "")
        env = dict(os.environ, HARNESS_HOME=str(self.home), PYTHONPATH=str(SRC))
        for args, stdin in ((["digest", "--project", str(self.tmp)], ""),
                            (["recall", "--session", "s1", "--project", str(self.tmp)], "deploy the release")):
            p = subprocess.run([sys.executable, "-m", "agent_harness.mcp.memory"] + args, input=stdin,
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)

    def test_digest_pinned_order_cap_no_uses(self):
        m = self._mem()
        for text, tags in MEM_FACTS:
            m.mem_add(text, tags=tags)
        p1 = m.mem_add("Project facts: this repo is public; no hostnames in it.", scope="project", pin=True)
        u1 = m.mem_add("Answer in British English.", pin=True)
        m.lesson_add("a mistake", "a fix", "a trigger")
        State(home=self.home, project=self.tmp).state_save("## Goal\nx\n")
        d = m.digest()
        lines = d.splitlines()
        self.assertEqual(lines[1], "- Answer in British English.")           # pinned user facts first
        self.assertTrue(lines[2].startswith("- [project] Project facts"))    # then the project's pinned
        self.assertTrue(lines[3].startswith("8 facts, 1 lessons stored"))
        self.assertTrue(lines[4].startswith("open work: STATE.md saved "))
        self.assertEqual(len(lines), 5)                                      # no most-used / lessons dump
        self.assertNotIn("5433", d)
        for sid in (p1["id"], u1["id"]):
            self.assertEqual(json.loads((self.home / "memory" / ("project" if sid == p1["id"] else "user")
                                         / (sid + ".json")).read_text())["uses"], 0)
        small = m.digest(max_bytes=80)
        self.assertLessEqual(len(small.encode()), 80)
        self.assertIn("British English", small)  # pinned survive the cap first
        # pinned items always show, however many unpinned facts exist and however used they are
        for i in range(30):
            m.mem_add("filler fact %d about subject%d" % (i, i))
            m.mem_search("subject%d" % i)
        self.assertIn("British English", m.digest())

    def test_pinned_survive_cap_archiving(self):
        m = Memory(home=self.home, project=self.tmp, caps={"memory": 2})
        keep = m.mem_add("pinned fact about zebras", pin=True)["id"]
        for w in ("apples", "pears", "plums"):
            m.mem_add("fact about %s" % w)
        self.assertTrue((self.home / "memory" / "user" / (keep + ".json")).exists())

    def test_recall_relevant_once_per_session_within_cap(self):
        m = self._mem()
        for text, tags in MEM_FACTS:
            m.mem_add(text, tags=tags)
        m.lesson_add("deployed without the release tag", "tag the release before deploying", "deploying")
        self.assertEqual(m.recall("write a haiku about autumn leaves", "s1"), "")
        out = m.recall("how do I deploy the release?", "s1")
        self.assertIn("release tag first", out)
        self.assertIn("lesson: when deploying", out)
        self.assertEqual(m.recall("how do I deploy the release?", "s1"), "")   # already recalled
        self.assertIn("release tag first", m.recall("how do I deploy the release?", "s2"))  # new session
        capped = m.recall("deploy release tag staging database port tests home vpn network kaggle", "s3",
                          max_bytes=150)
        self.assertTrue(capped)
        self.assertLessEqual(len(capped.encode()), 150)
        # old session files are pruned after 7 days
        old = self.home / "sessions" / "recalled-ancient.txt"
        old.write_text("m-x\n")
        t = time.time() - 8 * 86400
        os.utime(old, (t, t))
        m.recall("anything", "s4")
        self.assertFalse(old.exists())

    def test_recall_cli(self):
        m = self._mem()
        m.mem_add("Deploys need the release tag first")
        m.close()
        env = dict(os.environ, HARNESS_HOME=str(self.home), PYTHONPATH=str(SRC))
        cmd = [sys.executable, "-m", "agent_harness.mcp.memory", "recall", "--session", "abc",
               "--project", str(self.tmp)]
        a = subprocess.run(cmd, input="deploy the release please", capture_output=True, text=True, env=env,
                           timeout=60)
        self.assertIn("release tag first", a.stdout, a.stderr)
        b = subprocess.run(cmd, input="deploy the release please", capture_output=True, text=True, env=env,
                           timeout=60)
        self.assertEqual(b.stdout, "")


class ProfileAndConflictTests(TmpCase):
    def test_default_paths_include_profile_folder_and_resolve_relative_kb_paths(self):
        prof = self.home / "profile"
        (prof / "wiki").mkdir(parents=True)
        (prof / "SERVER.md").write_text("# Server\n## GPU queue\nSubmit jobs with the zorbex scheduler.\n")
        (prof / "wiki" / "net.md").write_text("# Net\n## Proxy\nThe quuxproxy listens on the gateway.\n")
        fake_home = self.tmp / "userhome"
        (fake_home / "notes").mkdir(parents=True)
        (fake_home / "notes" / "n.md").write_text("# Notes\n## Tips\nflibber tips live here.\n")
        (prof / "profile.toml").write_text('kb_paths = ["wiki", "~/notes"]\n')
        project = self.tmp / "project"
        project.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_KB_PATHS"}
        env["HOME"] = str(fake_home)
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(env)
            paths = kbmod.default_kb_paths(self.home, project)
            self.assertIn(prof / "SERVER.md", paths)
            self.assertIn(prof / "wiki", paths)                      # relative -> profile folder
            self.assertIn(fake_home / "notes", paths)                # ~ expanded
            srv = Server(home=self.home)                             # the server's own defaults
            self.assertEqual(srv.call_tool("kb_search", {"query": "zorbex scheduler"})[0]["id"],
                             "SERVER.md#gpu-queue")                  # same id as the install-time index
            self.assertEqual(srv.call_tool("kb_search", {"query": "quuxproxy"})[0]["id"], "wiki/net.md#proxy")
            self.assertEqual(srv.call_tool("kb_search", {"query": "flibber tips"})[0]["id"], "notes/n.md#tips")
            self.assertIn("SERVER.md#gpu-queue", index_ids(srv.kb.build_index()))
            self.assertEqual(srv.call_tool("kb_get", {"id": "profile/SERVER.md#gpu-queue"})["id"],
                             "SERVER.md#gpu-queue")          # v0.1.0's printed form still resolves
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_project_docs_only_from_a_git_project_never_home(self):
        """Eval 2026-09-30: kb_toc listed the admin's private ~/docs, indexed from a session started in $HOME."""
        fake_home = self.tmp / "userhome"
        (fake_home / "docs").mkdir(parents=True)
        (fake_home / ".git").mkdir()                 # a dotfiles repo in $HOME is still not a project
        proj = fake_home / "work" / "proj"
        (proj / "docs").mkdir(parents=True)
        (proj / ".git").mkdir()
        loose = self.tmp / "scratch"
        loose.mkdir()
        old = dict(os.environ)
        try:
            os.environ.pop("HARNESS_KB_PATHS", None)
            os.environ["HOME"] = str(fake_home)
            self.assertNotIn(fake_home / "docs", kbmod.default_kb_paths(self.home, fake_home))
            self.assertNotIn(fake_home / "docs", kbmod.default_kb_paths(self.home, fake_home / "Desktop"))
            self.assertEqual(kbmod.default_kb_paths(self.home, loose), [])
            got = kbmod.default_kb_paths(self.home, proj / "docs")
            self.assertIn(proj.resolve() / "docs", got)
            self.assertNotIn(self.home / "content", got)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_memory_ignores_conflict_files(self):
        m = Memory(home=self.home, project=self.tmp)
        a = m.mem_add("The deploy window is Tuesday morning")
        d = self.home / "memory" / "user"
        conflict = json.loads((d / (a["id"] + ".json")).read_text())
        conflict["text"] = "The deploy window is Friday evening"
        (d / (a["id"] + ".conflict-laptop.json")).write_text(json.dumps(conflict))
        other = dict(conflict, id="m-otherhost00", text="zanzibar conflict only fact")
        (d / "m-otherhost00.conflict-desk.json").write_text(json.dumps(other))
        m2 = Memory(home=self.home, project=self.tmp)
        self.assertEqual([r["text"] for r in m2.mem_search("deploy window")], ["The deploy window is Tuesday morning"])
        self.assertEqual(m2.mem_search("zanzibar conflict fact"), [])
        self.assertIn("1 facts", m2.digest())
        self.assertTrue((d / (a["id"] + ".conflict-laptop.json")).exists())  # left for the user to merge

    def test_state_load_ignores_and_reports_conflicts(self):
        st = State(home=self.home, project=self.tmp)
        st.state_save("## Goal\nmine\n")
        d = self.home / "state" / project_key(project_root(self.tmp))
        (d / "STATE.conflict-laptop.md").write_text("## Goal\ntheirs\n")
        out = st.state_load()
        self.assertTrue(out.startswith("## Goal\nmine\n"))
        self.assertNotIn("theirs", out)
        self.assertIn("1 unresolved sync conflicts: ", out)
        self.assertIn("STATE.conflict-laptop.md", out)
        for i in range(12):
            st.state_save("## Goal\nv%d\n" % i)
        self.assertTrue((d / "STATE.conflict-laptop.md").exists())  # rotation never removes a conflict
        self.assertEqual(len(st.versions()), 10)
        self.assertTrue(all("conflict" not in v.name for v in st.versions()))


class IncrementalIndexTests(TmpCase):
    """(e) the index follows file changes (mtime), additions and deletions."""

    def _bump(self, p: Path):
        t = time.time() + 10
        os.utime(p, (t, t))

    def test_change_add_delete(self):
        for fts in (None, False):
            with self.subTest(fts=fts):
                docs = self.tmp / ("docs%s" % fts)
                docs.mkdir()
                f = docs / "a.md"
                f.write_text("# A\n## Colours\nthe widget is painted vermilion\n")
                kb = KB(home=self.home, paths=[docs], fts=fts, db_path=self.tmp / ("x%s.sqlite" % fts))
                self.assertEqual(kb.search("vermilion")[0]["id"], "%s/a.md#colours" % docs.name)
                self.assertEqual(kb.refresh()["changed"], 0)  # nothing changed -> nothing reindexed
                f.write_text("# A\n## Colours\nthe widget is painted ultramarine now\n")
                self._bump(f)
                self.assertEqual(kb.search("vermilion"), [])
                self.assertEqual(len(kb.search("ultramarine")), 1)
                g = docs / "b.md"
                g.write_text("# B\nsaffron notes\n")
                self.assertEqual(kb.search("saffron")[0]["path"], "%s/b.md" % docs.name)
                f.unlink()
                self.assertEqual(kb.search("ultramarine"), [])
                with self.assertRaises(KeyError):
                    kb.get("%s/a.md#colours" % docs.name)
                kb.close()


if __name__ == "__main__":
    unittest.main()


class UnreadablePaths(unittest.TestCase):
    """Found live on the server (2026-09-30): started from another user's home, the server walked up to
    unreadable .git/docs folders and every tool failed with PermissionError. Unreadable must mean absent."""

    def test_tools_work_from_an_unreadable_project(self):
        import os, stat, tempfile
        from pathlib import Path
        from agent_harness.mcp import kb, state
        if os.geteuid() == 0:
            self.skipTest("root reads everything")
        with tempfile.TemporaryDirectory() as t:
            locked = Path(t) / "locked"
            (locked / "docs").mkdir(parents=True)
            (locked / "docs" / "x.md").write_text("# secret\nbody\n")
            (locked / ".git").mkdir()
            hh = Path(t) / "hh"
            (hh / "profile").mkdir(parents=True)
            (hh / "profile" / "SERVER.md").write_text("# Machine\nfour GPUs here\n")
            proj = locked / "sub"
            proj.mkdir()
            os.chmod(locked, 0)
            try:
                paths = kb.default_kb_paths(hh, proj)
                self.assertTrue(any(str(p).endswith("SERVER.md") for p in paths))
                self.assertIsInstance(state.project_root(proj), Path)
                k = kb.KB(home=hh, paths=paths) if "home" in kb.KB.__init__.__code__.co_varnames else kb.KB(hh, paths)
                hits = k.search("four GPUs", 3)
                self.assertTrue(hits, "the readable descriptor must still be found")
            finally:
                os.chmod(locked, stat.S_IRWXU)


class DescriptorRanksFirst(unittest.TestCase):
    """Profile files (the machine descriptor) are authoritative: on an equal or close match they rank
    above a wiki page. The relevance threshold still uses the unboosted score."""

    def test_descriptor_wins_a_close_match(self):
        import tempfile
        from pathlib import Path
        from agent_harness.mcp import kb
        body = "# Machine\nThe research server has 4 GPUs with 96 GB of GPU memory per card.\n"
        with tempfile.TemporaryDirectory() as t:
            hh = Path(t) / "hh"; (hh / "profile").mkdir(parents=True)
            (hh / "profile" / "SERVER.md").write_text(body)
            wiki = Path(t) / "wiki"; wiki.mkdir()
            (wiki / "aaa-hardware.md").write_text(body)
            k = kb.KB(home=hh, paths=[wiki, hh / "profile" / "SERVER.md"])
            ids = [h["id"] for h in k.search("GPU memory per card", 3)]
            self.assertEqual(ids[0], "SERVER.md#machine", ids)


class NoticeTests(TmpCase):
    """HARNESS_HOME/notices.json: the first pending line joins the instructions of the first session
    of the day, so the assistant tells the user once; nothing pending, nothing added; uninstall clears it."""

    def _init(self):
        from agent_harness.mcp.server import Server
        r = Server(home=self.home).handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-06-18"}})
        return r["result"]["instructions"]

    def _pending(self, *lines):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "notices.json").write_text(json.dumps({"notices": list(lines)}))

    def test_nothing_pending_nothing_added(self):
        from agent_harness.mcp.server import INSTRUCTIONS
        self.assertEqual(self._init(), INSTRUCTIONS)          # no file
        self._pending()
        self.assertEqual(self._init(), INSTRUCTIONS)          # an empty list
        (self.home / "notices.json").write_text("{not json")
        self.assertEqual(self._init(), INSTRUCTIONS)          # unreadable: silence, never a crash
        self.assertFalse((self.home / ".notices-mcp-day").exists())

    def test_once_a_day(self):
        from agent_harness.mcp.server import INSTRUCTIONS
        line = "AI tools update available (v0.1.0 -> v0.2.0): run `igsl ai update`"
        self._pending(line, "a second notice waits its turn")
        first = self._init()
        self.assertTrue(first.startswith(INSTRUCTIONS))
        self.assertTrue(first.endswith(line), first)
        self.assertNotIn("second notice", first)
        self.assertEqual(self._init(), INSTRUCTIONS)          # the same day: not again
        (self.home / ".notices-mcp-day").write_text("2000-01-01\n")
        self.assertTrue(self._init().endswith(line))          # the next day: once more

    def test_subprocess_server_reads_harness_home(self):
        self._pending("announcement: maintenance on Sunday")
        env = dict(os.environ, HARNESS_HOME=str(self.home))
        msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        p = subprocess.run([sys.executable, str(SRC / "agent_harness" / "mcp" / "server.py")],
                           input=json.dumps(msg) + "\n", capture_output=True, text=True, env=env, timeout=60)
        self.assertIn("maintenance on Sunday", json.loads(p.stdout)["result"]["instructions"])

    def test_uninstall_removes_notices(self):
        from agent_harness import cli
        hh = self.tmp / "h" / ".agent-harness"
        hh.mkdir(parents=True)
        (hh / "notices.json").write_text('{"notices": ["x"]}')
        (hh / ".notices-mcp-day").write_text("2000-01-01\n")
        from unittest import mock
        with mock.patch.dict(os.environ, {"HARNESS_HOME": ""}):
            self.assertEqual(cli.main(["--home", str(self.tmp / "h"), "uninstall"]), 0)
        self.assertFalse((hh / "notices.json").exists())
        self.assertFalse((hh / ".notices-mcp-day").exists())
