#!/usr/bin/env bash
# Regression suite for the rebuilt state engine.
#
# Organised by the defect each test reproduces. D1-D4 are the four failures that
# killed the previous ledger (evidence: DESIGN.md §1). G1-G10 are the gaps found
# after v1 shipped — G10 was found by this file's own plan file, when a node
# written "6b." silently vanished from the graph.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "$2" "$3"; fi; }
# -F throughout: every pattern here is a literal. Without it "[x] 4." reads as a
# character class, the assertion can never match, and a `hasnt` passes vacuously.
has()  { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "contains '$3'" "$2"; fi; }
hasnt(){ if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "NOT contains '$3'" "$2"; else ok "$1"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan"; cd "$T" || exit 1
fresh() { rm -rf "$T/plan"; mkdir -p "$T/plan"; cat > "$T/plan/t.md"; }

echo "== D1: failure is never terminal =="
fresh <<'EOF'
## goal: test
- [ ] 1. always fails | gate: exit 1
- [ ] 2. downstream | needs: 1 | gate: true
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
check "failed node returns to pending" "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
has "evidence recorded on the line" "$(cat plan/t.md)" "exit 1"
has "and it is ready again (backward edge)" "$("$PLAN" ready t 2>&1)" "READY 1"

echo "== D2: nothing may disappear from every report =="
fresh <<'EOF'
## goal: test
- [>] 1. root | owner: live | lease: 2999-01-01T00:00:00Z | gate: true
- [ ] 2. mid | needs: 1 | gate: true
- [ ] 3. two hops back | needs: 2 | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
has "1 hop blocked reported"   "$out" "BLOCKED 2"
has "2 hops blocked reported"  "$out" "BLOCKED 3"
has "names what it waits on"   "$out" "waiting on 1 (root)"

echo "== D3: an honest stop needs no lie =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
"$PLAN" note t 1 "blocked on hardware, reinstatable" >/dev/null
has "a hold note coexists with pending" "$(cat plan/t.md)" "reinstatable"
check "still pending" "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== D4: a dead session must not deadlock the graph =="
fresh <<'EOF'
## goal: test
- [>] 1. stale | owner: ghost | lease: 2020-01-01T00:00:00Z | gate: true
EOF
has "expired lease reads as pending" "$("$PLAN" ready t 2>&1)" "READY 1"
fresh <<'EOF'
## goal: test
- [>] 1. fresh | owner: live | lease: 2999-01-01T00:00:00Z | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
hasnt "live lease is not stolen" "$out" "READY 1"
has   "reported as running"      "$out" "RUNNING 1"

echo "== G10: a line that looks like a node must parse or be reported =="
fresh <<'EOF'
## goal: test
- [ ] 1. fine | gate: true
- [ ] 6b. malformed id | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
has "malformed node line is loud" "$out" "does not parse"
has "and names the line number"   "$out" "line 3"
fresh <<'EOF'
## goal: test
- [ ] 1. depends on a non-number | needs: 6b | gate: true
EOF
has "non-numeric dep is BROKEN, not dropped" "$("$PLAN" ready t 2>&1)" "BROKEN 1"
fresh <<'EOF'
## goal: test
- [ ] 1. no gate at all
EOF
has "a node with no gate is BROKEN" "$("$PLAN" ready t 2>&1)" "no gate"

echo "== G4: an irreversible node is never dispatched unattended =="
fresh <<'EOF'
## goal: test
- [ ] 1. deploy to prod | risk: irreversible | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
hasnt "not offered as ready"        "$out" "READY 1"
has   "held for a human"            "$out" "HELD 1"
"$PLAN" claim t >/dev/null 2>&1
check "claim refuses it"            "1" "$?"
"$PLAN" gate t 1 >/dev/null 2>&1
check "gate refuses without --attended" "3" "$?"
check "and it is still pending"     "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
"$PLAN" gate t 1 --attended >/dev/null 2>&1
check "runs with --attended"        "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== G7: retrying sessions spread instead of colliding =="
fresh <<'EOF'
## goal: test
- [ ] 1. already attempted twice | gate: true
      2026-08-18T00:00:00Z exit 1 in 2s — nope
      2026-08-18T00:01:00Z exit 1 in 2s — nope again
- [ ] 2. untouched | gate: true
EOF
check "claim takes the least-attempted node, not the lowest id" "2" "$("$PLAN" claim t 2>/dev/null)"

echo "== G1: a session sees its context budget before starting =="
printf 'x%.0s' $(seq 1 500) > big.txt
fresh <<'EOF'
## goal: test
- [ ] 1. sized node | ctx: big.txt | gate: true
EOF
out=$("$PLAN" context t 1)
has "file size is shown"     "$out" "big.txt (500b)"
has "budget is stated"       "$out" "CONTEXT BUDGET: 500b"
PLAN_CTX_BUDGET=100 "$PLAN" context t 1 > over.txt
has "over budget says split it" "$(cat over.txt)" "OVER BUDGET"

echo "== G2: a node can be yielded with a reason =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
"$PLAN" claim t --owner sessX >/dev/null
"$PLAN" release t 1 quota exhausted mid-node >/dev/null 2>&1
check "a stranger cannot release a live lease" "4" "$?"
"$PLAN" release t 1 quota exhausted mid-node --owner sessX >/dev/null
check "released node is pending again" "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
has "the reason is recorded"           "$(cat plan/t.md)" "quota exhausted mid-node"
has "and it is claimable again"        "$("$PLAN" ready t 2>&1)" "READY 1"

echo "== G3: the host is recorded, not just the user =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
"$PLAN" claim t >/dev/null
has "owner carries user@host" "$(cat plan/t.md)" "owner: $(whoami)@$(hostname -s)"

echo "== G9: per-node records hold the full history =="
# the output must not share any text with the gate COMMAND, or "the graph line
# stays short" can pass or fail for the wrong reason — the command is on that line.
printf 'FIRSTLINE\nSECONDLINE\nNEEDLE-IN-FULL-OUTPUT\n' > noise.txt
fresh <<'EOF'
## goal: test
- [ ] 1. noisy gate | gate: sh -c 'cat noise.txt; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
rec=$(ls plan/t/ 2>/dev/null | head -1)
[ -n "$rec" ] && ok "record file created" || bad "record file created" "a file in plan/t/" "nothing"
body=$(cat "plan/t/$rec" 2>/dev/null)
has "record holds the FULL gate output"  "$body" "NEEDLE-IN-FULL-OUTPUT"
has "record holds the gate command"      "$body" "noisy gate"
hasnt "the graph line stays short"       "$(grep '^- \[' plan/t.md)" "FIRSTLINE"
has "context feeds the record forward"   "$("$PLAN" context t 1)" "NEEDLE-IN-FULL-OUTPUT"
"$PLAN" record t 1 --summary tried the wrong layer >/dev/null
has "a written summary is retained"      "$("$PLAN" context t 1)" "tried the wrong layer"

echo "== G5/G6: the harness measures itself =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: sleep 1
- [ ] 2. b | gate: true
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
"$PLAN" gate t 2 >/dev/null 2>&1
out=$("$PLAN" stats t 2>&1)
has "duration recorded on the line" "$(cat plan/t.md)" "in 1s"
has "stats reports gate runs"       "$out" "gate runs        2"
has "stats reports durations"       "$out" "p50"
has "stats suggests a lease from data, not a guess" "$out" "lease suggestion"

echo "== repeated identical failure is named =="
fresh <<'EOF'
## goal: test
- [ ] 1. same every time | gate: sh -c 'echo boom; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
hasnt "one failure is not stuck"  "$("$PLAN" ready t 2>&1)" "STUCK"
"$PLAN" gate t 1 >/dev/null 2>&1
has "twice the same way is STUCK" "$("$PLAN" ready t 2>&1)" "STUCK: same failure 2x"
# A genuinely different failure means different TEXT. A bare varying number is
# not progress — `echo $$` used to sit here and it collapses under normalisation,
# correctly, because a changing PID says nothing about the work.
rm -f .counter
fresh <<'EOF'
## goal: test
- [ ] 1. different each time | gate: sh -c 'c=$(cat .counter 2>/dev/null || echo 0); c=$((c+1)); echo $c > .counter; if [ $c -eq 1 ]; then echo "AssertionError in the parser"; else echo "TypeError in the loader"; fi; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1; "$PLAN" gate t 1 >/dev/null 2>&1
hasnt "a CHANGED failure is progress" "$("$PLAN" ready t 2>&1)" "STUCK"

# C1 REGRESSION. Every real test runner prints a duration or a count, so a raw
# tail comparison never matched twice and STUCK never fired — an unfixable
# pytest node was dispatched every round forever. This test fails without
# digit normalisation in the failure signature.
rm -f .counter
fresh <<'EOF'
## goal: test
- [ ] 1. pytest-shaped failure | gate: sh -c 'c=$(cat .counter 2>/dev/null || echo 0); c=$((c+1)); echo $c > .counter; echo "FAILED tests/test_x.py::test_seam - AssertionError"; echo "1 failed, 12 passed in 4.$c s"; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1; "$PLAN" gate t 1 >/dev/null 2>&1
has "same failure with a VARYING DURATION is still STUCK" "$("$PLAN" ready t 2>&1)" "STUCK: same failure 2x"

echo "== parallel sessions =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
- [ ] 2. b | gate: true
- [ ] 3. c | gate: true
- [ ] 4. d | gate: true
- [ ] 5. e | gate: true
EOF
rm -f "$T/claims"
for i in 1 2 3 4 5; do ( "$PLAN" claim t --owner "s$i" >> "$T/claims" 2>/dev/null ) & done
wait
check "5 concurrent claims all succeeded" "5" "$(sort "$T/claims" | wc -l | tr -d ' ')"
check "no two took the same node"         "5" "$(sort -u "$T/claims" | wc -l | tr -d ' ')"

echo "== parsing and structure =="
fresh <<'EOF'
## goal: test
- [ ] 1. piped gate | gate: echo hi | grep hi
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
check "gate with a pipe runs and passes" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
has "gate text round-trips"              "$(cat plan/t.md)" "gate: echo hi | grep hi"
fresh <<'EOF'
## goal: test
- [ ] 1. a | needs: 2 | gate: true
- [ ] 2. b | needs: 1 | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
has "cycle detected (1)" "$out" "BROKEN 1"
has "cycle detected (2)" "$out" "BROKEN 2"

echo "== lint catches structure before a run =="
fresh <<'EOF'
## goal: test
- [ ] 1. fine | gate: true
EOF
"$PLAN" lint t >/dev/null 2>&1
check "clean plan lints OK" "0" "$?"
fresh <<'EOF'
## goal: test
- [ ] 1. broken | needs: 99 | gate: true
EOF
"$PLAN" lint t >/dev/null 2>&1
check "broken plan fails lint" "1" "$?"

echo "== plan new: the front half is user-gated by construction =="
rm -rf "$T/plan"; mkdir -p "$T/plan"
"$PLAN" new scaf "make the thing work" >/dev/null
has "goal recorded"                  "$(cat plan/scaf.md)" "make the thing work"
check "exactly 4 front nodes"        "4" "$(grep -c '^- \[' plan/scaf.md)"
out=$("$PLAN" ready scaf 2>&1)
has   "analysis is the entry point"  "$out" "READY 1"
hasnt "research cannot start first"  "$out" "READY 2"
"$PLAN" new scaf again >/dev/null 2>&1
check "refuses to overwrite"         "1" "$?"
# This used to append a bare heading plus "x" and expect each gate to pass,
# because the scaffold's gates WERE `grep -q '^## analysis'`. The test encoded
# the vacuity rather than catching it -- the same shape as the identity tests
# that encoded H6. The front half now gates on substance, so the test does the
# work the gate asks for.
printf '## analysis\nAsked: make the thing work. Out of scope: the CLI.\ndone when: `test -f out.txt` exits 0.\n' >> plan/scaf.md
"$PLAN" gate scaf 1 >/dev/null 2>&1; check "a real analysis passes node 1" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/scaf.md)"
printf '## research\nexisting: checked ListSkills and `bin/` — none applicable.\nSource: `plan.py` and https://example.com/ref\n' >> plan/scaf.md
"$PLAN" gate scaf 2 >/dev/null 2>&1; check "a real research section passes node 2" "- [x] 2." "$(grep -o '^- \[.\] 2\.' plan/scaf.md)"
printf '## plan\nOne node, gated on its own test which is written first.\n' >> plan/scaf.md
printf -- '- [ ] 5. build it | needs: 4 | gate: test -f out.txt\n' >> plan/scaf.md
"$PLAN" gate scaf 3 >/dev/null 2>&1
has "finalise unlocks after all three" "$("$PLAN" ready scaf 2>&1)" "READY 4"
# and a heading alone must NOT unlock it -- the property the old test lost
rm -rf "$T/plan"; mkdir -p "$T/plan"; "$PLAN" new hollow "g" >/dev/null 2>&1
printf '## analysis\nx\n' >> plan/hollow.md
"$PLAN" gate hollow 1 >/dev/null 2>&1
check "a heading alone does NOT pass node 1" "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/hollow.md)"
rm -rf "$T/plan"; mkdir -p "$T/plan"; "$PLAN" new scaf "make the thing work" >/dev/null 2>&1
printf '## analysis\nAsked: x. done when: `true` exits 0.\n## research\nexisting: none — checked `bin/`.\nSource: `plan.py`, https://e.com/r\n## plan\nOne gated node.\n' >> plan/scaf.md
printf -- '- [ ] 5. build it | needs: 4 | gate: true\n' >> plan/scaf.md
for i in 1 2 3; do "$PLAN" gate scaf $i >/dev/null 2>&1; done
"$PLAN" gate scaf 4 >/dev/null 2>&1
hasnt "shut until the human word"      "$("$PLAN" show scaf 2>&1)" "[x] 4."
printf 'APPROVED\n' >> plan/scaf.md; "$PLAN" gate scaf 4 >/dev/null 2>&1
has "APPROVED closes the front half"   "$("$PLAN" show scaf 2>&1)" "[x] 4."

