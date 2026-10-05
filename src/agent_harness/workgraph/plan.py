#!/usr/bin/env -S python3 -u
"""
plan — a durable work graph that several sessions can drain at once.

    [ ] pending        [>] running (leased)        [x] done

There is NO failed state. A failed gate appends evidence and returns the node to
pending. Measured basis: of 28 failures recorded by the previous ledger, only 5
were genuinely tried-and-failed; 82% were recoverable states with nowhere to go,
and the tool's own absorbing `fail` mark is what left 50 slices unreachable.

Node line — fields are order-free except `gate:`, which runs to end of line so a
gate command may contain pipes:

    - [ ] 7. Fix the seam | needs: 4,6 | ctx: src/a.py | risk: irreversible | gate: pytest -q

Each node also owns a record file at plan/<slug>/NN-title.md holding its full
history: every attempt, the complete gate output, durations, host, and written
summaries. The graph file stays scannable; the detail lives beside it; and
`plan context` feeds the record forward so a retry can see what the last attempt
actually tried.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

LEASE_HOURS = float(os.environ.get("PLAN_LEASE_HOURS", "4"))
GATE_TIMEOUT = int(os.environ.get("PLAN_GATE_TIMEOUT", "1800"))
CTX_BUDGET = int(os.environ.get("PLAN_CTX_BUDGET", "60000"))  # bytes before a node is "too big"
RECORD_TAIL = 4000    # bytes of the most RECENT record carried into a briefing
RECORD_HEAD = 1800    # bytes of the record's HEAD, where the decisions live
# Explicit character budgets, borrowed from Hermes' memory design. Without them
# a record grew to 142KB after ten attempts on one node — and records are
# git-tracked, so the file written to survive context loss becomes a cause of it.
# That is the previous ledger's exact defect: unbounded notes made `show` 12KB.
ATTEMPT_MAX = 6000    # bytes kept per attempt (head + tail, middle elided)
ATTEMPTS_KEPT = 12    # attempt sections retained before the oldest are pruned

NODE_RE = re.compile(r"^- \[(?P<mark>[ x>])\] (?P<id>\d+)\.\s*(?P<rest>.*?)\s*$")
# G10: anything that LOOKS like a node must either parse or be reported. A line
# written "- [ ] 6b." matched nothing in v1, so it vanished from the graph and a
# `needs: 6b` silently dropped — leaving a dependent node READY with an unmet
# dependency. Silence is the defect; this pattern is what makes it loud.
# R7 (red team): this was ^-\s*\[ , so an INDENTED node, a `*` bullet or a
# `+` bullet matched nothing, vanished from every bucket, and `ready`/`run`
# then printed ALL DONE over them. All three are ordinary markdown checkboxes.
# RC-8 (red team 2): widening this by relaxing the PREFIX made it match every
# markdown link bullet — `- [see docs](url)` — so ordinary documentation in a
# plan file became BROKEN and `plan run` refused healthy graphs. Constrain the
# SHAPE instead: a checkbox holds zero or one character, a link label does not.
NODELIKE_RE = re.compile(r"^[\s\u200b-\u200f\u2060\ufeff\u00a0]*[-*+]\s+\[[^\]]?\]")
# C1 (external certification): Python's \s does not match Unicode category Cf,
# so a ZERO-WIDTH SPACE before the bullet made a line match NEITHER pattern. It
# was not a node and not node-like, so it was counted nowhere: `plan lint` said
# LINT OK, `plan run` printed ALL DONE and exited 0 over a node marked
# `risk: irreversible`. That is this project's founding defect -- a line that
# looks like a node, is not parsed as one, and is reported by nothing -- and C1
# was its FOURTH recurrence.
#
# The lesson each time has been the same one and it was not learned: a guard
# written against the two characters that happened to be in the bug report does
# not close a character CLASS. So the check below is the class, not a list:
# anything Cf, plus the invisible Zs. Browser copy-paste and Windows-authored
# files carry these by default; nobody types them on purpose.
INVISIBLE_RE = re.compile(r"[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e"
                          r"\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]")
# H2 (external certification): parse_fields splits on ASCII "|" only, so a
# FULLWIDTH VERTICAL LINE made `risk: irreversible` part of the TITLE.
# is_irreversible() was False, the node was auto-dispatched unattended, and the
# destructive gate ran -- verified, the file it touched existed afterwards. One
# IME keystroke away for anyone typing CJK.
LOOKALIKE_SEP_RE = re.compile(r"[\uff5c\u2758\u2502\u01c0\u2223\u23b8\u23b9\u007c\ufe31]")


def invisible_defect(line):
    """Name every invisible or look-alike character on a node line, or None.

    Returned as a message rather than a bool because "your node is broken" with
    no codepoint is unactionable when the offending character is by definition
    unreadable.
    """
    bad = []
    for m in INVISIBLE_RE.finditer(line):
        bad.append(f"U+{ord(m.group()):04X} (invisible) at column {m.start() + 1}")
    for m in LOOKALIKE_SEP_RE.finditer(line):
        if m.group() == "|":
            continue
        bad.append(f"U+{ord(m.group()):04X} {m.group()!r} — a look-alike separator, "
                   f"not the ASCII | this format splits on, at column {m.start() + 1}")
    return "; ".join(bad) if bad else None
FENCE_RE = re.compile(r"^\s*(```|~~~)")
# R5 (red team): `exit (\d+)` could not match the `exit -9` that cmd_gate
# itself writes, so every signal death — OOM kill, SIGSEGV, Ctrl-C — was
# invisible to attempts(), stats and the STUCK detector, and the node was
# re-dispatched forever.
# The trailing [xxxxxxxx] is a signature over the whole normalised output. The
# group is optional so notes written before this existed still parse.
NOTE_RE = re.compile(r"exit (-?\d+)(?: in (\d+)s)?(?: \[([0-9a-f]{8})\])? — (.*)$")
FIELD_ORDER = ("needs", "ctx", "risk", "owner", "lease", "gate")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def procid():
    """R3 (red team): every orchestrator and shell on one machine shared the
    identity `user@host`, so the owner guard was a no-op between two shells,
    one run's cleanup released another's claims, and two `plan run` processes
    dispatched the same node in 3 of 6 trials. Identity must be per-process."""
    return f"{whoami()}/{os.getpid()}"


def sessionid():
    """Identity for interactive commands: stable across the invocations of one
    session, distinct between sessions.

    PPID was the previous answer and it was wrong for this tool's PRIMARY
    consumer. H6 (external certification): an agent session runs each shell
    command in a fresh subshell, so `plan claim` and the `plan gate` that
    followed it were ALWAYS strangers -- the documented manual protocol
    deadlocked on the first node for the full four-hour lease, every time, and
    the only escape the error offered was `--force`, which is precisely the flag
    that disables the guard. A guard whose ordinary path is "disable the guard"
    is not a guard.

    The order below is by how explicit the signal is:
      PLAN_OWNER               set deliberately -- worktrees, CI, a named worker
      CLAUDE_CODE_SESSION_ID   one agent session, however many subshells
      parent pid               a human in a terminal, the original case
    """
    explicit = os.environ.get("PLAN_OWNER", "").strip()
    if explicit:
        return explicit
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if sid:
        return f"{whoami()}/cc{sid[:12]}"
    return f"{whoami()}/s{os.getppid()}"


def whoami():
    # G3: Studio, laptop and the GPU box all touch the same repo. A bare
    # username cannot tell you which machine is holding a node.
    try:
        return f"{getpass.getuser()}@{machine_id()}"
    except Exception:
        return "unknown"


def machine_id():
    # A pid only means something on the machine that issued it, so anything
    # asking "is that holder still alive?" has to check this first.
    try:
        return socket.gethostname().split(".")[0]
    except Exception:
        return "unknown"


def die(msg, code=1):
    print(f"plan: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------- paths

def plan_dir():
    # H5 (external certification): the documented git-worktree flow handed TWO
    # sessions the same node. The mkdir lock is correct -- it is keyed to the
    # resolved plan FILE -- but `git worktree add` gives each session its own
    # git-tracked copy of plan/, so there was no shared state for the lock to
    # arbitrate. Both claimed node 1; nodes 2 and 3 were claimed by nobody.
    # PLAN_DIR points every worktree at one real directory, which is what makes
    # the guarantee in README.md true rather than aspirational.
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


def plan_path(slug):
    return os.path.join(plan_dir(), f"{slug}.md")


def record_path(slug, node):
    """Keyed by id, not title: an ordinary wording edit must not orphan a node's
    history. An existing record for this id wins over the derived name."""
    import glob as _glob
    d = os.path.join(plan_dir(), slug)
    hit = sorted(_glob.glob(os.path.join(d, f"{node['id']:02d}-*.md")))
    if hit:
        return hit[0]
    stem = re.sub(r"[^a-z0-9]+", "-", node["title"].lower()).strip("-")[:48] or "node"
    return os.path.join(d, f"{node['id']:02d}-{stem}.md")


# ----------------------------------------------------------------- parsing

def parse_fields(rest):
    gate = None
    m = re.search(r"\|\s*gate:\s*", rest)
    if m:
        gate = rest[m.end():].strip()
        rest = rest[:m.start()]
    parts = [p.strip() for p in rest.split("|")]
    title = parts[0].strip()
    fields, orphans = {}, []
    for p in parts[1:]:
        # A colon alone does not make a field. Found by the property tier: a
        # title containing "| :" was accepted as a field with an EMPTY key, so
        # no orphan was reported and the title was silently truncated — the same
        # silent-rewrite class as the dropped-field bug, one layer deeper.
        # A key has to look like a key.
        k, _, v = (p.partition(":") if ":" in p else ("", "", ""))
        k, v = k.strip().lower(), v.strip()
        # A field needs a key that looks like a key AND a value. "A:" has a
        # valid key and an empty value; it used to be stored as "" and then
        # dropped by render_node, truncating the title with nothing reported.
        if k and v and re.fullmatch(r"[a-z][a-z0-9_-]*", k):
            # C2 (external certification): this was `fields[k] = v`, so
            # `needs: 2 | needs: 1` silently bound needs=1 and DROPPED needs=2.
            # The node became READY while its declared parent was pending, lint
            # said OK, and render_node then wrote the survivor back -- deleting
            # user-written text from the git-tracked source of truth. A repeated
            # key is the ordinary result of pasting a new clause without
            # deleting the old one, and last-write-wins is never what was meant.
            if k in fields:
                orphans.append(f"duplicate field `{k}:` — "
                               f"{fields[k]!r} and {v!r}; delete one")
            else:
                fields[k] = v
        elif p:
            # A segment with no `key:` is text the split ate — almost always a
            # literal pipe in a title or a path. Dropping it silently renames
            # the node in the source of truth.
            orphans.append(p)
    if gate is not None:
        # A second `| gate:` is two shell commands with one silently discarded.
        if re.search(r"\|\s*gate:\s*", gate):
            orphans.append("a second `gate:` — one shell command would be "
                           "silently discarded; delete one")
        fields["gate"] = gate
    return title, fields, orphans


def _clean(v):
    """R1 (red team): field values and notes were written unsanitised, so a
    newline in PLAN_OWNER, in a release reason, or in a worker's JSON subtype
    injected a whole node line into the graph.

    Only characters that can forge STRUCTURE are touched: newlines make new node
    lines, and `|` is the field separator. An earlier version also collapsed
    runs of spaces, which the property tier caught round-tripping "0  0" into
    "0 0" — sanitising more than the threat is still data loss."""
    return re.sub(r"[\r\n\t\v\f]+", " ", str(v)).strip().replace("|", "\u2758")


def render_node(n):
    bits = [n["title"]]
    for k in FIELD_ORDER:
        v = n["fields"].get(k)
        if v and k != "gate":
            bits.append(f"{k}: {_clean(v)}")
    # Anything the user wrote that this tool does not know about is still theirs.
    # Dropping it on write-back is silent data loss in the source of truth.
    for k, v in n["fields"].items():
        if k not in FIELD_ORDER and v:
            bits.append(f"{k}: {_clean(v)}")
    if n["fields"].get("gate"):
        bits.append(f"gate: {n['fields']['gate']}")
    return f"- [{n['mark']}] {n['id']}. " + " | ".join(bits)


def load(slug):
    path = plan_path(slug)
    if not os.path.exists(path):
        die(f"no plan file at {path}")
    # plan/ is an ordinary directory that anything may drop an entry into, and
    # every listing command walks all of it. `open()` on a fifo BLOCKS until a
    # writer appears, which hung the SessionStart hook -- the thing in front of
    # every session start -- for as long as the entry existed. The check has to
    # come before the open, because an open that has already blocked cannot
    # report anything. os.stat does not block on a fifo; open does.
    if not os.path.isfile(path):
        die(f"{path} is not a regular file")
    try:
        lines = open(path, encoding="utf-8").read().split("\n")
    except OSError as e:
        die(f"cannot read {path}: {e.strerror or e}")
    except UnicodeDecodeError:
        die(f"{path} is not UTF-8 text")
    nodes, order, malformed = {}, [], []
    cur = None
    fenced, fence_line, swallowed = False, 0, 0
    for i, line in enumerate(lines):
        # K2 (red team 2): load() had no notion of fenced regions, so an example
        # node inside a ``` block became executable work — a plan file that
        # documents the node format dispatched its own documentation.
        if FENCE_RE.match(line):
            fenced = not fenced
            if fenced:
                fence_line, swallowed = i + 1, 0
            cur = None
            continue
        if fenced:
            if NODELIKE_RE.match(line):
                swallowed += 1
            continue
        m = NODE_RE.match(line)
        # C1/H2: a line can match NODE_RE perfectly and still be wrong, because
        # the damage is done by characters the eye cannot see. A FULLWIDTH
        # VERTICAL LINE parses as ordinary title text, so `risk: irreversible`
        # became part of the title and a destructive gate was auto-dispatched.
        # Checked here rather than in the regex so the report can name the
        # codepoint and column -- "this node is broken" is unactionable when the
        # offending character is by definition unreadable.
        bad = invisible_defect(line) if (m or NODELIKE_RE.match(line)) else None
        if bad:
            malformed.append((i + 1, f"{line.strip()[:60]}  <- {bad}"))
            cur = None
            continue
        if m:
            title, fields, orphans = parse_fields(m.group("rest"))
            nid = int(m.group("id"))
            if nid in nodes:
                die(f"duplicate node id {nid} (line {i + 1})")
            cur = {"id": nid, "mark": m.group("mark"), "title": title, "fields": fields,
                   "orphans": orphans, "line": i, "notes": [], "note_lines": []}
            nodes[nid] = cur
            order.append(nid)
        elif NODELIKE_RE.match(line):
            malformed.append((i + 1, line.strip()))
            cur = None
        elif cur is not None and line.strip() and line[0] in " \t":
            cur["notes"].append(line.strip())
            cur["note_lines"].append(i)
        elif not line.strip():
            pass
        else:
            cur = None
    if fenced:
        # THIRD INSTANCE of the vanishing-node defect. A fence opened and never
        # closed swallowed every remaining line, so real work disappeared from
        # every report and `show` said "0 broken". The suite missed it because it
        # only ever tested CLOSED fences.
        malformed.append((fence_line,
                          f"unclosed code fence — everything after this line was "
                          f"ignored, including {swallowed} node-like line(s)"))
    return path, lines, nodes, order, malformed


def save(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


def needs_of(n):
    """Returns (valid_ids, bad_tokens). A non-integer dependency is reported, never
    silently dropped — dropping one is what let a node run before its parent."""
    raw = n["fields"].get("needs", "")
    good, bad = [], []
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip()
        if not tok:
            continue
        if tok.isdigit():
            good.append(int(tok))
        else:
            bad.append(tok)
    return good, bad


def effective_mark(n):
    if n["mark"] != ">":
        return n["mark"]
    ts = parse_iso(n["fields"].get("lease", ""))
    if ts is None or datetime.now(timezone.utc) - ts > timedelta(hours=LEASE_HOURS):
        return " "
    return ">"


ATTEMPT_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:]+Z ")


def attempts(n):
    out = []
    for note in n["notes"]:
        # Anchored: only a line the tool itself wrote counts as an attempt.
        # NOTE_RE.search used to match worker-supplied text anywhere in a note,
        # so a gate that printed "exit 1 — ..." forged an attempt record.
        if not ATTEMPT_LINE_RE.match(note):
            continue
        m = NOTE_RE.search(note)
        if m:
            out.append({"code": m.group(1), "secs": int(m.group(2) or 0),
                        "sig": m.group(3), "tail": m.group(4).strip()})
    return out


# Volatile things that differ between two runs of the SAME failure. Durations
# and clocks, not counts: "3 failed" becoming "2 failed" is progress and must
# read as a different failure.
_VOLATILE = [
    (re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+Z?"), "TS"),
    (re.compile(r"\b\d\d:\d\d:\d\d(?:[.,]\d+)?\b"), "TS"),
    # A number is a duration only when a unit follows AND it is not part of an
    # identifier. Without the lookbehind, "test_h2s" normalised to "test_hN"
    # and two different tests collapsed into one signature.
    # H3 (external certification): this alternation was ms|msec|s|sec|secs|m|min
    # -- all ABBREVIATIONS -- and \b cannot match inside "seconds". rspec prints
    # "Finished in 4.21 seconds", so every run produced a fresh signature, the
    # STUCK breaker never fired, and a hopeless node burned the full round cap:
    # 20 dispatches of the user's rolling usage window instead of 2.
    # signature.test.sh stayed green throughout because all six runners it
    # covers abbreviate -- the corpus shared the blind spot with the code.
    (re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s*"
                r"(?:ms|msec|msecs|millisecond|milliseconds"
                r"|us|usec|ns|nsec"
                r"|s|sec|secs|second|seconds"
                r"|m|min|mins|minute|minutes"
                r"|h|hr|hrs|hour|hours)\b"), "DUR"),
    (re.compile(r"(?<![\w.])0x[0-9a-fA-F]+"), "ADDR"),
    (re.compile(r"/(?:private/)?(?:var|tmp)/[^\s:,)\]]+"), "TMPPATH"),
    (re.compile(r"\bpid[:= ]\s*\d+"), "PID"),
]


def _normalise(text):
    for rx, tok in _VOLATILE:
        text = rx.sub(tok, text)
    return text


def gate_signature(blob):
    """A signature over the WHOLE output, not a tail.

    Measured on real runner output: a three-line tail is wrong in both
    directions. go, jest and maven put boilerplate last, so two GENUINELY
    DIFFERENT failures produced the same tail and STUCK fired on a node that was
    converging. ctest puts a total-time line last, so two IDENTICAL failures
    looked different and the node was dispatched forever. Normalising the whole
    output and hashing it fixes both, and the hash is stored in the note so the
    detector never re-derives it from truncated text."""
    import hashlib
    return hashlib.sha1(_normalise(blob).encode("utf-8", "replace")).hexdigest()[:8]


def _sig(x):
    """Prefer the stored whole-output signature; fall back to a normalised tail
    for notes written before signatures existed."""
    if x.get("sig"):
        return (x["code"], x["sig"])
    return (x["code"], _normalise(x["tail"]))


def repeated_failures(n):
    a = attempts(n)
    if not a or a[-1]["code"] == "0":
        return 0
    last, c = _sig(a[-1]), 0
    for x in reversed(a):
        if _sig(x) != last:
            break
        c += 1
    return c


def stuck_tag(n):
    r = repeated_failures(n)
    return f"  [STUCK: same failure {r}x — change approach]" if r >= 2 else ""


def is_irreversible(n):
    return "irrevers" in n["fields"].get("risk", "").lower()


# ---------------------------------------------------------------- analysis

def find_cycles(nodes):
    """Tarjan's strongly connected components, iterative.

    K3 (red team 2): membership used to come from the DFS *path* — the nodes
    between the back-edge target and the current node. That is not the cycle; it
    is one walk through it, so members reachable by a different route were
    under-reported (~3.2% measured) and were then dispatched or told to wait
    forever. A node is in a cycle exactly when its SCC has more than one member,
    or it depends on itself. Iterative for the same reason as before: the
    recursive version died at a 990-deep chain and took every command with it."""
    index, low, onstack, stack = {}, {}, set(), []
    counter, bad = [0], set()

    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(needs_of(nodes[root])[0]))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); onstack.add(root)
        while work:
            nid, it = work[-1]
            advanced = False
            for d in it:
                if d not in nodes:
                    continue
                if d not in index:
                    index[d] = low[d] = counter[0]; counter[0] += 1
                    stack.append(d); onstack.add(d)
                    work.append((d, iter(needs_of(nodes[d])[0])))
                    advanced = True
                    break
                if d in onstack:
                    low[nid] = min(low[nid], index[d])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[nid])
            if low[nid] == index[nid]:
                comp = []
                while True:
                    w = stack.pop(); onstack.discard(w); comp.append(w)
                    if w == nid:
                        break
                if len(comp) > 1:
                    bad.update(comp)
                elif nid in needs_of(nodes[nid])[0]:
                    bad.add(nid)
    return bad


def analyse(nodes, order, malformed):
    eff = {nid: effective_mark(n) for nid, n in nodes.items()}
    cycles = find_cycles(nodes)
    ready, blocked, running, done, broken, held = [], [], [], [], [], []

    for nid in order:
        n = nodes[nid]
        m = eff[nid]
        if m == "x":
            done.append(nid); continue
        if m == ">":
            running.append(nid); continue
        deps, baddeps = needs_of(n)
        dangling = [d for d in deps if d not in nodes]
        gate_txt = n["fields"].get("gate") or ""
        # A node must not be able to edit the script its own gate runs. Found by
        # letting the orchestrator implement a command TDD-style: the node's ctx
        # named bin/doctor.test.sh and its gate WAS ./bin/doctor.test.sh, so the
        # worker was invited to rewrite its own acceptance criteria. It happened
        # to strengthen the test; nothing made that the only option.
        # Scoped to TEST files. "ctx: a.txt | gate: test -f a.txt" is the normal
        # case — the ctx file is the artefact the gate checks for, not the script
        # that judges it. The risk is a node that can edit its own test.
        def _is_test(fp):
            b = os.path.basename(fp)
            return (".test." in b or b.startswith("test_") or b.endswith("_test.py")
                    or b.endswith("_test.go") or "/tests/" in fp or fp.startswith("tests/"))
        self_gating = [f.strip() for f in (n["fields"].get("ctx") or "").split(",")
                       if f.strip() and _is_test(f.strip())
                       and os.path.basename(f.strip()) in gate_txt]
        # re.I (red team 2): the check was case-sensitive, so `| RISK: irreversible`
        # written after the gate was not merely swallowed but silently un-warned,
        # and the destructive gate then ran unattended. A shift key disarmed the
        # single most important safety marker in the tool.
        swallowed = re.search(r"\|\s*(needs|ctx|risk|owner|lease)\s*:", gate_txt, re.I)
        if self_gating:
            broken.append((nid, f"ctx names {self_gating[0]!r}, which its own gate runs — "
                                "a node cannot be allowed to edit its acceptance criteria"))
        elif swallowed:
            # R2 (red team), ranked the single worst defect found: `gate:` runs
            # to end of line, so `risk: irreversible` written AFTER it is not
            # merely ignored — it is DISARMED, and the node is dispatched and
            # its gate executed unattended with LINT OK printed over it. No
            # adversary needed; only a plausible authoring mistake.
            broken.append((nid, f"`{swallowed.group(1)}:` is buried inside `gate:` — "
                                "gate runs to end of line and must be the LAST field"))
        elif n.get("orphans"):
            broken.append((nid, "a `|` in the title or a value would be silently "
                                f"truncated on write-back: {n['orphans'][0]!r}"))
        elif baddeps:
            broken.append((nid, f"needs a non-numeric id: {','.join(baddeps)}"))
        elif dangling:
            broken.append((nid, f"needs missing node(s) {','.join(map(str, dangling))}"))
        elif nid in cycles:
            broken.append((nid, "dependency cycle"))
        elif not n["fields"].get("gate"):
            broken.append((nid, "no gate — a node without one can never be settled"))
        else:
            unmet = [d for d in deps if eff.get(d) != "x"]
            if unmet:
                blocked.append((nid, unmet))
            elif is_irreversible(n):
                held.append(nid)          # G4: never auto-dispatched
            else:
                ready.append(nid)

    pending = sum(1 for nid in order if eff[nid] == " ")
    assert len(ready) + len(blocked) + len(broken) + len(held) == pending, \
        "a pending node fell out of every bucket"
    return {"eff": eff, "ready": ready, "blocked": blocked, "running": running,
            "done": done, "broken": broken, "held": held, "malformed": malformed}


# ----------------------------------------------------------------- records

APPROVAL_RE = re.compile(r"\bAPPROVED\b")


def _approval_lines(slug):
    """The non-node lines that carry the APPROVED token — the human's authority,
    and the only thing in the prose a worker must never be able to write.

    This used to hash ALL non-node text, which made the product's own scaffold
    undrainable: `plan init` gates nodes 1-3 on the content of the plan file
    itself, so writing that section IS the work. Hashing
    every prose line meant an honest worker doing exactly what its gate asked
    was rolled back and told it had forged an approval, and the front half could
    never reach the sign-off it exists to reach. The prose is the worker's
    workspace; the APPROVED token is not.

    Notes are deliberately NOT covered here -- the orchestrator appends its own
    note inside the round, so watching them flags every honest run as tampering.
    They are covered per node by snapNotes in the reconcile instead, which can
    tell the orchestrator's one append from a worker's forgery. See H1.

    The token is matched anywhere in the line, not just anchored at column 0:
    the scaffold's gate is `grep -q '^APPROVED'`, but a hand-written one may
    drop the anchor, and this guard defends the loosest gate a user could write.
    """
    out = []
    try:
        for line in open(plan_path(slug), encoding="utf-8"):
            s = line.rstrip("\n")
            if NODE_RE.match(s) or (line[:1] in " \t" and line.strip()):
                continue
            if APPROVAL_RE.search(s):
                out.append(s)
    except OSError:
        return None
    return out


def _sandbox_prefix():
    """K1: DEPLOY.md's largest stated gap was that tamper handling is repair,
    not prevention — workers run as you, with your filesystem access, and the
    orchestrator's own `shell=True` gate execution sits entirely outside Claude
    Code's permission system. srt (Anthropic's sandbox-runtime, sandbox-exec on
    macOS) makes that an OS invariant. It WRAPS the official binary rather than
    proxying credentials, so OAuth is untouched.

    Opt in with PLAN_SANDBOX=1. Off by default because it is a Beta Research
    Preview and a wrong allowlist breaks every worker."""
    if os.environ.get("PLAN_SANDBOX", "") not in ("1", "true", "yes"):
        return []
    cfg = os.environ.get("PLAN_SANDBOX_CONFIG") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        "sandbox", "worker.json")
    if not os.path.exists(cfg):
        die(f"PLAN_SANDBOX is on but no profile at {cfg}", 8)
    if shutil.which("srt") is None:
        die("PLAN_SANDBOX is on but srt is not installed "
            "(npm i -g @anthropic-ai/sandbox-runtime)", 8)
    # --settings, not -s: the short form fails to load and srt then refuses to
    # run rather than falling back, which is the right behaviour but a silent
    # trap if you write the short flag.
    return ["srt", "--settings", cfg]


def _record_hash(path):
    import hashlib
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None


def ensure_record(slug, node):
    p = record_path(slug, node)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"# Node {node['id']} — {node['title']}\n\n"
                    f"plan: {slug}\n"
                    f"gate: `{node['fields'].get('gate', '(none)')}`\n"
                    f"ctx: {node['fields'].get('ctx', '(none)')}\n")
    return p


def _budget(body, cap=ATTEMPT_MAX):
    """Keep the head and the tail — what ran, and how it failed. The middle of a
    long test log is the least informative part of it."""
    body = body.rstrip()
    if len(body) <= cap:
        return body
    head, tail = int(cap * 0.3), int(cap * 0.7)
    return (body[:head] + f"\n\n… {len(body) - cap} bytes elided …\n\n" + body[-tail:])


def _prune(text, keep=ATTEMPTS_KEPT):
    """Drop the oldest attempt sections, but never silently — the count of what
    was dropped stays in the file."""
    # Split on OUR heading shapes only, never on any "## " the gate emitted.
    parts = re.split(r"\n(?=## (?:Attempt|Summary|Released) )", text)
    if len(parts) - 1 <= keep:
        return text
    dropped = len(parts) - 1 - keep
    return (parts[0].rstrip() + f"\n\n## … {dropped} earlier attempt(s) pruned …\n"
            + "".join("\n" + x for x in parts[-keep:]))


def _defang(body):
    """R6 (red team): _prune splits on "\n## ", and append_record wrote gate
    output raw — so a gate emitting markdown headings deleted every real
    attempt, fabricated the pruned count, and could forge a passing attempt
    that plan context then fed forward as evidence. Gate output is data."""
    return re.sub(r"(?m)^(#+ )", lambda m: "\u200b" + m.group(1), body)


def append_record(slug, node, heading, body):
    p = ensure_record(slug, node)
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"\n## {heading}\n\n{_defang(_budget(body))}\n")
    cur = open(p, encoding="utf-8").read()
    pruned = _prune(cur)
    if pruned != cur:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(pruned)
        os.replace(tmp, p)
    return p


def read_record(slug, node):
    p = record_path(slug, node)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


# -------------------------------------------------------------------- io

def restore_notes(path, n, keep):
    """Drop every note line on node `n` past the first len(keep). Used only by
    the reconcile, to undo a worker that forged or rewrote gate evidence."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    drop = set(n["note_lines"][len(keep):])
    out = [ln for i, ln in enumerate(lines) if i not in drop]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    os.replace(tmp, path)


