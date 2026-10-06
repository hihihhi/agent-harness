"""What the harness itself adds to every Claude Code request, measured from a real install.

The README's A/B table shows Claude's fixed context at 17,365 tokens plain and 37,173 with the harness.
Its own fine print says the conditions differed by more than the harness: the plain arm ran with no MCP
servers, the harness arms with the author's whole account, other plugins and connectors included. So the
table cannot say what the harness costs. This test measures exactly that, part by part, from a throwaway
install: about 8.8 KB (~2,450 tokens) in v0.3, an upper bound because it counts the MCP tool definitions,
which Claude Code defers until a tool is used. That is about an eighth of the 19,808-token gap.

The budget is the repo's own keep rule: token overhead within +15% of plain, i.e. 2,605 of 17,365
tokens. At ~3.6 bytes per token (English markdown) that is 9,378 bytes. The per-part caps exist so that a
part growing is visible as that part, before the total ever moves.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

PLAIN_TOKENS = 17365
KEEP_RULE = 0.15
BYTES_PER_TOKEN = 3.6
BUDGET_BYTES = int(PLAIN_TOKENS * KEEP_RULE * BYTES_PER_TOKEN)      # 9,378

CAPS = {                     # bytes; each part a little above its v0.3 size
    "rules": 3000,           # CLAUDE.md: rules + knowledge index
    "tools": 4600,           # MCP tool definitions, as advertised by default
    "skills": 1700,          # every skill's description is listed on every request
    "instructions": 400,     # MCP server instructions
    "commands": 200,         # slash-command descriptions
}


@pytest.fixture(scope="module")
def parts():
    home = tempfile.mkdtemp(prefix="budget-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARNESS_")}
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "harness"), "--home", home, "install", "--yes",
                        "--tools", "claude-code"], capture_output=True, text=True, env=env, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    hh = os.path.join(home, ".agent-harness")
    from agent_harness.mcp import server

    def frontmatter(path):
        t = open(path, encoding="utf-8").read()
        return t.split("---")[1] if t.startswith("---") else t.splitlines()[0]

    skills = os.path.join(home, ".claude", "skills")
    commands = os.path.join(home, ".claude", "commands")
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("HARNESS_")}
    try:
        tools = json.dumps(server.tool_list(), separators=(",", ":"))
    finally:
        os.environ.update(saved)
    return {
        "rules": open(os.path.join(home, ".claude", "CLAUDE.md"), encoding="utf-8").read(),
        "tools": tools,
        "skills": "\n".join(frontmatter(os.path.join(skills, s, "SKILL.md")) for s in sorted(os.listdir(skills))
                            if os.path.isfile(os.path.join(skills, s, "SKILL.md"))),
        "instructions": server.instructions(hh),
        "commands": "\n".join(frontmatter(os.path.join(commands, c)) for c in sorted(os.listdir(commands)))
        if os.path.isdir(commands) else "",
    }


@pytest.mark.parametrize("part", sorted(CAPS))
def test_each_part_within_its_cap(parts, part):
    size = len(parts[part].encode("utf-8"))
    assert size <= CAPS[part], "%s is %d bytes, cap %d" % (part, size, CAPS[part])


def test_total_within_the_keep_rule(parts):
    total = sum(len(v.encode("utf-8")) for v in parts.values())
    assert total <= BUDGET_BYTES, "the harness adds %d bytes per request; the keep rule allows %d" % (
        total, BUDGET_BYTES)


def test_the_work_graph_is_on_and_counted(parts):
    """The `plan` tool is on by default since v0.3.3 (owner), so its definition is inside the budget above."""
    assert '"name":"plan"' in parts["tools"]