# --- fixes from the 2026-08-18 adversarial review ---------------------------

echo "== H2: a lease cannot be settled by someone who does not hold it =="
fresh <<'EOF'
## goal: test
- [ ] 1. long job | gate: true
EOF
"$PLAN" claim t --owner sessionA >/dev/null
"$PLAN" gate t 1 --owner sessionB >/dev/null 2>&1
check "a stranger cannot gate it"     "4" "$?"
check "and it stays leased"           "- [>] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
# shellcheck disable=SC1010  # "done" is a literal argument, not a loop keyword
"$PLAN" mark t 1 done --owner sessionB >/dev/null 2>&1
check "nor mark it"                   "4" "$?"
"$PLAN" gate t 1 --owner sessionA >/dev/null 2>&1
check "the holder can settle it"      "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
fresh <<'EOF'
## goal: test
- [>] 1. held elsewhere | owner: ghost | lease: 2999-01-01T00:00:00Z | gate: true
EOF
"$PLAN" gate t 1 --owner me --force >/dev/null 2>&1
check "--force is the documented override" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== H3: write-back must not silently destroy what the user wrote =="
fresh <<'EOF'
## goal: test
- [ ] 1. a node | owner: alice | why: blocked on the vendor SDK | est: 3d | gate: true
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
body=$(cat plan/t.md)
has "an unknown field survives (why)" "$body" "why: blocked on the vendor SDK"
has "an unknown field survives (est)" "$body" "est: 3d"
has "gate is still written last"      "$body" "| gate: true"
fresh <<'EOF'
## goal: test
- [ ] 1. support a|b syntax | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
has "a pipe in a title is reported, not silently truncated" "$out" "BROKEN 1"