def set_node(path, lines, n, mark=None, **fields):
    if mark is not None:
        n["mark"] = mark
    for k, v in fields.items():
        if v is None:
            n["fields"].pop(k, None)
        else:
            n["fields"][k] = v
    lines[n["line"]] = render_node(n)
    save(path, lines)


def append_note(path, lines, n, text):
    at = (n["note_lines"][-1] if n["note_lines"] else n["line"]) + 1
    lines.insert(at, f"      {now_iso()} {_clean(text)}")
    save(path, lines)


def lock_holder(d):
    """(machine, pid) recorded inside lock dir `d`, or None when it carries no
    readable identity — a hand-made lock, or one from before locks were stamped.
    Absent identity is not evidence of anything, so callers fall back to age."""
    try:
        with open(os.path.join(d, "pid")) as fh:
            parts = fh.read().split()
    except OSError:
        return None
    if not parts:
        return None
    # One field is a bare pid, which can only have been written on this machine.
    machine, pid = (parts[0], parts[1]) if len(parts) > 1 else (machine_id(), parts[0])
    try:
        pid = int(pid)
    except ValueError:
        return None
    return (machine, pid) if pid > 0 else None


def holder_is_dead(d):
    """True only when the lock's holder is PROVABLY gone: same machine, and the
    kernel has no such process. A holder we cannot probe — another machine,
    another user, no identity at all — is treated as alive."""
    who = lock_holder(d)
    if who is None or who[0] != machine_id() or who[1] == os.getpid():
        return False
    try:
        os.kill(who[1], 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False          # EPERM: it exists and belongs to someone else
    return False


class Lock:
    """Whole-file mutex, one mkdir wide.

    A worker killed inside the lock window used to leave the dir behind and
    every subsequent write — claim, gate, note — blocked on it for the full
    30-minute gate timeout, because age was the only staleness signal there was.
    Age is a poor one: it is a guess about a process, made without asking the
    kernel about that process. So the holder now stamps its identity into the
    dir and a later writer asks directly. A provably dead holder is reclaimed at
    once; age remains the fallback for a holder that cannot be probed.
    """

    def __init__(self, path):
        self.d = path + ".lock"
        self.tag = None

    def __enter__(self):
        for _ in range(200):
            try:
                os.mkdir(self.d)
            except FileExistsError:
                if not self._reclaim():
                    time.sleep(0.05)
                continue
            self.tag = f"{machine_id()} {os.getpid()}\n"
            try:
                with open(os.path.join(self.d, "pid"), "w") as fh:
                    fh.write(self.tag)
            except OSError:
                # Unstamped is the old behaviour, not a failure: we still hold
                # the lock, and the next writer falls back to judging it by age.
                self.tag = None
            return self
        die(f"could not acquire lock; remove {self.d} if stale")

    def _reclaim(self):
        """Remove the lock if its holder is dead or it has outlived a gate.
        True when something was removed and the mkdir is worth retrying."""
        try:
            st = os.stat(self.d)
        except OSError:
            return True                       # already gone
        if not (holder_is_dead(self.d)
                or time.time() - st.st_mtime > GATE_TIMEOUT):
            return False
        # Two writers can find the same dead lock at the same instant. Renaming
        # it aside is atomic, so exactly one of them wins and the loser simply
        # retries — where a bare rmdir would let the second one delete the lock
        # the first had already replaced with its own.
        aside = f"{self.d}.{os.getpid()}.dead.lock"
        try:
            os.rename(self.d, aside)
        except OSError:
            return True                       # someone else won the rename
        if os.stat(aside).st_ino != st.st_ino:
            try:                              # we moved a lock we never inspected
                os.rename(aside, self.d)
                return True
            except OSError:
                pass
        shutil.rmtree(aside, ignore_errors=True)
        return True

    def __exit__(self, *a):
        if self.tag is not None:
            try:
                with open(os.path.join(self.d, "pid")) as fh:
                    if fh.read() != self.tag:
                        return                # reclaimed and re-taken; not ours
            except OSError:
                pass
        shutil.rmtree(self.d, ignore_errors=True)


FRONT_TEMPLATE = """## goal: {goal}

Done-condition: <one command that exits 0 when this goal is finished>

- [ ] 1. Analyse: what is actually being asked, and what would prove it done \
| gate: phase-check {slug} analysis
- [ ] 2. Research: what ALREADY solves this, prior art, unknowns resolved or deferred \
| needs: 1 | gate: phase-check {slug} research
- [ ] 3. Plan, with acceptance tests written before any implementation \
| needs: 2 | gate: phase-check {slug} plan
- [ ] 4. Finalise: scope, gates and done-condition agreed \
| needs: 3 | gate: grep -q '^APPROVED' plan/{slug}.md

<!-- Implementation nodes go below; each declares needs: 4 so no code starts
     before scope is agreed. Every node needs a gate. Mark anything with an
     irreversible effect `risk: irreversible` and the orchestrator will hold it
     for a human instead of running it unattended. -->
"""


PROJECT_CLAUDE = """# {name}

{goal}

## How work runs here

Plan state lives in `plan/<slug>.md`, git-tracked. Marks are `[ ]` pending,
`[>]` running (leased), `[x]` done. **There is no failed state** — a failed gate
appends evidence and returns the node to pending, which is the debug loop.

    plan ready <slug>     what can start now, or exactly why nothing can
    plan run <slug> --workers 3    drain it autonomously
    plan gate <slug> <n>  exit 0 is the only definition of done

Every node needs a gate: a shell command whose exit code settles it. A node
with an irreversible effect gets `risk: irreversible` and is held for a human.

## Rules

- The gate is the evidence. A report of success is a claim; run the gate.
- Never weaken a gate to make it pass. Fix the gate and say so in a note.
- Every test assertion must be able to fail.
- Don't add a subsystem without a recorded failure demanding it.
"""


GITIGNORE = """.DS_Store
__pycache__/
*.py[cod]
.pytest_cache/
node_modules/
target/
dist/
build/
.venv/
"""


def cmd_init(a):
    """From nothing to a scaffolded, gated plan in one command.

    Creates the directory, makes it its own git repo (memory and context scope
    per repo, which is why the workspace was split), writes a project CLAUDE.md
    and .gitignore, optionally installs the test tiers, and scaffolds the
    user-gated front half. Everything it creates is reported; nothing existing
    is overwritten."""
    root = os.path.abspath(os.path.expanduser(a.dir))
    made = []
    if not os.path.isdir(root):
        os.makedirs(root)
        made.append(root + "/")
    if not os.path.isdir(os.path.join(root, ".git")):
        r = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root,
                           capture_output=True, text=True)
        if r.returncode != 0:
            die("git init failed: " + (r.stderr or "").strip())
        made.append(".git/")
    pdir = os.path.join(root, "plan")
    if not os.path.isdir(pdir):
        os.makedirs(pdir)
        made.append("plan/")

    name = os.path.basename(root)
    goal = " ".join(a.goal) if a.goal else "No goal stated yet."

    cm = os.path.join(root, "CLAUDE.md")
    if not os.path.exists(cm):
        open(cm, "w", encoding="utf-8").write(PROJECT_CLAUDE.format(name=name, goal=goal))
        made.append("CLAUDE.md")
    gi = os.path.join(root, ".gitignore")
    if not os.path.exists(gi):
        open(gi, "w", encoding="utf-8").write(GITIGNORE)
        made.append(".gitignore")

    if a.python:
        # realpath, not abspath: ~/bin/plan is a symlink, and abspath returns the
        # LINK's directory, so the template resolved to a path that does not
        # exist and the test tiers were silently skipped.
        here = os.path.dirname(os.path.realpath(__file__))
        src = os.path.join(here, "tiers", "pytest.ini")   # inside the package, so it travels with an install
        dst = os.path.join(root, "pytest.ini")
        if os.path.exists(src) and not os.path.exists(dst):
            open(dst, "w", encoding="utf-8").write(open(src, encoding="utf-8").read())
            made.append("pytest.ini")
        td = os.path.join(root, "tests")
        if not os.path.isdir(td):
            os.makedirs(td)
            made.append("tests/")

    slug = a.slug or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "main"
    pf = os.path.join(pdir, slug + ".md")
    if os.path.exists(pf):
        die(f"{pf} already exists — pick another --slug")
    open(pf, "w", encoding="utf-8").write(FRONT_TEMPLATE.format(goal=goal, slug=slug))
    made.append(f"plan/{slug}.md")

    print(f"initialised {root}")
    for m in made:
        print(f"  + {m}")
    print()
    print("next:")
    print(f"  cd {root}")
    print(f"  plan ready {slug}          # the front half — analyse, research, plan, finalise")
    print(f"  plan run {slug} --workers 3   # once APPROVED is in the plan file")
    return 0


