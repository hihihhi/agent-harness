#!/usr/bin/env python3
"""Gate the front half on SUBSTANCE, not on a heading being present.

The scaffold's gates were `grep -q '^## research'`. That passes when the heading
exists and nothing is under it, so analysis, research and plan could each be
"done" without a single thought being recorded -- the shell-check-that-cannot-fail
shape, sitting in this project's own scaffold.

Measured consequence: 8 of 169 node records across ten real graphs mention a
skill or plugin. ~150 skills are in the model's context every session and it
reached for one on 5% of real work. Nothing ever required it to look, so it
didn't. A capability that exists, is listed, and is never reached is
indistinguishable from an absent one.

Each check below names the failure it catches. Run:  phase-check.py <slug> <phase>
"""
import os
import re
import sys

PHASES = ("analysis", "research", "plan")


def plan_dir():
    """Resolve plan/ the way plan.py does, including the walk up to the repo root.

    F2, found by reviewing this file's own diff: this used to be a bare relative
    "plan", so `cd bin && plan gate t 1` made the gate report "no plan file" on a
    plan that exists -- plan itself walks up and finds it, and the gate did not.
    Two path resolvers for one file is one too many.
    """
    override = os.environ.get("PLAN_DIR", "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    cur = os.path.abspath(os.getcwd())
    while True:
        cand = os.path.join(cur, "plan")
        if os.path.isdir(cand):
            return cand
        if os.path.isdir(os.path.join(cur, ".git")) or cur == "/":
            return os.path.join(cur, "plan")
        cur = os.path.dirname(cur)


def section(text, name):
    m = re.search(rf"^##\s+{name}\b(.*?)(?=^##\s|\Z)", text, re.M | re.S | re.I)
    return (m.group(1) if m else "").strip()


def fail(msg, fix):
    print(f"PHASE GATE FAILED — {msg}", file=sys.stderr)
    print(f"  what to do: {fix}", file=sys.stderr)
    sys.exit(1)


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip().split("\n\n")[-1], file=sys.stderr)
        sys.exit(64)
    slug, phase = sys.argv[1], sys.argv[2].lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug):
        fail(f"invalid slug {slug!r}", "letters, digits, dot, dash, underscore only")
    p = os.path.join(plan_dir(), f"{slug}.md")
    if not os.path.isfile(p):
        fail(f"no plan file at {p}", "run `plan new <slug> \"<goal>\"` first")
    text = open(p, encoding="utf-8").read()

    if phase not in PHASES:
        fail(f"unknown phase {phase!r}", f"one of: {', '.join(PHASES)}")

    body = section(text, phase)
    if not body:
        fail(f"`## {phase}` is missing or empty",
             f"write what you actually found under `## {phase}` in {p}")
    words = len(body.split())
    # NO WORD-COUNT FLOOR. The first version had one and it was backwards: a
    # length threshold rewards padding, which is precisely the filler this gate
    # exists to reject, while refusing the concise, specific answer that is the
    # better artefact. Caught by this suite's own accept cases — 41 words naming
    # what was checked and citing two sources was refused while 300 words of
    # "generally considered best practice" would have passed on length alone.
    # Every check below is STRUCTURAL: a thing is present or it is not.

    if phase == "analysis":
        if not re.search(r"done when|acceptance|proves? (it )?done|success is", body, re.I):
            fail("`## analysis` never says what would prove it done",
                 "add a `done when:` line naming a COMMAND whose exit 0 settles it — "
                 "'fix the bug' becomes 'this test reproduces it, then passes'")

    if phase == "research":
        # THE one that was missing. 150 skills in context, used on 5% of work.
        if not re.search(r"^\s*(existing|surveyed|already have)\s*:", body, re.M | re.I):
            fail("`## research` has no `existing:` line — you did not check what "
                 "already solves this",
                 "add `existing:` naming what you CHECKED — your tool's installed skills, "
                 "plugins or extensions and MCP tools, this repo's own scripts, and "
                 "proven upstream projects. Write `existing: none applicable — "
                 "checked X, Y, Z` if that is the honest answer. Building something "
                 "that already exists is the most expensive possible outcome.")
        # F3, found by reviewing this file's own diff: this required a URL, a
        # backtick span or a file extension, so the honest and complete
        # "existing: none applicable - checked ListSkills, SearchPlugins and the
        # bin folder" was REFUSED. That grades markdown formatting, not content.
        # A named thing is a named thing however it is typed.
        srcs = re.findall(
            r"(https?://\S+"                      # a link
            r"|`[^`]+`"                            # a code span
            r"|\b[\w./-]+\.(?:py|sh|md|ts|js|json|toml|yaml|yml)\b"  # a file
            r"|\b[a-z]+[A-Z]\w+"                   # camelCase: ListSkills, SearchPlugins
            r"|\b[a-z][\w-]*_[\w-]+\b"            # snake_case
            r"|\b[A-Z][a-z]+[A-Z]\w+)",            # PascalCase
            body)
        if len(srcs) < 2:
            fail(f"`## research` cites {len(srcs)} concrete sources",
                 "name at least two specific things you read — a URL, a file, a "
                 "command and its output. `existing: none applicable` still has to "
                 "say WHAT you checked, and those names count as sources.")

    if phase == "plan":
        nodes = re.findall(r"^-\s+\[[ x>]\]\s+(\d+)\.", text, re.M)
        impl = [n for n in nodes if int(n) > 4]
        if not impl:
            fail("no implementation nodes exist beyond the scaffold",
                 "add the work as nodes: `- [ ] 5. <unit> | needs: 4 | gate: <command>`")
        gateless = []
        for line in text.splitlines():
            m = re.match(r"^-\s+\[[ x>]\]\s+(\d+)\.", line)
            if m and "gate:" not in line:
                gateless.append(m.group(1))
        if gateless:
            fail(f"node(s) {', '.join(gateless)} have no gate",
                 "NO GATE, NO NODE. If you cannot name the command that settles it, "
                 "it is not specified yet")
        # THERE WAS A "the plan must name a test" CHECK HERE. IT IS GONE ON
        # PURPOSE. Do not add a fourth version.
        #
        #   v1 searched the whole plan file, which holds the scaffold's own title
        #      "Plan, with acceptance tests written before any implementation".
        #      Always matched.
        #   v2 searched the plan body. The body under test read "No tests
        #      anywhere. Just ship it." Contains "tests". Still passed.
        #   v3 searched implementation-node gates for /test|check|lint|.../. The
        #      gate under test was `test -f out.txt` -- the shell builtin `test`.
        #      Still passed.
        #
        # Three attempts, one class: I was pattern-matching a STRING when the
        # property is SEMANTIC. "This gate verifies correctness" and "this gate
        # checks a file exists" are not distinguishable by regex, and every
        # attempt produced an assertion that could not fail -- precisely the
        # defect this whole file exists to remove. Shipping a fourth would be the
        # thing being criticised.
        #
        # Two failures the same way means the approach is wrong, not the
        # execution. This belongs to review (the `standards` skill, the
        # code-reviewer subagent), which can read a gate and judge it. Every
        # check that remains here is the presence or absence of a THING, never a
        # judgement about meaning.

    print(f"phase {phase}: OK ({words} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