echo "== M1: retitling a node must not orphan its record =="
fresh <<'EOF'
## goal: test
- [ ] 1. fix the parser | gate: true
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
"$PLAN" record t 1 --summary tried the regex approach first >/dev/null
python3 - <<'PYX'
p = "plan/t.md"
s = open(p).read().replace("fix the parser", "fix the expression parser")
open(p, "w").write(s)
PYX
has "the record follows the node after a retitle" "$("$PLAN" context t 1)" "tried the regex approach first"
check "and no second record file appeared" "1" "$(ls plan/t/ | wc -l | tr -d ' ')"

echo "== M5: the gate runs where the plan file lives, not in the caller's cwd =="
fresh <<'EOF'
## goal: test
- [ ] 1. relative gate | gate: grep -q '^## goal' plan/t.md
EOF
mkdir -p src/deep
( cd src/deep && "$PLAN" gate t 1 >/dev/null 2>&1 )
check "passes from a subdirectory" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
rm -rf src

echo "== plan init: one command from nothing to a gated plan =="
I="$T/newproj"
"$PLAN" init "$I" build a csv summariser --python >/dev/null
[ -d "$I/.git" ]      && ok "its own git repo"        || bad "its own git repo" "" ".git missing"
[ -d "$I/plan" ]      && ok "plan directory"          || bad "plan directory" "" "missing"
[ -f "$I/CLAUDE.md" ] && ok "project CLAUDE.md"       || bad "project CLAUDE.md" "" "missing"
[ -f "$I/.gitignore" ] && ok "gitignore"              || bad "gitignore" "" "missing"
[ -f "$I/plan/newproj.md" ] && ok "front half scaffolded" || bad "front half scaffolded" "" "missing"
has "the goal is recorded" "$(cat "$I/plan/newproj.md")" "build a csv summariser"
has "CLAUDE.md states the invariant" "$(cat "$I/CLAUDE.md")" "no failed state"
# REGRESSION: ~/bin/plan is a symlink, so abspath(__file__) gave the LINK's
# directory and the tiers template resolved to a path that does not exist —
# pytest.ini was silently skipped. realpath fixes it.
[ -f "$I/pytest.ini" ] && ok "--python installs the test tiers" || bad "--python installs the test tiers" "pytest.ini" "missing"
check "all four tiers declared" "4" "$(grep -cE '^    (case|integration|property|eval):' "$I/pytest.ini" 2>/dev/null || echo 0)"
out=$(cd "$I" && "$PLAN" ready newproj 2>&1)
has   "analysis is the entry point"     "$out" "READY 1"
hasnt "no implementation node yet"      "$out" "READY 5"
printf 'SENTINEL\n' >> "$I/CLAUDE.md"
"$PLAN" init "$I" different goal --python >/dev/null 2>&1
check "refuses to clobber an existing plan" "1" "$?"
has "and left the existing CLAUDE.md alone" "$(cat "$I/CLAUDE.md")" "SENTINEL"