def cmd_new(a):
    d = plan_dir(); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{a.slug}.md")
    if os.path.exists(path):
        die(f"{path} already exists — edit it, or pick another slug")
    goal = " ".join(a.goal) if a.goal else a.slug
    open(path, "w", encoding="utf-8").write(FRONT_TEMPLATE.format(goal=goal, slug=a.slug))
    print(path); return 0


def _report_defects(st, nodes):
    for ln, text in st["malformed"]:
        # A malformed entry is either a node-shaped line that would not parse, or
        # a structural problem that already explains itself.
        lead = "" if text.startswith("unclosed") else "looks like a node but does not parse — "
        print(f"BROKEN line {ln}: {lead}{text}")
    for nid, why in st["broken"]:
        print(f"BROKEN {nid}. {nodes[nid]['title']} — {why}")
    for nid in st["held"]:
        print(f"HELD {nid}. {nodes[nid]['title']} — risk: irreversible, needs a human")


def cmd_ready(a):
    _, _, nodes, order, mal = load(a.slug)
    st = analyse(nodes, order, mal)
    for nid in st["ready"]:
        print(f"READY {nid}. {nodes[nid]['title']}{stuck_tag(nodes[nid])}")
    _report_defects(st, nodes)
    if st["ready"]:
        return 0
    for nid in st["running"]:
        n = nodes[nid]
        print(f"RUNNING {nid}. {n['title']}  (owner {n['fields'].get('owner','?')}, "
              f"lease {n['fields'].get('lease','?')})")
    for nid, unmet in st["blocked"]:
        names = ", ".join(f"{d} ({nodes[d]['title']})" for d in unmet if d in nodes)
        print(f"BLOCKED {nid}. {nodes[nid]['title']} — waiting on {names}")
    if not any((st["running"], st["blocked"], st["broken"], st["held"], st["malformed"])):
        print("ALL DONE" if st["done"] else "EMPTY — no nodes")
        return 0
    return 1


def cmd_claim(a):
    path = plan_path(a.slug)
    with Lock(path):
        _, lines, nodes, order, mal = load(a.slug)
        st = analyse(nodes, order, mal)
        if not st["ready"]:
            print("NONE READY", file=sys.stderr); return 1
        # G7: fewest attempts first, so retrying sessions spread across the
        # frontier instead of piling onto whichever node has the lowest id.
        nid = sorted(st["ready"], key=lambda i: (len(attempts(nodes[i])), i))[0]
        n = nodes[nid]
        set_node(path, lines, n, mark=">", lease=now_iso(), owner=a.owner)
        ensure_record(a.slug, n)
    print(nid); return 0


def cmd_release(a):
    """G2: yield a node on purpose — usage limit hit, laptop closing, wrong approach —
    with the reason recorded. Lease expiry covers the ungraceful case; this
    covers the graceful one, so an abandoned node never looks like a mystery."""
    path = plan_path(a.slug)
    with Lock(path):
        _, lines, nodes, _, _ = load(a.slug)
        if a.id not in nodes:
            die(f"no node {a.id}")
        n = nodes[a.id]
        # R3 (red team): release checked neither owner nor mark, so it reverted
        # gate-proven `done` work with exit 0, and yanked a live lease out from
        # under a running session — the documented crash-recovery command
        # producing a genuine double dispatch.
        if n["mark"] == "x" and not a.force:
            die(f"node {a.id} is done; pass --force to reopen gate-proven work", 4)
        _assert_owner(n, getattr(a, "owner", None), getattr(a, "force", False), a.id)
        reason = " ".join(a.reason) if a.reason else "released"
        append_note(path, lines, n, f"released — {reason}")
        _, lines, nodes, _, _ = load(a.slug)
        n = nodes[a.id]
        set_node(path, lines, n, mark=" ", lease=None)
        append_record(a.slug, n, f"Released {now_iso()} — {whoami()}", reason)
    print(f"{a.id} -> pending ({reason})"); return 0


def squeeze_record(rec):
    """Fit a node's record into a briefing without losing the load-bearing part.

    This was `rec[-RECORD_TAIL:]` -- the pure TAIL. A record's HEAD is where the
    planner writes the decisions that must not be reopened; its TAIL is the most
    recent gate output. Measured on a real 8.6 KB record: the briefing delivered
    4 KB of "attempt 32 failed, attempt 33 failed" and DROPPED "NEVER use the
    legacy endpoint; it silently drops writes".
    
    So the context-loss failure the whole graph exists to prevent was living
    inside the mechanism meant to prevent it -- and the bytes it spent were the
    least informative in the file, 59 near-identical lines.

    Two changes, in this order, because the first is free:

      1. Collapse runs of attempt lines that share a gate SIGNATURE. Twelve
         identical failures are one fact, not twelve, and the signature is
         already the thing STUCK is computed from. This is lossless: the count
         and the signature say everything the copies did.
      2. Only then truncate, keeping the HEAD as well as the tail, and SAYING so.
         A silent truncation is how a constraint disappears without anyone
         noticing it was ever there.
    """
    lines, out, i = rec.splitlines(), [], 0
    while i < len(lines):
        m = ATTEMPT_LINE_RE.match(lines[i].strip())
        sig = None
        if m:
            sm = re.search(r"\[([0-9a-f]{8})\]", lines[i])
            sig = sm.group(1) if sm else None
        if sig:
            j, first = i, lines[i]
            while j + 1 < len(lines) and f"[{sig}]" in lines[j + 1] \
                    and ATTEMPT_LINE_RE.match(lines[j + 1].strip()):
                j += 1
            run = j - i + 1
            if run > 2:
                out.append(first.rstrip())
                out.append(f"      ... {run - 2} more attempts, identical output "
                           f"[{sig}] — same failure, so the approach is wrong")
                out.append(lines[j].rstrip())
                i = j + 1
                continue
        out.append(lines[i].rstrip())
        i += 1
    body = "\n".join(out)
    if len(body) <= RECORD_HEAD + RECORD_TAIL:
        return body
    head, tail = body[:RECORD_HEAD], body[-RECORD_TAIL:]
    cut = len(body) - len(head) - len(tail)
    return (head.rstrip() + f"\n\n      ... {cut} bytes elided from the middle of "
            f"this record; the whole thing is on disk ...\n\n" + tail.lstrip())


