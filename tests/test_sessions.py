"""session_search: indexing of Claude Code / Codex transcripts and the search over them
(unittest-style; pytest collects it). Every test builds its own fake home in a temp dir."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_harness.mcp import sessions as S  # noqa: E402
from agent_harness.mcp.server import Server  # noqa: E402


def cl(kind, content, ts="2026-09-20T10:00:00.000Z", **extra):
    e = {"type": kind, "timestamp": ts, "cwd": "/work/proj", "sessionId": "s",
         "message": {"role": kind, "content": content}}
    e.update(extra)
    return json.dumps(e)


def cx(kind, payload, ts="2026-09-21T09:00:00.000Z"):
    return json.dumps({"timestamp": ts, "type": kind, "payload": payload})


def cx_msg(role, text, ts="2026-09-21T09:00:05.000Z"):
    t = "input_text" if role != "assistant" else "output_text"
    return cx("response_item", {"type": "message", "role": role, "content": [{"type": t, "text": text}]}, ts)


class Base(unittest.TestCase):
    fts = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.hh = self.home / ".agent-harness"
        self.cproj = self.home / ".claude" / "projects" / "-work-proj"
        self.cproj.mkdir(parents=True)
        self.xdir = self.home / ".codex" / "sessions" / "2026" / "09" / "21"
        self.xdir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def sess(self, **kw):
        s = S.Sessions(home=self.hh, user_home=self.home, fts=self.fts, **kw)
        self.addCleanup(s.close)
        return s

    def claude_file(self, name, lines, mode="w"):
        p = self.cproj / (name + ".jsonl")
        with open(p, mode, encoding="utf-8") as f:
            f.write("".join(ln + "\n" for ln in lines))
        return p

    def texts(self, res):
        return [r["text"] for r in res["results"]]


class IndexingTest(Base):
    def test_claude_user_and_assistant_words_only(self):
        self.claude_file("aaaa1111-2222", [
            cl("user", "Which partition key for the kestrel panel?"),
            cl("assistant", [{"type": "thinking", "thinking": "kestrel thinking secret plan"},
                             {"type": "text", "text": "Use date as the kestrel partition key."},
                             {"type": "tool_use", "name": "Bash", "input": {"command": "echo kestrel tooluse"}}]),
            cl("user", [{"type": "tool_result", "content": "kestrel tool output"}]),
            cl("user", "kestrel meta line", isMeta=True),
            cl("user", "kestrel sidechain line", isSidechain=True),
            cl("user", "<command-name>/kestrel</command-name>"),
            cl("user", "<system-reminder>kestrel reminder</system-reminder>real kestrel question"),
        ])
        got = self.texts(self.sess().search("kestrel", k=10))
        self.assertIn("Which partition key for the kestrel panel?", got)
        self.assertIn("Use date as the kestrel partition key.", got)
        self.assertIn("real kestrel question", got)
        for bad in ("thinking", "tooluse", "tool output", "meta line", "sidechain", "/kestrel", "reminder"):
            self.assertFalse(any(bad in t for t in got), bad)

    def test_codex_session_meta_and_injected_text(self):
        (self.xdir / "rollout-2026-09-21T09-00-00-abcd.jsonl").write_text("\n".join([
            cx("session_meta", {"id": "01a0beef-1234", "cwd": "/work/other"}),
            cx_msg("developer", "heron developer rules"),
            cx_msg("user", "# AGENTS.md instructions\n\n<INSTRUCTIONS>heron rules</INSTRUCTIONS>"),
            cx_msg("user", "<environment_context>\n<cwd>/heron</cwd>\n</environment_context>"),
            cx_msg("user", "set the heron universe to CSI500"),
            cx_msg("assistant", "Done: the heron universe is CSI500."),
        ]) + "\n")
        res = self.sess().search("heron universe", k=10)
        self.assertEqual(sorted(self.texts(res)), ["Done: the heron universe is CSI500.",
                                                   "set the heron universe to CSI500"])
        r = res["results"][0]
        self.assertEqual((r["tool"], r["session"], r["project"]), ("codex", "01a0beef", "/work/other"))
        self.assertEqual(r["date"][:10], "2026-09-21")
        self.assertEqual(self.sess().search("heron developer rules")["results"], [])

    def test_incremental_append_and_partial_line(self):
        p = self.claude_file("bbbb", [cl("user", "first osprey message")])
        s = self.sess()
        self.assertEqual(len(s.search("osprey")["results"]), 1)
        with open(p, "a", encoding="utf-8") as f:
            f.write(cl("assistant", [{"type": "text", "text": "second osprey reply"}]) + "\n")
            f.write(cl("user", "third osprey half")[:40])          # a line still being written
        got = self.texts(s.search("osprey", k=10))
        self.assertEqual(sorted(got), ["first osprey message", "second osprey reply"])   # no duplicate, no half line
        with open(p, "a", encoding="utf-8") as f:
            f.write(cl("user", "third osprey half")[40:] + "\n")
        self.assertIn("third osprey half", self.texts(s.search("osprey", k=10)))

    def test_rewritten_file_is_read_again(self):
        self.claude_file("cccc", [cl("user", "plover alpha one"), cl("user", "plover alpha two")])
        s = self.sess()
        self.assertEqual(len(s.search("plover alpha")["results"]), 2)
        self.claude_file("cccc", [cl("user", "plover beta")])        # shorter: truncated and rewritten
        self.assertEqual(self.texts(s.search("plover", k=10)), ["plover beta"])

    def test_vanished_transcript_is_forgotten(self):
        p = self.claude_file("dddd", [cl("user", "avocet decision")])
        s = self.sess()
        self.assertEqual(len(s.search("avocet")["results"]), 1)
        p.unlink()
        self.assertEqual(s.search("avocet")["results"], [])
        self.assertEqual(s.con.execute("SELECT COUNT(*) FROM sess_msgs").fetchone()[0], 0)

    def test_subagent_transcripts_are_not_sessions(self):
        sub = self.cproj / "eeee" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(cl("user", "dunlin subagent prompt") + "\n")
        self.assertEqual(self.sess().search("dunlin")["results"], [])

    def test_secrets_never_reach_the_index(self):
        key = "sk-" + "ant-" + "Q" * 30
        self.claude_file("ffff", [cl("user", "godwit: my key is %s and API_KEY=zz99zz99zz99" % key)])
        s = self.sess()
        res = s.search("godwit key")
        self.assertEqual(len(res["results"]), 1)
        dump = "\n".join(s.con.iterdump())
        self.assertNotIn(key, dump)
        self.assertNotIn("zz99zz99zz99", dump)
        self.assertIn("[REDACTED]", res["results"][0]["text"])

    def test_only_own_files_and_no_symlinks(self):
        outside = Path(self.tmp.name) / "someone-else.jsonl"
        outside.write_text(cl("user", "curlew private words") + "\n")
        os.symlink(str(outside), str(self.cproj / "gggg.jsonl"))
        self.assertEqual(self.sess().search("curlew")["results"], [], "a symlinked transcript was read")
        self.claude_file("hhhh", [cl("user", "curlew own words")])
        with mock.patch.object(S.os, "getuid", return_value=os.getuid() + 12345):
            self.assertEqual(self.sess().search("curlew")["results"], [], "a file of another uid was read")
        self.assertEqual(len(self.sess().search("curlew")["results"]), 1)   # control: own file is read

    def test_cap_drops_the_oldest_sessions(self):
        self.claude_file("old1", [cl("user", "knot old " + "x" * 400, ts="2026-01-01T00:00:00Z")])
        self.claude_file("new1", [cl("user", "knot new " + "y" * 400, ts="2026-09-01T00:00:00Z")])
        got = self.texts(self.sess(cap_bytes=600).search("knot", k=10))
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("knot new"))

    def test_budget_leaves_work_for_the_next_call(self):
        for i in range(3):
            self.claude_file("b%d" % i, [cl("user", "sanderling %d" % i)])
        s = self.sess()
        first = s.search("sanderling", k=10, budget_s=-1)
        self.assertEqual(first["pending"], 3)
        self.assertIn("not indexed yet", S.render(first))
        done = s.search("sanderling", k=10)
        self.assertEqual((done["pending"], len(done["results"])), (0, 3))


class SearchTest(Base):
    def test_a_shared_common_word_is_not_a_match(self):
        self.claude_file("iiii", [cl("user", "the server restart went fine yesterday")])
        s = self.sess()
        self.assertEqual(s.search("which bar size did we choose for the server study")["results"], [])
        self.assertEqual(len(s.search("server restart")["results"]), 1)       # control

    def test_sessions_started_after_before_are_ignored(self):
        self.claude_file("jjjj", [cl("user", "turnstone earlier talk", ts="2026-09-20T10:00:00Z")])
        self.claude_file("kkkk", [cl("user", "turnstone this very talk", ts="2026-09-30T10:00:00Z")])
        cut = S._epoch("2026-09-25T00:00:00Z")
        self.assertEqual(self.texts(self.sess().search("turnstone", before=cut)), ["turnstone earlier talk"])

    def test_a_resumed_transcript_keeps_its_past_but_not_the_asking_turn(self):
        self.claude_file("resu", [cl("user", "stilt old decision", ts="2026-09-20T10:00:00Z"),
                                  cl("user", "stilt what did we decide", ts="2026-09-30T10:00:00Z")])
        cut = S._epoch("2026-09-25T00:00:00Z")
        self.assertEqual(self.texts(self.sess().search("stilt", before=cut)), ["stilt old decision"])

    def test_two_servers_sharing_the_index_store_a_message_once(self):
        self.claude_file("race", [cl("user", "phalarope once")])
        a, b = self.sess(), self.sess()
        real = S.Sessions.files

        def files_then_other_server_syncs(self_):
            out = real(self_)
            if self_ is a:
                b.sync()                   # the other session's server reads the same file in between
            return out
        with mock.patch.object(S.Sessions, "files", files_then_other_server_syncs):
            a.sync()
        self.assertEqual(len(a.search("phalarope")["results"]), 1)
        self.assertEqual(a.con.execute("SELECT COUNT(*) FROM sess_msgs").fetchone()[0], 1)

    def test_a_secret_across_the_length_cut_is_not_half_stored(self):
        tokn = "gh" + "p_" + "A" * 36
        self.claude_file("cut", [cl("user", "grebe " + "x " * ((S.MSG_CHARS - 20) // 2) + tokn)])
        s = self.sess()
        s.sync()
        self.assertNotIn("ghp_", "\n".join(s.con.iterdump()))

    def test_filter_by_session_prefix(self):
        self.claude_file("aaaa0000-1", [cl("user", "whimbrel one")])
        self.claude_file("bbbb0000-2", [cl("user", "whimbrel two")])
        self.assertEqual(self.texts(self.sess().search("whimbrel", session="bbbb")), ["whimbrel two"])

    def test_excerpt_is_short_and_centred(self):
        self.claude_file("llll", [cl("assistant", [{"type": "text", "text": "a " * 500 + "the dotterel decision"}])])
        r = self.sess().search("dotterel")["results"][0]
        self.assertLessEqual(len(r["text"]), S.EXCERPT_CHARS)
        self.assertIn("dotterel", r["text"])


class NoFtsSearchTest(SearchTest):
    fts = False


class ServerToolTest(Base):
    def test_session_search_through_the_server_skips_its_own_session(self):
        self.claude_file("mmmm", [cl("user", "ruff decided: bar size 7 minutes", ts="2026-09-20T10:00:00Z")])
        srv = Server(home=self.hh)
        with mock.patch.object(S.Path, "home", return_value=self.home):
            out = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "session_search", "arguments": {"query": "ruff bar size"}}})
        text = out["result"]["content"][0]["text"]
        self.assertIn("bar size 7 minutes", text)
        self.assertIn("claude-code mmmm", text)

    def test_the_asking_session_is_not_its_own_hit(self):
        import datetime
        srv = Server(home=self.hh)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.claude_file("nnnn", [cl("user", "what did we decide about the ruff universe", ts=now)])
        with mock.patch.object(S.Path, "home", return_value=self.home):
            out = srv.call_tool("session_search", {"query": "ruff universe decide"})
        self.assertEqual(out["results"], [])


if __name__ == "__main__":
    unittest.main()