echo "== records are budgeted: the durable file cannot become the context problem =="
# Measured before the budget: 142KB after TEN attempts, on git-tracked files.
# That is the previous ledger's exact defect — unbounded notes made its own
# `show` 12KB, so reading the file meant to survive compaction consumed the
# context compaction was destroying. Budgets borrowed from Hermes' memory design.
fresh <<'EOF'
## goal: test
- [ ] 1. verbose failure | gate: sh -c 'for i in $(seq 1 200); do echo "FAILED tests/test_$i.py::test_case - AssertionError expected 4 got 5"; done; exit 1'
EOF
for i in $(seq 1 16); do "$PLAN" gate t 1 >/dev/null 2>&1; done
sz=$(wc -c < plan/t/01-verbose-failure.md | tr -d ' ')
if [ "$sz" -lt 100000 ]; then ok "record stays bounded after 16 attempts (${sz}b)"
else bad "record stays bounded" "< 100000b" "${sz}b"; fi
body=$(cat plan/t/01-verbose-failure.md)
has "long output is elided, and says so"    "$body" "bytes elided"
has "old attempts are pruned, and say so"   "$body" "earlier attempt(s) pruned"
has "the newest failure is still intact"    "$body" "AssertionError expected 4 got 5"
ctx=$("$PLAN" context t 1 | wc -c | tr -d ' ')
if [ "$ctx" -lt 8000 ]; then ok "the carried briefing stays small (${ctx}b)"
else bad "carried briefing stays small" "< 8000b" "${ctx}b"; fi


echo "== red team: the plan file is not a trusted resource =="
# The single most important finding. Seven verified paths produced a green
# ALL DONE over work that never happened, because a worker can write the plan
# file directly. "A worker cannot mark a node done" was enforced by a PROMPT.
mkdir -p "$T/stub"
cat > "$T/stub/claude" <<'STUB'
#!/usr/bin/env bash
if sed --version >/dev/null 2>&1; then sed -i 's/^- \[ \] 2\./- [x] 2./' plan/t.md; else sed -i '' 's/^- \[ \] 2\./- [x] 2./' plan/t.md; fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":50,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. worker also edits node 2 | gate: true
- [ ] 2. must never be marked by a worker | gate: false
EOF
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 2>&1)
has   "tampering is detected"        "$out" "TAMPERED 2"
has   "and the run refuses"          "$out" "REFUSING TO CONTINUE"
hasnt "no false completion"          "$out" "ALL DONE"
# detection alone leaves the graph wrong until a human reads it
check "the tampered node is REPAIRED" "- [ ] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"
has   "and the repair is stated"      "$out" "repaired from the pre-dispatch snapshot"

# a worker rewriting ITS OWN node's gate was invisible to the first version of
# this fix, which skipped claimed nodes wholesale
cat > "$T/stub/claude" <<'STUB'
#!/usr/bin/env bash
if sed --version >/dev/null 2>&1; then sed -i 's|gate: test -f never.txt|gate: true|' plan/t.md; else sed -i '' 's|gate: test -f never.txt|gate: true|' plan/t.md; fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":50,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. worker rewrites its own gate | gate: test -f never.txt
EOF
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 2>&1)
has   "a worker cannot rewrite its own gate" "$out" "TAMPERED 1"
has   "the original gate is restored"        "$(cat plan/t.md)" "gate: test -f never.txt"
check "and the node is not done"             "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

# a worker deleting a node must not shrink the graph
cat > "$T/stub/claude" <<'STUB'
#!/usr/bin/env bash
if sed --version >/dev/null 2>&1; then sed -i '/^- \[ \] 2\./d' plan/t.md; else sed -i '' '/^- \[ \] 2\./d' plan/t.md; fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":50,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. deletes node 2 | gate: true
- [ ] 2. must survive | gate: false
EOF
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 2>&1)
has "a deleted node is noticed"  "$out" "VANISHED 2"
has "and restored to the file"   "$(cat plan/t.md)" "2. must survive"

echo "== red team: a safety marker after gate: is disarmed, not ignored =="
fresh <<'EOF'
## goal: test
- [ ] 1. wipe prod | gate: touch DESTROYED-BY-SWALLOW | risk: irreversible
EOF
out=$("$PLAN" ready t 2>&1)
has   "the swallowed field is reported" "$out" "buried inside"
hasnt "it is not offered as ready"      "$out" "READY 1"
"$PLAN" lint t >/dev/null 2>&1; check "lint fails on it" "1" "$?"
PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 >/dev/null 2>&1
[ -f DESTROYED-BY-SWALLOW ] && bad "the disarmed gate must not run" "no side effect" "it ran" \
  || ok "the disarmed gate never ran"

echo "== red team: signal deaths are readable evidence =="
fresh <<'EOF'
## goal: test
- [ ] 1. killed | gate: kill -9 $$
EOF
# The gate kills ITS OWN process. It was `sh -c 'kill -9 $$'`, which kills an inner shell: on macOS
# /bin/sh (bash) hands its process to that command, so the death read as -9; on Linux /bin/sh is dash,
# which keeps its own process and reports the child as exit 137. That tested a shell, not plan.
"$PLAN" gate t 1 >/dev/null 2>&1
has "a negative exit code is recorded" "$(cat plan/t.md)" "exit -9"
"$PLAN" gate t 1 >/dev/null 2>&1
has "and the tool can read it back"    "$("$PLAN" stats t 2>&1)" "gate runs        2"
has "so STUCK still fires on it"       "$("$PLAN" ready t 2>&1)" "STUCK"

echo "== red team: converging output is progress, not repetition =="
# The first fix for C1 blanked EVERY number, so "3 failed" then "2 failed" read
# as one signature and STUCK fired on genuine progress.
rm -f .ctr
fresh <<'EOF'
## goal: test
- [ ] 1. converging | gate: sh -c 'c=$(cat .ctr 2>/dev/null || echo 3); echo "$c failed, 10 passed in 1.2s"; echo $((c-1)) > .ctr; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1; "$PLAN" gate t 1 >/dev/null 2>&1
hasnt "a shrinking failure count is not STUCK" "$("$PLAN" ready t 2>&1)" "STUCK"