def build_context(slug, nid):
    _, _, nodes, order, _ = load(slug)
    if nid not in nodes:
        die(f"no node {nid}")
    n = nodes[nid]
    a = type("A", (), {"slug": slug, "id": nid})()
    header = ""
    for line in open(plan_path(a.slug), encoding="utf-8"):
        if line.startswith("## goal:") or line.startswith("# "):
            header = line.strip(); break

    out = []
    if header:
        out.append(header)
    out.append(f"NODE {n['id']}: {n['title']}")
    if n["fields"].get("gate"):
        out.append(f"GATE: {n['fields']['gate']}")
    if is_irreversible(n):
        out.append("RISK: IRREVERSIBLE — do not run the gate unattended.")

    # G1: a session should know its context budget BEFORE it starts, not after
    # it compacts mid-node.
    ctx = [f.strip() for f in n["fields"].get("ctx", "").split(",") if f.strip()]
    if ctx:
        total = 0
        parts = []
        for f in ctx:
            try:
                sz = os.path.getsize(f); total += sz; parts.append(f"{f} ({sz}b)")
            except OSError:
                parts.append(f"{f} (does not exist yet)")
        out.append("FILES: " + ", ".join(parts))
        out.append(f"CONTEXT BUDGET: {total}b of {CTX_BUDGET}b" +
                   ("  ** OVER BUDGET — split this node **" if total > CTX_BUDGET else ""))

    parents = [d for d in needs_of(n)[0] if d in nodes]
    if parents:
        out.append(""); out.append("DEPENDS ON:")
        for d in parents:
            p = nodes[d]
            out.append(f"  [{p['mark']}] {d}. {p['title']}")
            if p["notes"]:
                out.append(f"      -> {p['notes'][-1]}")

    # G9: carry this node's own history forward, so a retry can see what the
    # previous attempt actually did rather than only that it failed.
    rec = read_record(a.slug, n)
    if rec:
        body = squeeze_record(rec)
        out.append(""); out.append("--- THIS NODE'S RECORD ---")
        out.append(body.strip())
        out.append("--- end record ---")
    out.append(""); out.append(f"WHEN DONE: plan gate {a.slug} {a.id}")
    return "\n".join(out)


def cmd_context(a):
    print(build_context(a.slug, a.id)); return 0


SHIMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
_GUARD = [None, False]   # (module, tried)