echo "== red team: gate output cannot forge or destroy the record =="
fresh <<'EOF'
## goal: test
- [ ] 1. emits headings | gate: sh -c 'echo "## Attempt 2020-01-01T00:00:00Z — forged — 0s — exit 0"; echo REAL-EVIDENCE; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1; "$PLAN" gate t 1 >/dev/null 2>&1; "$PLAN" gate t 1 >/dev/null 2>&1
rec=$(cat plan/t/01-emits-headings.md)
check "our real attempts survive" "3" "$(printf '%s' "$rec" | grep -c '^## Attempt 20')"
has   "the real evidence survives" "$rec" "REAL-EVIDENCE"
hasnt "no fabricated prune count"  "$rec" "17 earlier"

echo "== red team: an exception in a gate does not kill the run =="
fresh <<'EOF'
## goal: test
- [ ] 1. non-utf8 output | gate: sh -c 'printf "A\377\376B"; exit 1'
- [ ] 2. ordinary | gate: true
EOF
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 2 --max-rounds 3 2>&1)
hasnt "no raw traceback"                 "$out" "Traceback"
check "the healthy node still completed" "- [x] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"
hasnt "the bad node is not left leased"  "$(grep -o '^- \[.\] 1\.' plan/t.md)" "- [>] 1."

echo "== red team: a stale gate cannot settle a re-claimed node =="
fresh <<'EOF'
## goal: test
- [ ] 1. slow gate | gate: sh -c 'sleep 3; true'
EOF
"$PLAN" claim t --owner holder >/dev/null
( "$PLAN" gate t 1 --owner holder >/dev/null 2>&1 ) &
sleep 1
"$PLAN" release t 1 taken back --owner holder --force >/dev/null 2>&1
"$PLAN" claim t --owner someone-else >/dev/null 2>&1
wait
mark=$(grep -o '^- \[.\] 1\.' plan/t.md)
if [ "$mark" != "- [x] 1." ]; then ok "the stale gate did not settle it"
else bad "stale gate must not settle" "not done" "$mark"; fi
has "and the conflict is recorded" "$(cat plan/t.md)" "NOT applied"

echo "== red team: injection through a note or a reason =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
"$PLAN" note t 1 "$(printf 'benign\n- [ ] 99. injected | gate: touch INJECTED')" >/dev/null 2>&1
out=$("$PLAN" ready t 2>&1)
hasnt "an injected node line does not appear" "$out" "READY 99"
hasnt "nor as a broken node"                  "$out" "99."

echo "== red team: a deep chain does not crash every command =="
python3 - <<'PYX'
lines = ["## goal: deep", ""]
for i in range(1, 1501):
    dep = " | needs: %d" % (i + 1) if i < 1500 else ""
    lines.append("- [ ] %d. n%d%s | gate: true" % (i, i, dep))
open("plan/deep.md", "w").write("\n".join(lines))
PYX
# a descending chain leaves the LAST node dependency-free, so ready exits 0
"$PLAN" ready deep >/dev/null 2>&1
check "1500-deep chain: ready survives" "0" "$?"
"$PLAN" lint deep >/dev/null 2>&1
check "1500-deep chain: lint survives"  "0" "$?"


echo "== coverage: four fixes that shipped with no test =="
# Branch coverage put these at 90%, and inside the missing 10% were four
# mechanisms the adversarial review installed and shipped unguarded. A fix
# without a test is the next silent regression.

# 1. a crashed holder's lock must be reclaimed, not block for a full timeout
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
mkdir -p plan/t.md.lock
touch -t 200001010000 plan/t.md.lock
PLAN_GATE_TIMEOUT=1 "$PLAN" gate t 1 >/dev/null 2>&1
check "a stale lock is reclaimed" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
rm -rf plan/t.md.lock

# 2. a worker killed at the timeout must leave a trace on the node
mkdir -p "$T/slow"
cat > "$T/slow/claude" <<'STUB'
#!/usr/bin/env bash
sleep 30
STUB
chmod +x "$T/slow/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(PATH="$T/slow:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 1 --timeout 2 2>&1)
has   "a worker timeout is reported"     "$out" "DISPATCH FAILED"
has   "and recorded on the node"         "$(cat plan/t.md)" "timed out"
check "the node is not left leased"      "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

# 3. a guard denial is not a task failure and must be recorded as such
mkdir -p "$T/denied"
cat > "$T/denied/claude" <<'STUB'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":40,"total_cost_usd":0.01,"result":"blocked","permission_denials":[{"tool":"Bash"},{"tool":"Write"}]}\n'
STUB
chmod +x "$T/denied/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(PATH="$T/denied:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 1 2>&1)
has "denials are reported"                "$out" "permission denial"
has "and persisted for the next attempt"  "$(cat plan/t.md)" "not a task failure"

# 4. an oversize node is warned about at dispatch, not silently exceeded
printf 'y%.0s' $(seq 1 3000) > fat.txt
fresh <<'EOF'
## goal: test
- [ ] 1. huge context | ctx: fat.txt | gate: true
EOF
out=$(PLAN_CTX_BUDGET=100 PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 1 2>&1)
has "an oversize node is warned about" "$out" "over the"
has "and it still runs (advisory)"     "$out" "dispatching"


echo "== a node cannot be allowed to edit its own acceptance criteria =="
# Found by letting the orchestrator implement a command TDD-style: the node's
# ctx named the very test file its gate ran, so the worker was invited to
# rewrite its own gate. It strengthened it -- nothing made that the only option.
fresh <<'EOF'
## goal: test
- [ ] 1. self gating | ctx: bin/x.test.sh | gate: ./bin/x.test.sh
- [ ] 2. fine | ctx: src/a.py | gate: pytest -q
EOF
out=$("$PLAN" ready t 2>&1)
has   "a self-gating node is BROKEN"  "$out" "BROKEN 1"
has   "and says why"                  "$out" "acceptance criteria"
has   "a normal node is unaffected"   "$out" "READY 2"
"$PLAN" lint t >/dev/null 2>&1; check "lint catches it before any run" "1" "$?"