def _guard():
    """The harness guard, content/hooks/guard.py, three levels above this file in a checkout and in an
    install alike (src|lib / agent_harness / workgraph / plan.py)."""
    if not _GUARD[1]:
        _GUARD[1] = True
        p = os.environ.get("PLAN_GUARD") or os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "content", "hooks", "guard.py"))
        if p != "off" and os.path.isfile(p):
            import importlib.util
            spec = importlib.util.spec_from_file_location("agent_harness_guard", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _GUARD[0] = mod
    return _GUARD[0]


def guard_refusal(gate, root):
    """Why the guard refuses this gate, or None.

    A gate is a shell command this engine runs ITSELF, so it never passes the PreToolUse hook that
    judges the agent's own commands. In agentic-os, writing `gate: rm -rf ~` into a plan file and then
    running `plan gate` was arbitrary execution that no guard ever saw. Every gate is now judged first.
    With no guard to be found, gates are refused rather than run unguarded; PLAN_GUARD=off is the named
    way to accept that, so it can never happen by accident.
    """
    if os.environ.get("PLAN_GUARD") == "off":
        return None
    g = _guard()
    if g is None:
        return ("no guard found next to this engine (content/hooks/guard.py); set PLAN_GUARD=off to run "
                "gates unguarded")
    return g.verdict(gate, cwd=root)


def cmd_gate(a):
    path = plan_path(a.slug)
    _, lines, nodes, _, _ = load(a.slug)
    if a.id not in nodes:
        die(f"no node {a.id}")
    n = nodes[a.id]
    gate = n["fields"].get("gate")
    if not gate:
        die(f"node {a.id} has no gate; a node without one can never be settled")
    assert_sound(a.slug, a.id, getattr(a, "force", False))
    # RC-1 (red team 2): a worker swapped a claimed node's gate to `true`, let the
    # orchestrator run the substitute, and swapped it back — ALL DONE, exit 0,
    # over a gate that never passed, with nothing reported. Comparing at the two
    # round endpoints cannot see it. The authorised string is now passed in and
    # must still be on disk at gate time.
    authorised = getattr(a, "authorised_gate", None)
    if authorised is not None and gate != authorised:
        die(f"node {a.id}: the gate on disk is not the gate this run authorised — "
            f"refusing to execute it", 7)
    if is_irreversible(n) and not a.attended:
        die(f"node {a.id} is risk: irreversible — re-run with --attended, with a human present", 3)
    _assert_owner(n, getattr(a, "owner", None), getattr(a, "force", False), a.id)

    before_mark = n["mark"]
    before_owner = n["fields"].get("owner")
    before_lease = n["fields"].get("lease")

    print(f"$ {gate}", file=sys.stderr)
    t0 = time.time()
    try:
        root = os.path.dirname(plan_dir())
        # Gates call `plan` and `phase-check` by name; this directory resolves them wherever the
        # harness is installed, without anyone adding it to their own PATH.
        env = dict(os.environ, PATH=SHIMS + os.pathsep + os.environ.get("PATH", ""))
        refused = guard_refusal(gate, root)
        sb = _sandbox_prefix()
        if refused:
            # Not run at all. Recorded as an ordinary failed attempt, so the node returns to pending
            # with the reason as evidence and the same refusal twice reads as STUCK.
            code, blob = 126, "refused by the guard before it ran: %s\n" % refused
        elif sb:
            r = subprocess.run(sb + ["-c", gate], capture_output=True, text=True,
                               timeout=GATE_TIMEOUT, cwd=root, env=env)
            code, blob = r.returncode, (r.stdout + r.stderr)
        else:
            r = subprocess.run(gate, shell=True, capture_output=True, text=True,
                               timeout=GATE_TIMEOUT, cwd=root, env=env)
            code, blob = r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        code, blob = 124, f"timed out after {GATE_TIMEOUT}s"
    secs = int(time.time() - t0)

    sys.stderr.write(blob)
    sig = gate_signature(blob)
    tail = " / ".join(l.strip() for l in blob.strip().split("\n")[-3:] if l.strip())
    tail = (tail[:400] if tail else "(no output)")

    with Lock(path):
        _, lines, nodes, _, _ = load(a.slug)
        n = nodes[a.id]
        # R3 (red team): the settle write was unconditional. A gate that ran for
        # six seconds would overwrite whatever the node had become in the
        # meantime — silently destroying another session's live lease, 10/10.
        # Compare and swap: if the node moved while the gate ran, record the
        # evidence but do not settle someone else's node.
        moved = (n["mark"] != before_mark or n["fields"].get("owner") != before_owner
                 or n["fields"].get("lease") != before_lease)
        append_record(a.slug, n, f"Attempt {now_iso()} — {whoami()} — {secs}s — exit {code}"
                                 + (" (NOT APPLIED: node was re-claimed)" if moved else ""),
                      "```\n" + (blob.strip() or "(no output)") + "\n```")
        if moved:
            append_note(path, lines, n,
                        f"gate ran to exit {code} in {secs}s but the node had been "
                        f"re-claimed by {n['fields'].get('owner', '?')} — result NOT applied")
            print(f"CONFLICT {a.id}: re-claimed while the gate ran; result not applied",
                  file=sys.stderr)
            return 5
        append_note(path, lines, n, f"exit {code} in {secs}s [{sig}] — {tail}")
        _, lines, nodes, _, _ = load(a.slug)
        n = nodes[a.id]
        set_node(path, lines, n, mark=("x" if code == 0 else " "), lease=None)
    print(("PASS " if code == 0 else "FAIL ") + f"{a.id} (exit {code}, {secs}s)")
    return 0 if code == 0 else 1


def assert_sound(slug, nid, force=False):
    """RC-5 (red team 2): only the read-only commands consulted analyse(), so
    `gate`, `mark`, `note`, `claim` and `release` all wrote happily over a graph
    that `lint` called BROKEN — the |-truncation lint predicts was then performed
    silently by gate. A structural defect must stop every write, not just the
    reports."""
    if force:
        return
    _, _, nodes, order, mal = load(slug)
    st = analyse(nodes, order, mal)
    bad = dict(st["broken"])
    if nid in bad:
        die(f"node {nid} is BROKEN — {bad[nid]}. Fix it, or pass --force", 6)
    if mal:
        die(f"the plan file has {len(mal)} unparseable node-like line(s); run "
            f"`plan lint {slug}`. Pass --force to write anyway", 6)


def _assert_owner(n, owner, force, nid):
    """A worker told not to touch plan state is following a prompt, not a
    mechanism — and under bypassPermissions a prompt is not a control. Settling
    a node someone else holds clears their lease and hands their files to a
    third session."""
    held = n["fields"].get("owner")
    if n["mark"] == ">" and held and owner not in (None, held) and not force:
        die(f"node {nid} is leased to {held}; pass --force to override", 4)


def cmd_mark(a):
    path = plan_path(a.slug)
    with Lock(path):
        _, lines, nodes, _, _ = load(a.slug)
        if a.id not in nodes:
            die(f"no node {a.id}")
        n = nodes[a.id]
        _assert_owner(n, getattr(a, "owner", None), getattr(a, "force", False), a.id)
        assert_sound(a.slug, a.id, getattr(a, "force", False))
        if a.note:
            append_note(path, lines, n, a.note)
            _, lines, nodes, _, _ = load(a.slug); n = nodes[a.id]
        set_node(path, lines, n, mark=("x" if a.state == "done" else " "), lease=None)
    print(f"{a.id} -> {a.state}"); return 0


def cmd_note(a):
    path = plan_path(a.slug)
    with Lock(path):
        _, lines, nodes, _, _ = load(a.slug)
        if a.id not in nodes:
            die(f"no node {a.id}")
        assert_sound(a.slug, a.id, getattr(a, "force", False))
        append_note(path, lines, nodes[a.id], " ".join(a.text))
    return 0


def cmd_record(a):
    _, _, nodes, _, _ = load(a.slug)
    if a.id not in nodes:
        die(f"no node {a.id}")
    n = nodes[a.id]
    if a.summary:
        p = append_record(a.slug, n, f"Summary {now_iso()} — {whoami()}", " ".join(a.summary))
        print(p); return 0
    print(read_record(a.slug, n) or f"(no record yet for node {a.id})"); return 0


def cmd_show(a):
    _, _, nodes, order, mal = load(a.slug)
    st = analyse(nodes, order, mal)
    blocked_ids = [b[0] for b in st["blocked"]]
    broken_ids = [b[0] for b in st["broken"]]
    for nid in order:
        n = nodes[nid]
        if nid in st["ready"]:      flag = "  <- READY" + stuck_tag(n)
        elif nid in blocked_ids:    flag = "  <- blocked"
        elif nid in broken_ids:     flag = "  <- BROKEN"
        elif nid in st["held"]:     flag = "  <- HELD (irreversible)"
        elif st["eff"][nid] == ">": flag = f"  <- running ({n['fields'].get('owner','?')})"
        else:                       flag = ""
        print(f"[{st['eff'][nid]}] {nid}. {n['title']}{flag}")
        for note in n["notes"][-2:]:
            print(f"        {note}")
    for ln, text in mal:
        print(f"[!] line {ln}: {text}")
    print(f"\n{len(st['done'])} done, {len(st['ready'])} ready, {len(st['running'])} running, "
          f"{len(st['blocked'])} blocked, {len(st['held'])} held, "
          f"{len(st['broken']) + len(mal)} broken")
    return 0


def _classify(slug):
    """Why a graph stopped, not merely that it did. `plan list` gives counts, and
    counts cannot tell "finished" from "abandoned mid-round three hours ago"."""
    try:
        _, _, nodes, order, mal = load(slug)
    except (SystemExit, Exception):
        # Deliberately everything short of KeyboardInterrupt. This runs once per
        # entry in plan/ on behalf of `plan resume`, and `plan resume`'s exit
        # code is what the SessionStart hook keys on: exit 0 means "nothing
        # outstanding", and a traceback exits 1 but prints nothing the hook will
        # inject, so one unreadable entry made the hook report the exact
        # opposite of the truth about every OTHER plan in the directory. One bad
        # entry is allowed to be unreadable; it is not allowed to decide what
        # the session is told about the rest.
        # The reason stays a constant. This string is printed by `plan resume`,
        # and the hook wraps that output in trusted prose and injects it -- the
        # same channel safe_slug() exists to defend, so an exception message
        # built out of file content does not get to travel down it.
        return ("unreadable", "cannot be read — `plan lint` says why", 0, 0, False)
    st = analyse(nodes, order, mal)
    done, total = len(st["done"]), len(order)
    if mal or st["broken"]:
        why = (st["broken"][0][1] if st["broken"]
               else f"{len(mal)} line(s) look like nodes but do not parse")
        return ("BROKEN", why, done, total, False)
    if not total:
        return ("empty", "no nodes", 0, 0, False)

    live = [i for i in st["running"]
            if (parse_iso(nodes[i]["fields"].get("lease", "")) or datetime.min.replace(
                tzinfo=timezone.utc)) > datetime.now(timezone.utc) - timedelta(hours=LEASE_HOURS)]
    # A node whose lease expired is work someone started and never finished.
    stale = [i for i in order
             if nodes[i]["mark"] == ">" and effective_mark(nodes[i]) == " "]
    fresh = [i for i in st["ready"] if repeated_failures(nodes[i]) < 2]
    stuck = [i for i in st["ready"] if repeated_failures(nodes[i]) >= 2]

    if live:
        return ("in flight", f"{len(live)} node(s) leased by another session",
                done, total, False)
    if stale:
        ts = parse_iso(nodes[stale[0]]["fields"].get("lease", ""))
        age = (f", oldest {int((datetime.now(timezone.utc) - ts).total_seconds() // 3600)}h ago"
               if ts else "")
        return ("abandoned", f"{len(stale)} node(s) started and never settled{age}",
                done, total, True)
    if fresh:
        return ("ready", f"{len(fresh)} node(s) can start now", done, total, True)
    if stuck:
        return ("stuck", f"{len(stuck)} node(s) failed the same way twice",
                done, total, False)
    if st["held"]:
        return ("held", f"{len(st['held'])} irreversible node(s) need --attended",
                done, total, False)
    if st["blocked"]:
        return ("blocked", f"{len(st['blocked'])} node(s) waiting on a dependency",
                done, total, False)
    return ("done", "", done, total, False)


SAFE_SLUG_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def safe_slug(slug):
    """A slug for DISPLAY. A slug is a filename, and a filename survives
    `git clone`.

    M2 (external certification): the slug was printed raw into `plan resume`'s
    output, which the SessionStart hook injects into the next session's context.
    A plan file whose name contained newlines produced a forged
    `OPERATOR DIRECTIVE` block inside that injected text, wrapped in the hook's
    own vouching prose -- so cloning a repository was enough to put
    attacker-chosen instructions in front of the next session, laundered as
    trusted local machinery.

    Anything outside the safe set is shown as a repr, which is unrunnable and
    obviously wrong rather than quietly convincing.
    """
    return slug if SAFE_SLUG_RE.match(slug) else repr(slug)


def cmd_resume(a):
    d = plan_dir()
    if not os.path.isdir(d):
        print(f"(no plan/ directory at {d})")
        return 0
    slugs = sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md"))
    rows, outstanding, resumable = [], 0, []
    for slug in slugs:
        state, why, done, total, can = _classify(slug)
        if state == "done":
            continue
        outstanding += 1
        rows.append((slug, state, why, done, total, can))
        if can:
            resumable.append(slug)

    if not rows:
        print("nothing outstanding — every plan is done")
        return 0

    print(f"{outstanding} plan(s) with outstanding work\n")
    for slug, state, why, done, total, can in rows:
        print(f"  {safe_slug(slug):<16} {done}/{total:<4} {state:<10} {why}")
        if can:
            print(f"  {'':<16} -> plan run {safe_slug(slug)} --workers {a.workers}")
    if not a.run:
        return 1

    if not resumable:
        print("\nnothing is auto-resumable; the states above need a human")
        return 1
    # This demanded --accept-risks for a day after `plan run` stopped requiring
    # it, and the line above printed the same dead flag. A tool that tells you
    # to type a flag it no longer accepts is describing a version of itself
    # that is gone -- the same defect as the run refusal that cited five
    # already-fixed bugs. resume.test.sh runs the command this prints.
    rc = 0
    for slug in resumable:
        print(f"\n=== resuming {slug} ===")
        ns = type("A", (), {"slug": slug, "workers": a.workers, "max_rounds": a.max_rounds,
                            "timeout": a.timeout, "model": a.model, "dry_run": False,
                            "accept_risks": True})()
        rc = cmd_run(ns) or rc
    return rc


def progress_line(st, order, nodes, rnd=None):
    """One line, printed every round, answering the only question a human has
    while an unattended run drains: is it moving, and how far along?

    A round number alone does not answer that. Round 7 of a 40-node graph looks
    identical whether 2 nodes are done or 30, so a long run is indistinguishable
    from a hang, and the honest response to that is to kill it and look -- which
    is the manual babysitting the orchestrator exists to remove.

    Counts appear only when non-zero. A line that always ends `0 stuck 0 held`
    trains the eye to skip the whole line, and then the one round where it says
    `1 STUCK` is skipped too.
    """
    total = len(order)
    done = len(st["done"])
    pct = int(round(100.0 * done / total)) if total else 0
    filled = int(round(12.0 * done / total)) if total else 0
    bar = "\u2588" * filled + "\u2591" * (12 - filled)
    stuck = [i for i in st["ready"] if repeated_failures(nodes[i]) >= 2]
    ready = [i for i in st["ready"] if i not in stuck]
    bits = [f"{done}/{total} done"]
    for label, seq in (("running", st["running"]), ("ready", ready),
                       ("blocked", st["blocked"]), ("held", st["held"]),
                       ("STUCK", stuck)):
        if seq:
            bits.append(f"{len(seq)} {label}")
    tail = f"   round {rnd}" if rnd is not None else ""
    return f"[{bar}] {pct:>3}%  " + " \u00b7 ".join(bits) + tail


def cmd_status(a):
    """A dashboard, not a report. Everything a dev wants mid-run and nothing
    else: how far along, what is moving right now and for how long, what is
    waiting, and the last thing that actually failed."""
    _, _, nodes, order, mal = load(a.slug)
    st = analyse(nodes, order, mal)
    total = len(order) or 1
    done = len(st["done"])
    pct = int(round(100.0 * done / total))
    filled = int(round(12.0 * done / total))
    bar = "\u2588" * filled + "\u2591" * (12 - filled)

    goal = ""
    for line in open(plan_path(a.slug), encoding="utf-8"):
        if line.startswith("## goal:"):
            goal = line.split(":", 1)[1].strip()
            break
    print(f"{goal[:44]:<44} {done}/{len(order)}  {bar}  {pct}%")

    def row(tag, nid, extra=""):
        print(f"  {tag:<9} {nid:>2}. {nodes[nid]['title'][:44]:<44} {extra}")

    now = datetime.now(timezone.utc)
    for nid in st["running"]:
        n = nodes[nid]
        ts = parse_iso(n["fields"].get("lease", ""))
        age = f"{int((now - ts).total_seconds() // 60)}m" if ts else "?"
        row("running", nid, f"{n['fields'].get('owner', '?')}  {age}")
    for nid in st["ready"]:
        r = repeated_failures(nodes[nid])
        if r >= 2:
            row("STUCK", nid, f"same failure {r}x")
        else:
            row("ready", nid)
    for nid in st["held"]:
        row("HELD", nid, "risk: irreversible")
    for nid, why in st["broken"]:
        row("BROKEN", nid, why[:40])
    blocked = [str(b[0]) for b in st["blocked"]]
    if blocked:
        print(f"  {'blocked':<9} {', '.join(blocked)}")
    if mal:
        print(f"  {'unparsed':<9} {len(mal)} line(s) look like nodes but do not parse")

    runs = fails = 0
    durs = []
    last_fail = None
    for nid in order:
        for att in attempts(nodes[nid]):
            runs += 1
            durs.append(att["secs"])
            if att["code"] != "0":
                fails += 1
                last_fail = (nid, att["code"], att["tail"])
    durs.sort()
    p50 = durs[len(durs) // 2] if durs else 0
    print(f"  {'gates':<9} {runs} runs \u00b7 {fails} failed \u00b7 p50 {p50}s")
    if last_fail:
        nid, code, tail = last_fail
        print(f"  {'last fail':<9} {nid}. exit {code} \u2014 {tail[:60]}")
    return 0


def cmd_stats(a):
    """G5/G6: the harness metric. Without duration and attempt counts, 'is this
    better than what we had' is unfalsifiable, and the 4h lease stays a guess."""
    _, _, nodes, order, mal = load(a.slug)
    st = analyse(nodes, order, mal)
    durs, tries, fails, stuck = [], 0, 0, 0
    for nid in order:
        a_ = attempts(nodes[nid])
        tries += len(a_)
        fails += sum(1 for x in a_ if x["code"] != "0")
        durs += [x["secs"] for x in a_]   # 0s gates are the common case, not noise
        if repeated_failures(nodes[nid]) >= 2:
            stuck += 1
    durs.sort()
    import math
    p = (lambda q: durs[max(0, min(int(math.ceil(len(durs) * q)) - 1, len(durs) - 1))]) \
        if durs else (lambda q: 0)
    print(f"nodes            {len(order)} ({len(st['done'])} done, {len(st['ready'])} ready, "
          f"{len(st['blocked'])} blocked, {len(st['held'])} held)")
    print(f"gate runs        {tries} ({fails} failed, {tries - fails} passed)")
    print(f"attempts/node    {tries / len(order):.2f}" if order else "attempts/node    n/a")
    print(f"gate duration    p50 {p(0.5)}s  p95 {p(0.95)}s  max {durs[-1] if durs else 0}s")
    print(f"stuck nodes      {stuck}")
    if durs:
        # A lease must cover a whole round: worker time plus the gates in the
        # batch, not just the longest gate. Ignoring worker time guarantees
        # expiry mid-round at higher --workers.
        rec = max(1.0, round(((GATE_TIMEOUT + 3 * durs[-1]) * 3) / 3600.0, 1))
        print(f"lease suggestion {rec}h  (covers worker timeout + gates in a round; "
              f"PLAN_LEASE_HOURS={LEASE_HOURS})")
    return 0


def cmd_lint(a):
    _, _, nodes, order, mal = load(a.slug)
    st = analyse(nodes, order, mal)
    bad = len(st["broken"]) + len(mal)
    _report_defects(st, nodes)
    over = 0
    for nid in order:
        ctx = [f.strip() for f in nodes[nid]["fields"].get("ctx", "").split(",") if f.strip()]
        tot = sum(os.path.getsize(f) for f in ctx if os.path.exists(f))
        if tot > CTX_BUDGET:
            print(f"OVERSIZE {nid}. {nodes[nid]['title']} — ctx {tot}b > {CTX_BUDGET}b; split it")
            over += 1
    if bad == 0 and over == 0:
        print("LINT OK"); return 0
    print(f"LINT: {bad} broken, {over} oversize"); return 1


def cmd_list(a):
    d = plan_dir()
    if not os.path.isdir(d):
        print(f"(no plan/ directory at {d})"); return 0
    for f in sorted(os.listdir(d)):
        if f.endswith(".md"):
            slug = f[:-3]
            try:
                _, _, nodes, order, mal = load(slug)
                st = analyse(nodes, order, mal)
                print(f"{slug:24} {len(st['done'])}/{len(order)} done, {len(st['ready'])} ready, "
                      f"{len(st['broken']) + len(mal)} broken")
            except (SystemExit, Exception):
                # Same rule as _classify: one entry nobody can read must cost
                # you that entry, not the listing of every other plan.
                print(f"{slug:24} (unreadable)")
    return 0


# ------------------------------------------------------------------ doctor

LOCK_SUSPECT = 60     # seconds; a writer holds a lock for milliseconds, never this long


def _probe(cmd, timeout=15):
    """First line of a version-ish command, or None if it cannot be run at all.
    Never raises: a doctor that dies on its first missing tool reports nothing
    about the other nine."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or r.stderr or "").strip().split("\n")
    return out[0].strip() or None


def _writable(d):
    """Actually write, rather than ask os.access. access() answers from the mode
    bits, which lie under root, on read-only mounts and on network filesystems —
    and the whole point of this check is what happens at 3am, not what the bits
    claim."""
    probe = os.path.join(d, f".doctor-probe-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("")
        os.unlink(probe)
        return True
    except OSError:
        return False


def cmd_doctor(a):
    """Is this machine fit to be left draining a graph overnight?

    Every check here is something that has, or plainly would, turn an unattended
    run into silent damage: a missing worker binary fails every dispatch, an
    unwritable plan/ throws away the evidence the whole design rests on, a lease
    shorter than a gate timeout hands one node to two workers, and a stale lock
    is a thing you want named now rather than discovered at 2am.

    Warnings degrade the run; failures mean do not start it. Exit 0 iff there
    are no failures."""
    import shutil
    import textwrap

    rows = []

    def add(level, name, detail):
        rows.append((level, name, detail))

    # --- the interpreter gates will actually shell out to ---------------
    mine = ".".join(str(x) for x in sys.version_info[:3])
    py = shutil.which("python3")
    if py is None:
        add("FAIL", "python", f"no python3 on PATH — plan is running under {sys.executable} "
                              f"({mine}), but any gate that shells out to python3 cannot run")
    else:
        # Compare version and prefix, not the executable path: /usr/bin/python3
        # is a stub that execs the real one, so comparing paths cries wolf. What
        # decides whether a gate finds its imports is the prefix it resolves to.
        sig = _probe([py, "-c", "import sys;print('%s %s' % "
                                "('.'.join(map(str, sys.version_info[:3])), sys.prefix))"])
        if sig is None:
            add("FAIL", "python", f"{py} is on PATH but will not run — every gate that shells "
                                  f"out to python3 dies before its first line")
        elif sig != f"{mine} {sys.prefix}":
            add("warn", "python", f"gates get {py} ({sig}); plan itself runs under "
                                  f"{sys.executable} ({mine} {sys.prefix}) — two interpreters "
                                  f"means two sets of installed packages")
        else:
            add("ok", "python", f"{mine} at {py}")

    # --- the worker binary the orchestrator dispatches -------------------
    claude = shutil.which("claude")
    cver = _probe([claude, "--version"]) if claude else None
    if cver is None:
        add("FAIL", "claude", "not runnable on PATH — `plan run` dispatches `claude -p` for "
                              "every node, so an unattended drain would burn its whole round "
                              "cap on dispatch failures and change nothing")
    else:
        add("ok", "claude", f"{cver} at {claude}")

    # --- the property tier's only dependency -----------------------------
    hv = _probe([py or sys.executable, "-c", "import hypothesis; print(hypothesis.__version__)"])
    if hv:
        add("ok", "hypothesis", f"{hv} — the property tier can generate")
    else:
        add("warn", "hypothesis", "not importable — `pytest -m property` then collects zero "
                                  "tests, and a tier over zero tests passes vacuously")

    # --- where the evidence gets written ---------------------------------
    d = plan_dir()
    entries = sorted(f for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []
    goals = [f[:-3] for f in entries if os.path.isfile(os.path.join(d, f))]
    odd = [f for f in entries if not os.path.isfile(os.path.join(d, f))]
    if odd:
        # Named here because doctor is where you find out about a thing before
        # 2am. Every listing command now skips these rather than opening them,
        # but an entry ending .md that is a directory or a fifo is not a plan
        # and will never drain -- it is either a mistake or someone's idea of one.
        add("warn", "plan dir", f"{len(odd)} entry(ies) end .md but are not regular files "
                                f"({', '.join(safe_slug(f) for f in odd[:3])}) — they are "
                                f"skipped, never loaded, and will never drain")
    if not os.path.isdir(d):
        add("FAIL", "plan dir", "does not exist — there is nothing here to drain")
    elif not _writable(d):
        add("FAIL", "plan dir", "is not writable — every claim, note and gate result would be "
                                "lost, so a restarted drain would redo work already done")
    else:
        add("ok", "plan dir", f"writable, {len(goals)} goal file(s)")

    # --- the setting that decides whether two workers get one node -------
    lease_s = LEASE_HOURS * 3600
    if lease_s <= GATE_TIMEOUT:
        add("FAIL", "lease", f"PLAN_LEASE_HOURS={LEASE_HOURS} ({lease_s:.0f}s) is not longer "
                             f"than one gate timeout ({GATE_TIMEOUT}s) — a lease can expire "
                             f"while its own gate is still running, and the node is then "
                             f"handed to a second worker")
    elif lease_s < GATE_TIMEOUT * 3:
        add("warn", "lease", f"PLAN_LEASE_HOURS={LEASE_HOURS} ({lease_s:.0f}s) leaves little "
                             f"room over a {GATE_TIMEOUT}s gate timeout; a round is worker "
                             f"time plus the gates in the batch. `plan stats` suggests a value")
    else:
        add("ok", "lease", f"PLAN_LEASE_HOURS={LEASE_HOURS} covers a {GATE_TIMEOUT}s gate "
                           f"timeout {lease_s / GATE_TIMEOUT:.0f}x over")

    # --- things a previous run may have left behind ----------------------
    for f in (sorted(os.listdir(d)) if os.path.isdir(d) else []):
        if not f.endswith(".lock"):
            continue
        try:
            age = time.time() - os.path.getmtime(os.path.join(d, f))
        except OSError:
            continue
        if age > LOCK_SUSPECT:
            if holder_is_dead(os.path.join(d, f)):
                reclaimed = "its holder is gone, so the next writer reclaims it immediately"
            elif age > GATE_TIMEOUT:
                reclaimed = "it is already old enough to be reclaimed by the next writer"
            else:
                reclaimed = f"writers block on it until it is {GATE_TIMEOUT}s old"
            add("warn", "locks", f"{f} has been held {int(age)}s — no writer holds one for "
                                 f"more than a moment, so its holder died; {reclaimed}")

    for slug in goals:
        try:
            _, _, nodes, order, mal = load(slug)
        except (SystemExit, Exception):
            add("warn", "goal files",
                f"{safe_slug(slug)}.md does not load — `plan lint {safe_slug(slug)}` says why")
            continue
        dead = [n["id"] for n in nodes.values() if n["mark"] == ">" and effective_mark(n) == " "]
        if dead:
            add("warn", "leases", f"{slug}: node(s) {', '.join(str(i) for i in dead)} are still "
                                  f"marked [>] under an expired lease — a drain died without "
                                  f"releasing them. They are reclaimable and will re-dispatch")

    # --- room to write for a whole night ---------------------------------
    try:
        stv = os.statvfs(d if os.path.isdir(d) else os.getcwd())
        free = stv.f_bavail * stv.f_frsize
    except OSError:
        free = None
    if free is not None:
        gb = free / (1024.0 ** 3)
        if free < 200 * 1024 ** 2:
            add("FAIL", "disk", f"{gb:.1f}G free — records and gate output are written on every "
                                f"attempt; a full volume corrupts the save-and-rename")
        elif free < 2 * 1024 ** 3:
            add("warn", "disk", f"{gb:.1f}G free on the volume holding plan/")
        else:
            add("ok", "disk", f"{gb:.1f}G free on the volume holding plan/")

    # --- whether the record survives the machine -------------------------
    if os.path.isdir(d) and _probe(["git", "-C", d, "rev-parse", "--show-toplevel"]):
        add("ok", "git", "plan/ is inside a git repo, so records are versioned")
    else:
        add("warn", "git", "plan/ is not inside a git repo — the records written to survive "
                           "context loss are themselves unversioned and unrecoverable")

    print(f"plan doctor — {d}\n")
    for level, name, detail in rows:
        head = f"  {level:<4} {name:<11} "
        print(textwrap.fill(detail, width=94, initial_indent=head,
                            subsequent_indent=" " * len(head),
                            break_long_words=False, break_on_hyphens=False))
    fails = sum(1 for r in rows if r[0] == "FAIL")
    warns = sum(1 for r in rows if r[0] == "warn")
    print()
    if fails:
        print(f"DOCTOR: {fails} blocking, {warns} warning(s) — do not start an unattended drain")
        return 1
    print(f"DOCTOR OK — {warns} warning(s), fit for an unattended drain" if warns
          else "DOCTOR OK — fit for an unattended drain")
    return 0


# ------------------------------------------------------------- orchestrator

WORKER_INSTRUCTIONS = """

--- YOU ARE A WORKER ---
Do the work this node describes, and nothing else.

Do NOT run any `plan` command and do NOT edit the plan file. The orchestrator
owns every state transition and will run the gate itself once you return — a
worker's claim of success is not evidence, the gate's exit code is. You may run
the gate command read-only to check yourself, but the result that counts is the
orchestrator's.

Touch only the files this node names. Another worker is very likely editing a
different node at this moment.

Report in two lines: what you changed, and anything the next attempt should know.
"""


def _dispatch(slug, nid, timeout, model=None):
    """Run one worker. Returns dispatch health, never task truth."""
    prompt = build_context(slug, nid) + WORKER_INSTRUCTIONS
    # Scope what a worker inherits. `claude -p` loads PROJECT-scoped MCP servers
    # without the approval prompt an interactive session shows, so a .mcp.json in
    # any repo plan run is pointed at would execute silently, once per worker per
    # round. --setting-sources user drops project/local settings;
    # --strict-mcp-config admits no MCP server we did not pass explicitly.
    cmd = _sandbox_prefix() + ["claude", "-p", prompt, "--output-format", "json",
                               "--setting-sources", "user", "--strict-mcp-config"]
    budget = os.environ.get("PLAN_WORKER_BUDGET")
    if budget:
        # Bounds the "STUCK burns the round cap at API cost" defect at the
        # dispatch boundary instead of trying to detect it afterwards.
        cmd += ["--max-budget-usd", budget]
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        _LIVE.add(proc)
        out, _err = proc.communicate(timeout=timeout)
        raw = (out or "").strip()
        if proc.returncode is not None and proc.returncode < 0:
            return {"id": nid, "ok": False,
                    "why": f"worker killed by signal {-proc.returncode}",
                    "quota": False, "dispatch_failed": True, "denials": 0,
                    "secs": int(time.time() - t0), "cost": 0, "text": ""}
    except subprocess.TimeoutExpired:
        if proc is not None:
            try:
                proc.kill(); proc.communicate(timeout=5)
            except Exception:
                pass
        return {"id": nid, "ok": False, "why": f"worker timed out after {timeout}s",
                "quota": False, "dispatch_failed": True, "denials": 0,
                "secs": int(time.time() - t0), "cost": 0, "text": ""}
    except Exception as e:
        # Anything else — claude missing from PATH, OSError, a decode fault. An
        # uncaught raise here propagates out of ex.map and leaves every claimed
        # node marked [>] under a dead owner for the whole lease.
        return {"id": nid, "ok": False, "why": f"dispatch crashed: {e}",
                "quota": False, "dispatch_failed": True, "denials": 0,
                "secs": int(time.time() - t0), "cost": 0, "text": ""}
    finally:
        if proc is not None:
            _LIVE.discard(proc)
    info = {}
    try:
        info = json.loads(raw) if raw.startswith("{") else {}
    except ValueError:
        info = {}
    # api_error_status is how quota/rate-limit is told apart from the worker
    # simply failing its task. Conflating them makes a harness thrash on a wall.
    return {"id": nid, "secs": int(info.get("duration_ms", (time.time() - t0) * 1000) / 1000),
            # An equivalent-cost ESTIMATE, not a bill: this runs on a Max
            # subscription where nothing is charged per call. Useful only as a
            # proxy for how big the per-dispatch packet got.
            "cost": info.get("total_cost_usd", 0) or 0,
            "denials": len(info.get("permission_denials") or []),
            "text": str(info.get("result", ""))[:300],
            **classify_worker(info)}


def classify_worker(info):
    """Turn one `claude -p --output-format json` envelope into a dispatch verdict.

    Pure, so the wall conditions can be tested without a worker. Returns ok / quota /
    overloaded / auth / dispatch_failed / why.
    """
    api_err = info.get("api_error_status")
    errored = bool(info.get("is_error", True))
    result = str(info.get("result", "")).lower()[:400]
    # The result TEXT only indicates quota when the worker actually errored.
    # A successful worker reporting "added a retry for the API usage limit" is
    # not a quota event, and treating it as one throws away completed work and
    # writes a false reason into the durable record.
    # 429 is your rate limit or quota. 529 is the API being OVERLOADED — a
    # transient server condition that is not your fault and not your allowance.
    # Both are reasons to stop dispatching rather than retry into a wall, but
    # writing "quota" into a durable record for a 529 is a false reason, and the
    # note is what the next session reads. Observed live: seven workers halted
    # with $0.00 spent while the API was overloaded.
    overloaded = str(api_err) == "529"
    quota = str(api_err) == "429" or overloaded or (
        errored and ("quota" in result or "usage limit" in result))
    # A DEAD CREDENTIAL IS A WALL, NOT A TASK FAILURE, and it is the third wall this
    # loop has met. Observed 2026-09-08: six dispatches across two rounds, every one
    # returning in 58ms with "Failed to authenticate: OAuth session expired and could
    # not be refreshed", each recorded against a different node as though that node had
    # somehow failed. Two more rounds would have burned three nodes to STUCK for a
    # reason that has nothing to do with any of them. Halt the run and name the fix.
    auth = errored and ("authenticate" in result or "oauth" in result
                        or "not logged in" in result or "invalid api key" in result)

    # `subtype` describes how the TURN ended, not whether the TASK did: it is "success"
    # even when is_error is true. Printing it as the reason produced the line
    # "DISPATCH FAILED — success", which says nothing and actively misleads — it hid a
    # dead OAuth session behind the word that means the opposite. When the worker
    # errored, the reason is what the worker actually said.
    if errored:
        first = str(info.get("result") or "").strip().splitlines()
        why = (first[0][:160] if first else "") or str(
            info.get("terminal_reason") or info.get("subtype") or "no-json")
    else:
        why = info.get("subtype", "no-json")

    return {"ok": not errored, "quota": quota, "auth": auth,
            "dispatch_failed": errored and not quota and not auth,
            "overloaded": overloaded, "why": why}


def _announce_new(nodes, order, known, announced):
    """Name every node that appeared after the run started, once each.

    A worker can append a node, and its `gate:` is a shell string this loop
    would then execute unattended. Such a node is made VISIBLE but is never
    dispatched — it waits for a human to read it and re-run.

    M1 (external certification): the notice printed the node's TITLE while
    telling the reader to review its GATE, and never showed the gate at all. A
    socially engineered title ("cleanup temp files") over an unseen shell
    command is not a review prompt — it is the attacker writing the review.
    Show the gate verbatim, and label which is which."""
    fresh = [i for i in order if i not in known and i not in announced]
    for i in fresh:
        announced.add(i)
        print(f"NEW NODE {i}. appeared mid-run — NOT dispatched. "
              f"Review the gate below, then re-run.")
        print(f"    title: {nodes[i]['title']}")
        print(f"    gate:  {nodes[i]['fields'].get('gate') or '(none — it can never run)'}")
    return fresh


_PATHISH_RE = re.compile(r"[A-Za-z0-9_./~+-]{2,}")


def gate_files(cmd, root, depth=3, owned=()):
    """See the docstring below. `owned` is the node's declared ctx: files."""
    """Every local file a gate command depends on, hashed.

    The project rests on one sentence: a worker's report is a claim, and the
    gate's exit code is the evidence. That sentence is FALSE if the worker can
    edit the gate. Verified against the real tool: a worker that rewrote its own
    gate script to `exit 0` got PASS, [x], and 100% done, with no tamper report,
    because the integrity envelope covered the plan file and nothing else.

    Found by the first real workload rather than by any of 480 stubbed
    assertions -- a real worker touched a gate script for an innocent reason
    (adding `|| exit 1` to a `cd`), which is what made me check whether it could
    have done worse.

    Recurses into the scripts it finds, because pinning `./run-tests.sh` while
    leaving the helper it execs unpinned moves the hole rather than closing it.
    Depth-capped: this is a dependency check, not a build system.
    """
    import hashlib
    root = os.path.abspath(root)
    # A gate almost always NAMES the thing the work produces -- `test -f a.txt`,
    # `pytest tests/test_new.py`. Pinning those makes every honest run fail, and
    # the first version of this did exactly that. The distinction that matters
    # is not what the gate touches, it is what JUDGES the work:
    #   - a file the node declares in ctx: is its to edit, by definition
    #   - a file that does not exist yet cannot be a rewritten judge
    # Everything else the gate depends on is the judge, and is pinned.
    ownset = {os.path.abspath(os.path.expanduser(o)) for o in owned if o}
    ownset |= {os.path.abspath(os.path.join(root, o)) for o in owned if o}
    found, queue, seen = {}, [(cmd, depth)], set()
    while queue:
        text, d = queue.pop()
        for tok in _PATHISH_RE.findall(text):
            for cand in (tok, os.path.join(root, tok)):
                fp = os.path.abspath(os.path.expanduser(cand))
                if fp in seen or not os.path.isfile(fp) or fp in ownset:
                    continue
                seen.add(fp)
                try:
                    with open(fp, "rb") as fh:
                        blob = fh.read(1 << 20)
                except OSError:
                    continue
                found[fp] = hashlib.sha1(blob).hexdigest()
                # Only follow things inside the project. Recursing into
                # /bin/bash or a system python would pin the OS.
                if d > 0 and fp.startswith(root + os.sep):
                    try:
                        queue.append((blob.decode("utf-8", "replace"), d - 1))
                    except Exception:
                        pass
    return found


def _work_one(slug, nid, timeout, model, owner, authorised=None, gatepin=None):
    """Dispatch one node, then gate it — both inside the worker pool, so gating
    is concurrent too. A worker that never ran is never gated: dispatch failure
    must not be recorded as evidence about the task."""
    r = _dispatch(slug, nid, timeout, model)
    if r["quota"] or r.get("dispatch_failed"):
        return r
    g = type("A", (), {"slug": slug, "id": nid, "attended": False,
                       "owner": owner, "force": False,
                       "authorised_gate": authorised})()
    # Verify the gate's own scripts BEFORE running it. After is too late: the
    # rewritten gate has already produced its exit 0 and the only thing left to
    # do with it is throw it away, which is indistinguishable from a failure the
    # worker caused honestly.
    if gatepin is not None:
        now_files = gate_files(gatepin["cmd"], gatepin["root"],
                               owned=gatepin.get("owned", ()))
        # Only files pinned AT SNAPSHOT TIME count. A file the round created is
        # the work product, not a judge that was swapped.
        changed = sorted(f for f, h in gatepin["files"].items()
                         if now_files.get(f) != h)
        if changed:
            r["gate_tampered"] = changed
            r["gate_rc"] = 1
            return r
    try:
        r["gate_rc"] = cmd_gate(g)
    except SystemExit as e:
        r["gate_rc"] = e.code if isinstance(e.code, int) else 1
    except Exception as e:
        # R4 (red team): _work_one caught only SystemExit, so a gate emitting a
        # non-UTF-8 byte, an unwritable record, or a duplicate id escaped
        # ex.map, discarded a COMPLETED gate and aborted the whole run — and
        # the finally-release died too, stranding every claimed node for 4h.
        r["gate_rc"] = 1
        r["gate_error"] = f"{type(e).__name__}: {e}"
    return r


def cmd_run(a):
    """Drain a graph with N concurrent workers.

    The orchestrator does the claiming, the dispatching and ALL the gating.
    Workers only change files. That split is the whole reliability story: a
    worker reporting success is a claim, and the gate is the evidence."""
    from concurrent.futures import ThreadPoolExecutor, wait as cf_wait

    slug = a.slug
    # This refused by default and printed five known-open defects. All five are
    # now closed and each carries a regression test: identity is per-terminal
    # (plan.test.sh), the STUCK signature is whole-output and correct on six real
    # runners (signature.test.sh), SIGHUP releases in-flight claims, cycle
    # membership is Tarjan SCC checked against a reference, and fenced blocks --
    # including the unclosed case that silently swallowed a graph -- are caught.
    #
    # Leaving the refusal up while citing fixed defects would be a lie in the
    # tool, which is the exact class of thing this project exists to remove. The
    # flag is still accepted so older invocations keep working.
    #
    # What is NOT claimed: no external adversarial pass has completed (three
    # attempts, all lost to server overload), and no real workload has run
    # through this. The safety that matters is structural and unchanged --
    # irreversible nodes are held, a malformed graph is refused, a worker that
    # writes the plan file is caught and the round voided, and the gate is run
    # by the orchestrator rather than trusted from the worker.
    if not os.environ.get("PLAN_QUIET"):
        print("plan run: unattended. Irreversible nodes are held for you; a "
              "malformed graph is refused.", file=sys.stderr)
        print("  Certified 2026-08-19 (29 agents, 7 lenses, 20 findings closed) "
              "and drained once for real: 6 nodes, 6 gates, 0 failures.",
              file=sys.stderr)
    owner = f"orch/{procid()}"
    spent, dispatched, passed, failed, crashed = 0.0, 0, 0, 0, 0
    known = None
    announced = set()
    capped = True

    for rnd in range(1, a.max_rounds + 1):
        _, _, nodes, order, mal = load(slug)
        st = analyse(nodes, order, mal)

        # Order matters: a file whose ONLY node is malformed has an empty
        # order, and reporting "EMPTY — no nodes" there hides the defect behind
        # a message that reads like an empty file. Name the broken line first.
        if mal or st["broken"]:
            print("REFUSING TO RUN — fix the graph first:")
            _report_defects(st, nodes)
            return 2
        if not order:
            print("EMPTY — no nodes in this plan")
            return 1
        if known is None:
            known = set(order)
        _announce_new(nodes, order, known, announced)

        # flush: this is the only sign of life during a long round, and a
        # block-buffered pipe would hold it back until the run ended.
        print(progress_line(st, order, nodes, rnd), flush=True)

        pool = [i for i in st["ready"] if i in known and repeated_failures(nodes[i]) < 2]
        for i in pool:
            ctxf = [f.strip() for f in nodes[i]["fields"].get("ctx", "").split(",") if f.strip()]
            tot = sum(os.path.getsize(f) for f in ctxf if os.path.exists(f))
            if tot > CTX_BUDGET:
                # Advisory, not blocking: an oversize node still runs, but the
                # worker will likely compact mid-node, so say it out loud.
                print(f"  warning: node {i} ctx is {tot}b over the {CTX_BUDGET}b budget — "
                      f"consider splitting it")
        skipped = [i for i in st["ready"] if repeated_failures(nodes[i]) >= 2]

        # Always visible, whatever ends the run — a node nothing will ever
        # dispatch must never be reported as merely "ready".
        for i in skipped:
            print(f"STUCK {i}. {nodes[i]['title']} — same failure twice, left for a human")
        for i in st["held"]:
            print(f"HELD {i}. {nodes[i]['title']} — risk: irreversible; settle it with "
                  f"`plan gate {slug} {i} --attended`")

        if not pool:
            capped = False
            if st["running"]:
                print(f"round {rnd}: nothing dispatchable; "
                      f"{len(st['running'])} still leased elsewhere")
            elif not skipped and not st["held"] and not st["blocked"] \
                    and not [x for x in order if x not in known]:
                # M1: a node that appeared mid-run is never in the pool, so an
                # empty pool used to read as completion — ALL DONE printed in
                # the same round as an injected node nothing had dispatched.
                # Work that has not been dispatched is outstanding work.
                print(f"ALL DONE after {rnd - 1} round(s)")
            break

        batch = pool[:a.workers]
        claimed = []
        for nid in batch:
            path = plan_path(slug)
            with Lock(path):
                _, lines, nodes2, order2, mal2 = load(slug)
                st2 = analyse(nodes2, order2, mal2)
                if nid not in st2["ready"]:
                    continue
                set_node(path, lines, nodes2[nid], mark=">", lease=now_iso(), owner=owner)
                ensure_record(slug, nodes2[nid])
                claimed.append(nid)
        if not claimed:
            continue

        # R1 (red team), the single most important finding: workers and gates can
        # write the plan file, and seven verified paths produced a green
        # ALL DONE over work that never happened — a worker marking nodes done,
        # rewriting its own gate, stripping `risk: irreversible`, deleting a
        # node. "A worker cannot mark a node done" was enforced by a PROMPT.
        # Snapshot every node line now; reconcile after the round.
        _, _, snapNodes, snapOrder, _ = load(slug)
        snapshot = {i: render_node(snapNodes[i]) for i in snapOrder}
        snapFields = {i: {"gate": snapNodes[i]["fields"].get("gate"),
                          "risk": snapNodes[i]["fields"].get("risk"),
                          "owner": snapNodes[i]["fields"].get("owner"),
                          "mark": snapNodes[i]["mark"],
                          "title": snapNodes[i]["title"]} for i in snapOrder}
        # H4 (external certification): RC-3's exemption read snapFields[i]["owner"],
        # but snapFields never stored one — heldBy was None for every input, so the
        # exemption was dead code that has never fired and every change made by a
        # parallel session was reverted as tampering. CLAUDE.md and DEPLOY.md both
        # tell the user to run parallel sessions against one graph, so the guard was
        # silently destroying gate-proven work. A node that was leased to a
        # DIFFERENT owner when we snapshotted is that session's to settle: its line
        # and its appended evidence are none of our business. Everything else — a
        # node that was unowned, or ours — is still judged exactly as before.
        heldElsewhere = {i for i in snapOrder
                         if snapFields[i]["mark"] == ">"
                         and snapFields[i]["owner"] not in (None, "", owner)}
        snapRecords = {record_path(slug, snapNodes[i]): _record_hash(record_path(slug, snapNodes[i]))
                       for i in snapOrder}
        # RC-1 (red team 2): the snapshot was a set of rendered NODE lines, so
        # everything else in the file was invisible — including the APPROVED
        # token that gates the human-approval boundary.
        # H1 (external certification): the note region is where gate evidence
        # lives, and it was inside no integrity check. A worker appending one
        # forged `exit 0` line per round broke repeated_failures()'s run-length
        # count, so the STUCK breaker never fired and a hopeless node burned the
        # full round cap -- 20 dispatches of the user's rolling usage window
        # instead of 2 -- while `plan status` reported the fabricated passes as
        # gate evidence. A worker could also rewrite the orchestrator's own
        # historical `exit 1` lines to `exit 0`, destroying the audit trail.
        #
        # Snapshot per node rather than by hash, because the orchestrator makes
        # ONE legitimate append per claimed node per round and a hash cannot
        # tell that from a forgery.
        snapNotes = {i: list(snapNodes[i]["notes"]) for i in snapOrder}
        # Pin the SCRIPTS each claimed gate depends on, not just the gate string
        # in the plan file. A worker that rewrites ./bin/run-tests.sh gets a real
        # exit 0 from a real command, and every check that looks at the plan file
        # says the graph is clean. Only the claimed nodes are pinned: hashing
        # every gate in a large graph every round is real cost for no gain.
        _groot = os.path.dirname(os.path.dirname(os.path.abspath(plan_path(slug))))
        gatepins = {}
        for i in claimed:
            gcmd = snapNodes[i]["fields"].get("gate", "") if i in snapNodes else ""
            gctx = [c.strip() for c in
                    (snapNodes[i]["fields"].get("ctx", "") if i in snapNodes else "").split(",")
                    if c.strip()]
            gatepins[i] = {"cmd": gcmd, "root": _groot, "owned": gctx,
                           "files": gate_files(gcmd, _groot, owned=gctx)}
        snapApproval = _approval_lines(slug)
        # C3 (external certification): storing only a sha1 meant the prose could
        # be detected as changed but never PUT BACK. The tamper notice printed,
        # the run stopped -- and the forged APPROVED stayed on disk with the
        # sign-off node left [x], so the very next `plan run` adopted the
        # poisoned file as its clean baseline and dispatched the deploy. A
        # detector that announces a violation and then ships it is worse than
        # none: it leaves an audit trail saying the guard worked.
        try:
            with open(plan_path(slug), encoding="utf-8") as _pf:
                snapProseLines = _pf.readlines()
        except OSError:
            snapProseLines = None
        ourRecords = {record_path(slug, snapNodes[i]) for i in claimed if i in snapNodes}

        print(f"round {rnd}: dispatching {len(claimed)} node(s) to {a.workers} worker(s): "
              + ", ".join(str(c) for c in claimed), flush=True)
        if a.dry_run:
            for nid in claimed:
                path = plan_path(slug)
                with Lock(path):
                    _, lines, nodes3, _, _ = load(slug)
                    set_node(path, lines, nodes3[nid], mark=" ", lease=None)
            print("dry-run: claimed and released without dispatching")
            return 0

        halt = False
        # `list(ex.map(...))` parks the main thread in an UNINTERRUPTIBLE lock
        # acquire, so Python never reaches the signal handler and a SIGHUP was
        # absorbed until every worker finished -- up to the worker timeout.
        # Waiting in short slices keeps the main thread interruptible, and the
        # executor is shut down without waiting on the abnormal path.
        ex = ThreadPoolExecutor(max_workers=a.workers)
        try:
            futs = [ex.submit(_work_one, slug, i, a.timeout, a.model, owner,
                              snapFields.get(i, {}).get("gate"),
                              gatepins.get(i)) for i in claimed]
            pending = set(futs)
            while pending:
                _settled, pending = cf_wait(pending, timeout=0.5)
            results = [f.result() for f in futs]
        finally:
            _kill_workers()
            ex.shutdown(wait=False)
            # Whatever happened above, no node may be left leased to a dead run.
            # R4: this block itself called load(), which dies on a duplicate id
            # appended mid-round — so the cleanup died with the run and both
            # nodes stayed [>] for the full lease. It must never raise.
            try:
                _, lines, nodesF, _, _ = load(slug)
                for nid in claimed:
                    nF = nodesF.get(nid)
                    if nF is not None and nF["mark"] == ">" \
                            and nF["fields"].get("owner") == owner:
                        path = plan_path(slug)
                        with Lock(path):
                            _, lines2, nodes4, _, _ = load(slug)
                            set_node(path, lines2, nodes4[nid], mark=" ", lease=None)
                            append_note(path, lines2, nodes4[nid],
                                        "released — orchestrator round ended without settling it")
            except SystemExit:
                print("  cleanup could not read the plan file — nodes may still be "
                      "leased; they will free at lease expiry", file=sys.stderr)
            except Exception as e:
                print(f"  cleanup failed ({type(e).__name__}) — nodes will free at "
                      f"lease expiry", file=sys.stderr)

        # What this orchestrator itself wrote for each node it settled: exit 0
        # means [x], anything else means back to [ ]. A node that came back any
        # other way was written by something else.
        expected_mark = {}
        for r in results:
            if r.get("quota") or r.get("dispatch_failed") or r.get("gate_error"):
                continue
            if "gate_rc" in r:
                expected_mark[r["id"]] = "x" if r["gate_rc"] == 0 else " "

        # Reconcile. Two classes, because a node this orchestrator settled has
        # legitimately changed:
        #   - a node we did NOT claim: the whole line must be identical.
        #   - a node we DID claim: we only ever write mark, lease, owner and a
        #     note. The GATE and the RISK are ours to read, never to write, so
        #     a change there is tampering even on our own node. Skipping claimed
        #     nodes wholesale — the first version of this fix — left the
        #     verified "a worker rewrites its own gate" path invisible.
        # Detection alone is not enough: the graph is left wrong until a human
        # reads it. Tampered lines are RESTORED from the snapshot.
        try:
            path = plan_path(slug)
            with Lock(path):
                _, aLines, afterNodes, afterOrder, _ = load(slug)
                tampered, vanished, repaired = [], [], 0
                for i in afterOrder:
                    if i not in snapshot:
                        continue
                    nA = afterNodes[i]
                    if i in claimed:
                        was = snapFields.get(i, {})
                        # RC-2 (red team 2): gate/risk/title were compared but the
                        # MARK was not — the field the orchestrator's whole
                        # authority is defined by. A worker forging [x] produced a
                        # green ALL DONE over a gate that printed FAIL. The
                        # orchestrator knows what it wrote: exit 0 -> x, else " ".
                        expect = expected_mark.get(i)
                        if (nA["fields"].get("gate") != was.get("gate")
                                or nA["fields"].get("risk") != was.get("risk")
                                or nA["title"] != was.get("title")
                                or (expect is not None and nA["mark"] != expect)):
                            tampered.append(i)
                    elif render_node(nA) != snapshot[i] and i not in heldElsewhere:
                        # RC-3 (red team 2): a concurrent orchestrator settling a
                        # node it legitimately holds is not tampering. Only judge
                        # nodes that were unowned, or ours, at snapshot time.
                        tampered.append(i)
                for i in tampered:
                    # RC-3 (red team 2): the repair forced every node to [ ] and
                    # dropped owner, so a node that was PROVEN DONE at snapshot
                    # time came back pending — while the message printed the [x]
                    # it had not written. Restore the snapshot's own mark; only
                    # drop the lease when it is this dead round's.
                    m = NODE_RE.match(snapshot[i])
                    t0, f0, _ = parse_fields(m.group("rest"))
                    mark0 = m.group("mark")
                    if i in claimed:
                        f0.pop("lease", None)
                        f0.pop("owner", None)
                        if mark0 == ">":
                            mark0 = " "
                    aLines[afterNodes[i]["line"]] = render_node(
                        {"id": i, "mark": mark0, "title": t0, "fields": f0})
                    repaired += 1
                # Notes are append-only, and only WE may append. History that
                # changed is a rewritten audit trail; more than one new line on
                # a node we claimed is forged evidence; any new line on a node
                # we did not claim is forgery outright.
                for i in afterOrder:
                    if i not in snapNotes:
                        continue
                    was, now_ = snapNotes[i], afterNodes[i]["notes"]
                    # H4: the session that holds the node appends its own gate
                    # evidence — however many lines its attempt produced. History
                    # is still append-only for everyone, but the count is only
                    # ours to bound on nodes we are responsible for.
                    if i in heldElsewhere:
                        if now_[:len(was)] == was:
                            continue
                        allowed = 0   # history was rewritten: roll back to the snapshot
                    else:
                        allowed = 1 if i in claimed else 0
                    if now_[:len(was)] != was or len(now_) > len(was) + allowed:
                        if i not in tampered:
                            tampered.append(i)
                        restore_notes(path, afterNodes[i], now_[:len(was) + allowed])
                        _, aLines, afterNodes, afterOrder, _ = load(slug)
                        repaired += 1

                vanished = [i for i in snapshot if i not in afterNodes]
                prose_changed = (_approval_lines(slug) != snapApproval)
                for i in vanished:
                    # Re-appending the raw snapshot line restored a [>] with this
                    # round's lease, stranding the node under a dead PID.
                    mv = NODE_RE.match(snapshot[i])
                    tv, fv, _ = parse_fields(mv.group("rest"))
                    markv = mv.group("mark")
                    if markv == ">":
                        markv = " "
                        fv.pop("lease", None); fv.pop("owner", None)
                    aLines.append(render_node({"id": i, "mark": markv, "title": tv, "fields": fv}))
                    aLines.append(f"      {now_iso()} restored — this node was deleted "
                                  f"from the plan file during a run")
                if tampered or vanished:
                    save(path, aLines)
        except SystemExit:
            print("REFUSING TO CONTINUE — the plan file became unreadable during the round")
            return 2

        if prose_changed:
            print("REFUSING TO CONTINUE — the plan file's non-node text changed where "
                  "the APPROVED token lives. A worker must never be able to grant its "
                  "own approval.")
            # Detection is not enough. Restore the prose verbatim, and revert
            # every node this round settled: their gates ran against a file the
            # worker had edited, so no result from this round can be trusted --
            # including a PASS that looks unrelated.
            if snapProseLines is not None:
                try:
                    path = plan_path(slug)
                    with Lock(path):
                        _, cLines, cNodes, cOrder, _ = load(slug)
                        keepNodes = {i: render_node(cNodes[i]) for i in cOrder}
                        out, seen = [], set()
                        for ln in snapProseLines:
                            m2 = NODE_RE.match(ln.rstrip("\n"))
                            if m2:
                                nid2 = int(m2.group("id"))
                                seen.add(nid2)
                                if nid2 in keepNodes:
                                    out.append(keepNodes[nid2])
                                    continue
                            out.append(ln.rstrip("\n"))
                        for i in cOrder:
                            if i not in seen:
                                out.append(keepNodes[i])
                        # every node we settled this round goes back to pending
                        reverted = []
                        for i in claimed:
                            if i in cNodes and cNodes[i]["mark"] != " ":
                                reverted.append(i)
                        save(path, out)
                        _, rLines, rNodes, _, _ = load(slug)
                        for i in reverted:
                            if i in rNodes:
                                set_node(path, rLines, rNodes[i], mark=" ",
                                         lease=None, owner=None)
                                _, rLines, rNodes, _, _ = load(slug)
                        print(f"  restored the non-node text from the pre-dispatch "
                              f"snapshot; reverted {len(reverted)} node(s) settled "
                              f"this round — their gates ran against an edited file")
                except SystemExit:
                    print("  could not restore: the plan file is unreadable")
            return 2
        if tampered or vanished:
            print("REFUSING TO CONTINUE — the plan file was modified during the round "
                  "by something other than this orchestrator:")
            for i in tampered:
                print(f"  TAMPERED {i}. restored to: {snapshot[i]}")
            for i in vanished:
                print(f"  VANISHED {i}. restored: {snapshot[i]}")
            print(f"  {repaired + len(vanished)} node(s) repaired from the pre-dispatch "
                  "snapshot. A worker or a gate is writing graph state — review the diff "
                  "before trusting any result from this run.")
            return 2

        # Records are evidence rather than state, but plan context feeds them
        # into the next attempt, so a silent edit is worth naming.
        for rp, was in snapRecords.items():
            now = _record_hash(rp)
            if now != was and rp not in ourRecords:
                print(f"  note: record {os.path.basename(rp)} changed outside this "
                      f"orchestrator's own writes")

        dispatched += len(results)
        for r in results:
            nid = r["id"]
            spent += r.get("cost", 0) or 0
            if r.get("auth"):
                ns = type("A", (), {"slug": slug, "id": nid,
                                    "reason": [f"dispatch halted: worker could not "
                                               f"authenticate — {r['why']}"],
                                    "owner": None, "force": True})()
                cmd_release(ns)
                print(f"  node {nid}: NOT AUTHENTICATED — released, halting run "
                      f"({r['why']}). Headless `claude -p` has no usable session; "
                      f"run `claude` interactively and sign in, then `plan resume`.")
                halt = True
                continue
            if r["quota"]:
                why = ("Anthropic's servers are overloaded (529) — transient, and "
                       "nothing to do with your account; re-run or `plan resume` "
                       "later" if r.get("overloaded")
                       else "hit the rolling Claude usage limit (429)")
                ns = type("A", (), {"slug": slug, "id": nid,
                                    "reason": [f"dispatch halted: {why}"],
                                    "owner": None, "force": True})()
                cmd_release(ns)
                label = "OVERLOADED" if r.get("overloaded") else "QUOTA"
                print(f"  node {nid}: {label} — released, halting run ({why})")
                halt = True
                continue
            if r.get("gate_tampered"):
                # The gate was never run: a rewritten gate's exit 0 is not
                # evidence about anything, and running it first would produce a
                # result indistinguishable from an honest pass.
                crashed += 1
                path = plan_path(slug)
                with Lock(path):
                    _, lg, ng, _, _ = load(slug)
                    append_note(path, lg, ng[nid],
                                "gate NOT run — its script changed during the round")
                print(f"  node {nid}: GATE CHANGED during the round — not run, "
                      f"node left pending. A worker must never be able to edit "
                      f"the thing that judges it.")
                for f in r["gate_tampered"][:4]:
                    print(f"    {f}")
                halt = True
                continue
            if r.get("gate_error"):
                crashed += 1
                print(f"  node {nid}: GATE CRASHED — {r['gate_error']} (node left pending)")
                continue
            if r.get("dispatch_failed"):  # includes worker timeout
                # The worker never ran. Record that, and do NOT let it become
                # gate evidence — two of these would otherwise burn the node to
                # STUCK on a transient API outage.
                crashed += 1
                path = plan_path(slug)
                with Lock(path):
                    _, lines5, nodes5, _, _ = load(slug)
                    append_note(path, lines5, nodes5[nid], f"dispatch failed — {r['why']}")
                print(f"  node {nid}: DISPATCH FAILED — {r['why']} (not gated)")
                continue
            if r.get("denials"):
                path = plan_path(slug)
                with Lock(path):
                    _, lines6, nodes6, _, _ = load(slug)
                    append_note(path, lines6, nodes6[nid],
                                f"worker hit {r['denials']} permission denial(s) — "
                                f"the safety guard blocked it, this is not a task failure")
                print(f"  node {nid}: worker hit {r['denials']} permission denial(s)")
            if r.get("gate_rc") == 0:
                passed += 1
            else:
                failed += 1
        if halt:
            capped = False
            break
    if capped:
        print(f"stopped at the {a.max_rounds}-round cap")

    _, _, nodes, order, mal = load(slug)
    # M1: the announcement only ran at the TOP of a round, so a node injected
    # during the LAST round — the round cap, a halt, an empty pool — was never
    # named at all. Whatever ends the run, say what appeared during it.
    if known is not None:
        _announce_new(nodes, order, known, announced)

    print(f"\ndispatched {dispatched}, gates passed {passed}, gates failed {failed}, "
          f"dispatch failures {crashed}, worker usage ~${spent:.2f} equivalent")
    st = analyse(nodes, order, mal)
    print(progress_line(st, order, nodes))
    # R10 (red team): completion was inferred from an empty dispatch pool, so a
    # graph with blocked, held or broken nodes could still read as success.
    outstanding = (len(st["ready"]) + len(st["blocked"]) + len(st["broken"])
                   + len(st["held"]) + len(st["running"]) + len(mal))
    return 0 if order and outstanding == 0 else 1


_LIVE = set()
_LIVE_LOCK = None


def _kill_workers():
    """Signals reach the main thread, but it is parked in the executor's join,
    which waits for every worker — so a closed laptop did not release its nodes
    for up to the worker timeout. Ending the children lets the pool drain and
    the release in `finally` actually run."""
    for proc in list(_LIVE):
        try:
            proc.kill()
        except Exception:
            pass


def _install_signal_handlers():
    """A closed terminal during an overnight drain sent SIGHUP, which Python
    does not turn into an exception — so cmd_run's `finally` never ran and every
    in-flight node stayed [>] under a dead owner for the full lease. That is the
    previous ledger's dead-lease defect, reachable today. Raising SystemExit
    lets the existing cleanup do its job."""
    import signal

    def _bail(signum, _frame):
        _kill_workers()
        raise SystemExit(128 + signum)

    for sig in ("SIGHUP", "SIGTERM"):
        h = getattr(signal, sig, None)
        if h is not None:
            try:
                signal.signal(h, _bail)
            except (ValueError, OSError):
                pass


def main():
    _install_signal_handlers()
    p = argparse.ArgumentParser(prog="plan", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("new"); q.add_argument("slug"); q.add_argument("goal", nargs="*"); q.set_defaults(fn=cmd_new)
    # `dir` is optional so that bare `plan init` means "here", the way `git
    # init` does. It was required, so the zero-argument bootstrap printed a
    # usage error; `plan new` then walked UP out of the uninitialised directory
    # and scaffolded into whatever plan/ it found in a parent.
    q = sub.add_parser("init"); q.add_argument("dir", nargs="?", default=".")
    q.add_argument("goal", nargs="*")
    q.add_argument("--slug", default=None)
    q.add_argument("--python", action="store_true"); q.set_defaults(fn=cmd_init)
    q = sub.add_parser("ready"); q.add_argument("slug"); q.set_defaults(fn=cmd_ready)
    q = sub.add_parser("claim"); q.add_argument("slug")
    q.add_argument("--owner", default=os.environ.get("PLAN_OWNER") or sessionid()); q.set_defaults(fn=cmd_claim)
    q = sub.add_parser("release", help="yield a node: plan release <slug> <id> <why...> [--owner O] [--force]")
    q.add_argument("slug"); q.add_argument("id", type=int)
    # The reason must come BEFORE any flag — argparse cannot place a variadic
    # positional after optionals, and it fails with an opaque error if you try.
    q.add_argument("reason", nargs="*")
    q.add_argument("--owner", default=os.environ.get("PLAN_OWNER") or sessionid())
    q.add_argument("--force", action="store_true"); q.set_defaults(fn=cmd_release)
    q = sub.add_parser("context"); q.add_argument("slug"); q.add_argument("id", type=int); q.set_defaults(fn=cmd_context)
    q = sub.add_parser("gate"); q.add_argument("slug"); q.add_argument("id", type=int)
    q.add_argument("--attended", action="store_true")
    q.add_argument("--owner", default=os.environ.get("PLAN_OWNER") or sessionid())
    q.add_argument("--force", action="store_true"); q.set_defaults(fn=cmd_gate)
    q = sub.add_parser("mark"); q.add_argument("slug"); q.add_argument("id", type=int)
    q.add_argument("state", choices=["done", "pending"]); q.add_argument("--note")
    q.add_argument("--owner", default=os.environ.get("PLAN_OWNER") or sessionid())
    q.add_argument("--force", action="store_true"); q.set_defaults(fn=cmd_mark)
    q = sub.add_parser("note"); q.add_argument("slug"); q.add_argument("id", type=int)
    q.add_argument("text", nargs="+")
    q.add_argument("--force", action="store_true"); q.set_defaults(fn=cmd_note)
    q = sub.add_parser("record"); q.add_argument("slug"); q.add_argument("id", type=int)
    q.add_argument("--summary", nargs="*"); q.set_defaults(fn=cmd_record)
    q = sub.add_parser("show"); q.add_argument("slug"); q.set_defaults(fn=cmd_show)
    q = sub.add_parser("resume", help="where was I: every plan with outstanding work, and why it stopped")
    q.add_argument("--run", action="store_true", help="continue every resumable plan")
    q.add_argument("--accept-risks", action="store_true")
    q.add_argument("--workers", type=int, default=int(os.environ.get("PLAN_WORKERS", "3")))
    q.add_argument("--max-rounds", type=int, default=20)
    q.add_argument("--timeout", type=int, default=1800)
    q.add_argument("--model", default=None)
    q.set_defaults(fn=cmd_resume)
    q = sub.add_parser("status", help="compact dashboard: progress, what is moving, what failed last")
    q.add_argument("slug"); q.set_defaults(fn=cmd_status)
    q = sub.add_parser("stats"); q.add_argument("slug"); q.set_defaults(fn=cmd_stats)
    q = sub.add_parser("lint"); q.add_argument("slug"); q.set_defaults(fn=cmd_lint)
    q = sub.add_parser("list"); q.set_defaults(fn=cmd_list)
    q = sub.add_parser("doctor", help="is this machine fit for an unattended drain?")
    q.set_defaults(fn=cmd_doctor)
    q = sub.add_parser("run"); q.add_argument("slug")
    q.add_argument("--workers", type=int, default=int(os.environ.get("PLAN_WORKERS", "3")))
    q.add_argument("--max-rounds", type=int, default=20)
    q.add_argument("--timeout", type=int, default=1800)
    q.add_argument("--model", default=None)
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--accept-risks", action="store_true",
                   help=argparse.SUPPRESS)  # accepted for compatibility; no longer required
    q.set_defaults(fn=cmd_run)
    a = p.parse_args()
    if getattr(a, "slug", None) and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", a.slug):
        die(f"invalid slug {a.slug!r} — letters, digits, dot, dash, underscore only")
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