echo "== red team 2: the fixes themselves =="
mkdir -p "$T/stub2"

# RC-1: the gate that RUNS must be the gate that was authorised. A worker swapped
# a claimed node's gate to `true`, let the orchestrator run the substitute, and
# swapped it back -- ALL DONE, exit 0, over a gate that never passed.
cat > "$T/stub2/claude" <<'STUB'
#!/usr/bin/env bash
if sed --version >/dev/null 2>&1; then sed -i 's|gate: test -f NEVER.txt|gate: true|' plan/t.md; else sed -i '' 's|gate: test -f NEVER.txt|gate: true|' plan/t.md; fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":40,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub2/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. swaps its own gate | gate: test -f NEVER.txt
EOF
out=$(PATH="$T/stub2:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 2>&1)
hasnt "a substituted gate is never green" "$out" "ALL DONE"
check "and the node is not done"          "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
[ -f NEVER.txt ] && bad "artefact must not exist" "" "it does" || ok "the real gate never passed"

# RC-1b: APPROVED lives in the non-node text, which was outside the snapshot
cat > "$T/stub2/claude" <<'STUB'
#!/usr/bin/env bash
printf 'APPROVED\n' >> plan/t.md
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":40,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub2/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. grants its own approval | gate: true
EOF
out=$(PATH="$T/stub2:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 2>&1)
has   "a worker cannot grant its own approval" "$out" "non-node text changed"
hasnt "and the run does not complete"          "$out" "ALL DONE"

# RC-2: the MARK is the field the orchestrator's authority is defined by
cat > "$T/stub2/claude" <<'STUB'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":40,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
sleep 0.2
if sed --version >/dev/null 2>&1; then sed -i 's/^- \[ \] 1\./- [x] 1./' plan/t.md; else sed -i '' 's/^- \[ \] 1\./- [x] 1./' plan/t.md; fi
STUB
chmod +x "$T/stub2/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. forges its own done mark | gate: false
EOF
out=$(PATH="$T/stub2:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 2>&1)
hasnt "a forged done mark is never green" "$out" "ALL DONE"

# RC-3: the repair must not destroy proven work. A node that was [x] at snapshot
# time came back [ ] because the restore hardcoded pending -- while printing [x].
cat > "$T/stub2/claude" <<'STUB'
#!/usr/bin/env bash
if sed --version >/dev/null 2>&1; then sed -i 's|gate: KEEPME|gate: false|' plan/t.md; else sed -i '' 's|gate: KEEPME|gate: false|' plan/t.md; fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":40,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub2/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. tamperer | gate: true
- [x] 2. already proven done | gate: KEEPME
EOF
PATH="$T/stub2:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 2 >/dev/null 2>&1
check "proven work is not downgraded by the repair" "- [x] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"
has   "and its gate is restored"                    "$(cat plan/t.md)" "gate: KEEPME"

echo "== red team 2: a shift key must not disarm the hold =="
fresh <<'EOF'
## goal: test
- [ ] 1. wipe prod | gate: touch UPPERCASE-BOOM | RISK: irreversible
EOF
out=$("$PLAN" ready t 2>&1)
has   "uppercase RISK: is caught"  "$out" "buried inside"
hasnt "and is not offered as ready" "$out" "READY 1"

echo "== red team 2: structural validation is not advisory =="
fresh <<'EOF'
## goal: test
- [ ] 1. broken | gate: touch X | risk: irreversible
EOF
"$PLAN" gate t 1 >/dev/null 2>&1;      check "gate refuses a BROKEN node" "6" "$?"
# shellcheck disable=SC1010  # "done" is a literal argument, not a loop keyword
"$PLAN" mark t 1 done >/dev/null 2>&1; check "mark refuses a BROKEN node" "6" "$?"
"$PLAN" note t 1 hi >/dev/null 2>&1;   check "note refuses a BROKEN node" "6" "$?"
"$PLAN" gate t 1 --force >/dev/null 2>&1
[ "$?" -ne 6 ] && ok "--force is the documented override" || bad "--force override" "not 6" "6"

echo "== red team 2: ordinary markdown is not a node =="
fresh <<'EOF'
## goal: test
- [ ] 1. real | gate: true

Notes for later:
- [the design doc](https://example.com/design)
- [another link](https://example.com/other)
EOF
out=$("$PLAN" ready t 2>&1)
has   "the real node is ready"        "$out" "READY 1"
hasnt "link bullets are not reported" "$out" "does not parse"
"$PLAN" lint t >/dev/null 2>&1; check "and the graph lints clean" "0" "$?"


echo "== guards for fixes a mutation probe found NAKED =="
# A probe reverted each recent fix and re-ran every suite. Five survived, i.e.
# nothing would notice if they regressed. Two of those five HAD tests -- they
# asserted a downstream symptom (no ALL DONE) that a different guard also
# produces, so the mechanism under test was never isolated. These assert the
# specific message each mechanism emits.

echo "-- procid: identity must be per-process --"
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
- [ ] 2. b | gate: true
EOF
mkdir -p "$T/stub3"
cat > "$T/stub3/claude" <<'STUB'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":30,"total_cost_usd":0.0,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub3/claude"
PATH="$T/stub3:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 1 >/dev/null 2>&1
own=$(grep -oE 'owner: orch/[^ |]+' plan/t.md | head -1)
if printf '%s' "$own" | grep -qE 'owner: orch/.+@.+/[0-9]+$'; then
  ok "the orchestrator owner carries a pid ($own)"
else bad "orchestrator owner carries a pid" "got: $own"; fi

echo "-- authorised gate: the specific refusal, not a downstream symptom --"
cat > "$T/stub3/claude" <<'STUB'
#!/usr/bin/env bash
if sed --version >/dev/null 2>&1; then sed -i 's|gate: test -f NOPE.txt|gate: true|' plan/t.md; else sed -i '' 's|gate: test -f NOPE.txt|gate: true|' plan/t.md; fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":30,"total_cost_usd":0.0,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub3/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. swaps its gate | gate: test -f NOPE.txt
EOF
out=$(PATH="$T/stub3:$PATH" "$PLAN" run t --accept-risks --workers 1 --max-rounds 1 2>&1)
has "the substituted gate is refused BY NAME" "$out" "not the gate this run authorised"

echo "-- expected_mark: the forged mark is named as tampering --"
# The forgery must land AFTER our own gate settles the node, or our set_node
# simply overwrites it and the mechanism is never exercised. So: two nodes, and
# node 2's worker is still running when node 1 has already been gated.
cat > "$T/stub3/claude" <<'STUB'
#!/usr/bin/env bash
prompt="$2"
if printf '%s' "$prompt" | grep -q '^NODE 2:'; then
  sleep 3
  if sed --version >/dev/null 2>&1; then sed -i 's/^- \[ \] 1\./- [x] 1./' plan/t.md; else sed -i '' 's/^- \[ \] 1\./- [x] 1./' plan/t.md; fi
fi
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":30,"total_cost_usd":0.0,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x "$T/stub3/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. gated first, then forged | gate: false
- [ ] 2. slow worker that forges node 1 | gate: true
EOF
out=$(PATH="$T/stub3:$PATH" "$PLAN" run t --accept-risks --workers 2 --max-rounds 1 2>&1)
has   "a forged mark is reported as TAMPERED" "$out" "TAMPERED 1"
check "and the forged mark is undone"         "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "-- _sig: a changing TIMESTAMP is not a different failure --"
rm -f .tsctr
fresh <<'EOF'
## goal: test
- [ ] 1. timestamped failure | gate: sh -c 'c=$(cat .tsctr 2>/dev/null || echo 0); c=$((c+1)); echo $c > .tsctr; echo "2026-08-1${c}T00:00:0${c}Z ERROR connection refused"; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1; "$PLAN" gate t 1 >/dev/null 2>&1
has "same failure with a moving timestamp is STUCK" "$("$PLAN" ready t 2>&1)" "STUCK: same failure 2x"

echo "-- signal handling: SIGHUP must release in-flight claims --"
cat > "$T/stub3/claude" <<'STUB'
#!/usr/bin/env bash
sleep 25
STUB
chmod +x "$T/stub3/claude"
fresh <<'EOF'
## goal: test
- [ ] 1. slow worker | gate: true
- [ ] 2. slow worker | gate: true
EOF
# No subshell: `( ... ) &` makes $! the SUBSHELL's pid, so kill -HUP never
# reaches plan itself -- which is the known-open "process-directed signal"
# gap, reproduced here by accident.
PATH="$T/stub3:$PATH" "$PLAN" run t --accept-risks --workers 2 --max-rounds 1 >/dev/null 2>&1 &
runpid=$!
sleep 3
leased_before=$(grep -c '^- \[>\]' plan/t.md)
kill -HUP "$runpid" 2>/dev/null
sleep 3
kill -9 "$runpid" 2>/dev/null; wait "$runpid" 2>/dev/null
leased_after=$(grep -c '^- \[>\]' plan/t.md)
if [ "$leased_before" -gt 0 ]; then ok "nodes were leased before the signal ($leased_before)"
else bad "nodes leased before signal" "1 or more" "0 (fixture did not race)"; fi
if [ "$leased_after" -eq 0 ]; then ok "SIGHUP released every in-flight claim"
else bad "SIGHUP releases every in-flight claim" "0 leased" "$leased_after still leased"; fi


echo "== plan status: a dashboard, not a report =="
fresh <<'EOF'
## goal: a real looking graph
- [x] 1. done one | gate: true
      2026-08-18T09:00:00Z exit 0 in 4s — ok
- [>] 2. in flight | owner: orch/me/999 | lease: 2999-01-01T00:00:00Z | gate: true
- [ ] 3. wedged | gate: false
      2026-08-18T10:00:00Z exit 1 in 2s — FAILED test_x at line 40
      2026-08-18T10:05:00Z exit 1 in 2s — FAILED test_x at line 40
- [ ] 4. later | needs: 3 | gate: true
- [ ] 5. deploy | risk: irreversible | gate: true
EOF
out=$("$PLAN" status t 2>&1)
has "shows the goal"              "$out" "a real looking graph"
has "shows progress as a count"   "$out" "1/5"
has "and as a percentage"         "$out" "20%"
has "names what is running"       "$out" "running"
has "with its owner"              "$out" "orch/me/999"
has "flags the wedged node"       "$out" "STUCK"
has "flags the irreversible one"  "$out" "HELD"
has "lists what is blocked"       "$out" "blocked"
has "summarises the gates"        "$out" "gates"
has "and shows the last failure"  "$out" "FAILED test_x at line 40"
lines=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
if [ "$lines" -le 12 ]; then ok "stays compact ($lines lines)"
else bad "stays compact" "12 lines or fewer" "$lines"; fi


echo "== K2: a fenced code block is documentation, not graph =="
# A plan file that documents the node format used to DISPATCH its own example.
fresh <<'EOF'
## goal: a plan that documents itself
- [ ] 1. real work | gate: true

The node format looks like this:

```
- [ ] 99. EXAMPLE ONLY | gate: touch FENCED-EXAMPLE-RAN
```

~~~
- [ ] 98. ALSO AN EXAMPLE | gate: touch TILDE-EXAMPLE-RAN
~~~

- [ ] 2. more real work | needs: 1 | gate: true
EOF
out=$("$PLAN" show t 2>&1)
hasnt "a backtick-fenced example is not a node" "$out" "99."
hasnt "a tilde-fenced example is not a node"    "$out" "98."
has   "the node before the fence survives"      "$out" "1. real work"
has   "and parsing resumes after it"            "$out" "2. more real work"
hasnt "and it is not reported as malformed"     "$out" "does not parse"
"$PLAN" lint t >/dev/null 2>&1; check "the graph lints clean" "0" "$?"

echo "== K3: cycle membership is the whole component =="
# Membership used to come from the DFS path -- one walk through the cycle -- so
# a member reachable only by a second route was under-reported, then dispatched.
fresh <<'EOF'
## goal: test
- [ ] 1. a | needs: 3 | gate: true
- [ ] 2. b | needs: 1 | gate: true
- [ ] 3. c | needs: 2 | gate: true
- [ ] 4. outside the cycle | needs: 1,3 | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
has "every member of the cycle is reported (1)" "$out" "BROKEN 1"
has "every member of the cycle is reported (2)" "$out" "BROKEN 2"
has "every member of the cycle is reported (3)" "$out" "BROKEN 3"
hasnt "a node outside it is not called a cycle" "$out" "BROKEN 4"
fresh <<'EOF'
## goal: test
- [ ] 1. depends on itself | needs: 1 | gate: true
EOF
has "a self-dependency is a cycle" "$("$PLAN" ready t 2>&1)" "BROKEN 1"


echo "== S2: identity is per-session, not per-user and not per-process =="
# Per-PROCESS identity breaks the ordinary flow: every invocation is a new
# process, so claim and the gate that follows would be strangers. The bare
# username is the other failure: everything on one machine becomes one identity
# and settles its own nodes.
#
# This block used to assert identity was per-TERMINAL (the parent pid), and
# H6 of the external certification showed that was wrong for this tool's
# primary consumer: an agent session runs each command in a fresh subshell, so
# claim and gate were always strangers and the documented manual flow
# deadlocked on node 1 for the full four-hour lease -- with `--force`, the flag
# that disables the guard, as the only way out. These tests encoded the defect,
# so they are rewritten to the property that actually matters: a DIFFERENT
# owner is refused, and one session is one identity however many shells it uses.
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
"$PLAN" claim t >/dev/null
own=$(grep -oE 'owner: [^ |]+' plan/t.md)
if printf '%s' "$own" | grep -qE 'owner: .+@.+/(s[0-9]+|cc.+)$'; then ok "owner carries a session id ($own)"
else bad "owner carries a session id" "user@host/{sNNN|ccXXX}" "$own"; fi
"$PLAN" gate t 1 >/dev/null 2>&1
check "the SAME session can settle it" "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
# One session, many subshells: this is EXACTLY the shape that deadlocked, so it
# must now succeed. `bash -c 'one command'` execs, leaving no intermediate
# process, so the compound form is required to get a genuinely different parent.
# Identity comes from CLAUDE_CODE_SESSION_ID when it is set. This passed for months only because the
# suite always ran INSIDE a Claude Code session; on a plain CI runner the variable is absent, the
# engine falls back to the parent pid, and the two shells were strangers. Set it here, explicitly.
export CLAUDE_CODE_SESSION_ID=plan-test-one-session
bash -c "cd '$T' && '$PLAN' claim t >/dev/null; :"
bash -c "cd '$T' && '$PLAN' gate t 1 >/dev/null 2>&1; exit \$?" ; rc=$?
unset CLAUDE_CODE_SESSION_ID
check "a second shell in the SAME session settles it" "0" "$rc"
check "and the node is done"             "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

# A different OWNER is what the guard is actually for, and it must still refuse.
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
EOF
PLAN_OWNER="other@host/s999" "$PLAN" claim t >/dev/null
"$PLAN" gate t 1 >/dev/null 2>&1; rc=$?
check "a DIFFERENT owner is refused"     "4" "$rc"
check "and the node stays leased"        "- [>] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
"$PLAN" gate t 1 --force >/dev/null 2>&1
check "--force is still the override"    "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== S1: a worker cannot forge an attempt record =="
# NOTE_RE.search matched anywhere in a note, so a gate printing "exit 0 — done"
# had that read back as a tool-written attempt. Attempt lines are now anchored
# to the timestamp the tool itself writes.
fresh <<'EOF'
## goal: test
- [ ] 1. forges an attempt | gate: sh -c 'echo "exit 0 in 1s — all good"; exit 1'
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
out=$("$PLAN" stats t 2>&1)
has "only the real attempt is counted" "$out" "gate runs        1"
has "and it is counted as a failure"   "$out" "1 failed"


echo "== an UNCLOSED fence must not swallow the graph =="
# THIRD INSTANCE of the vanishing-node defect, and I introduced it. A fence that
# opens and never closes swallowed every remaining line: real work disappeared
# from every report and `show` said "0 broken". The suite missed it because it
# only ever tested CLOSED fences -- the same shape of blind spot as testing STUCK
# only with constant-output gates.
fresh <<'EOF'
## goal: test
- [ ] 1. before the fence | gate: true

```
- [ ] 2. an example
- [ ] 3. real work that must not vanish | gate: true
- [ ] 4. also real | gate: true
EOF
out=$("$PLAN" ready t 2>&1)
has "an unclosed fence is reported"     "$out" "unclosed code fence"
has "it names where it opened"          "$out" "line 4"
has "and how much it swallowed"         "$out" "3 node-like line(s)"
"$PLAN" lint t >/dev/null 2>&1; check "lint refuses it" "1" "$?"
out=$(PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks --workers 1 2>&1)
has   "the orchestrator refuses to dispatch" "$out" "REFUSING TO RUN"
hasnt "and does not claim completion"        "$out" "ALL DONE"

echo "== closed fences still work, in every shape =="
fresh <<'EOF'
## goal: test
- [ ] 1. real | gate: true
````
- [ ] 90. four-backtick example | gate: true
````
```python
- [ ] 91. language-tagged example | gate: true
```
~~~
- [ ] 92. tilde example | gate: true
~~~
- [ ] 2. real again | gate: true
EOF
out=$("$PLAN" show t 2>&1)
hasnt "four-backtick example is hidden"  "$out" "90."
hasnt "language-tagged example is hidden" "$out" "91."
hasnt "tilde example is hidden"           "$out" "92."
has   "real node before the fences"       "$out" "1. real"
has   "real node after the fences"        "$out" "2. real again"
"$PLAN" lint t >/dev/null 2>&1; check "and it lints clean" "0" "$?"


echo "== happy path =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | gate: true
- [ ] 2. b | needs: 1 | gate: true
EOF
"$PLAN" gate t 1 >/dev/null 2>&1
has "downstream unlocks" "$("$PLAN" ready t 2>&1)" "READY 2"
"$PLAN" gate t 2 >/dev/null 2>&1
has "graph completes"    "$("$PLAN" ready t 2>&1)" "ALL DONE"

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
