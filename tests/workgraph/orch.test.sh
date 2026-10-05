#!/usr/bin/env bash
# Orchestrator suite. Workers are stubbed so the loop is deterministic and this
# can sit inside release-check; bin/e2e.test.sh proves the same loop against
# live sessions.
#
# The load-bearing test is "a lying worker cannot mark a node done": the
# orchestrator reads worker JSON for DISPATCH health and runs the gate itself
# for TASK truth. Conflating those two is how a harness reports green over work
# that never happened.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     expected: %s\n     got:      %s\n' "$1" "$2" "$3"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "$2" "$3"; fi; }
has()  { if printf '%s' "$2" | grep -qF -- "$3"; then ok "$1"; else bad "$1" "contains '$3'" "$2"; fi; }
hasnt(){ if printf '%s' "$2" | grep -qF -- "$3"; then bad "$1" "NOT contains '$3'" "$2"; else ok "$1"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan" "$T/stub"; cd "$T" || exit 1

# --- stub workers -----------------------------------------------------------
# working: creates whatever file the node's FILES: line names, reports success.
cat > stub/claude <<'EOF'
#!/usr/bin/env bash
prompt="$2"
printf '%s\n' "$prompt" | sed -n 's/^FILES: //p' | head -1 | tr ',' '\n' | while read -r f; do
  f="$(printf '%s' "$f" | sed 's/ (.*//' | tr -d ' ')"
  [ -n "$f" ] && printf 'done\n' > "$f"
done
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":120,"total_cost_usd":0.01,"result":"created it","permission_denials":[]}\n'
EOF
# lying: reports glowing success, changes nothing.
cat > stub/claude-liar <<'EOF'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":90,"total_cost_usd":0.01,"result":"All done! Everything works perfectly.","permission_denials":[]}\n'
EOF
# quota: the API-side wall, which is not a task failure.
cat > stub/claude-quota <<'EOF'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"error_during_execution","is_error":true,"api_error_status":429,"num_turns":0,"duration_ms":50,"total_cost_usd":0,"result":"usage limit reached","permission_denials":[]}\n'
EOF
chmod +x stub/claude stub/claude-liar stub/claude-quota
use() { cp "stub/$1" stub/claude 2>/dev/null || true; }

fresh() { rm -rf "$T/plan" a.txt b.txt c.txt; mkdir -p "$T/plan"; cat > "$T/plan/t.md"; }
run()  { PATH="$T/stub:$PATH" "$PLAN" run t --accept-risks "$@" 2>&1; }

echo "== a broken graph is never dispatched =="
fresh <<'EOF'
## goal: test
- [ ] 1. dangling | needs: 99 | gate: true
EOF
out=$(run --workers 2)
has "refuses and says why" "$out" "REFUSING TO RUN"
has "names the defect"     "$out" "BROKEN 1"

echo "== dry-run claims nothing permanently =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(run --workers 2 --dry-run)
has "reports the dry run"        "$out" "dry-run"
check "node left pending"        "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
[ -f a.txt ] && bad "no work done in dry-run" "no a.txt" "a.txt exists" || ok "no work done in dry-run"

echo "== the loop drains a graph with N workers =="
cp stub/claude stub/claude.bak
fresh <<'EOF'
## goal: test
- [ ] 1. make a | ctx: a.txt | gate: test -f a.txt
- [ ] 2. make b | ctx: b.txt | gate: test -f b.txt
- [ ] 3. needs both | needs: 1,2 | ctx: c.txt | gate: test -f c.txt
EOF
out=$(run --workers 2)
has "dispatches the frontier together" "$out" "dispatching 2 node(s)"
has "reaches the end"                  "$out" "ALL DONE"
check "every node done" "3" "$(grep -c '^- \[x\]' plan/t.md)"
[ -f a.txt ] && [ -f b.txt ] && [ -f c.txt ] && ok "all work landed" || bad "all work landed" "a,b,c" "missing"
has "reports usage as an ESTIMATE, not spend" "$out" "worker usage ~$"

echo "== a lying worker cannot mark a node done =="
cp stub/claude-liar stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. claims success, does nothing | ctx: a.txt | gate: test -f a.txt
EOF
out=$(run --workers 1 --max-rounds 2)
check "node is NOT done"            "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
has   "the gate failure is recorded" "$(cat plan/t.md)" "exit 1"
has   "and it is reported as failed" "$out" "gates failed"
hasnt "no false completion"          "$out" "ALL DONE"
cp stub/claude.bak stub/claude

echo "== a node that fails the same way twice is left, and the rest still drains =="
fresh <<'EOF'
## goal: test
- [ ] 1. impossible | ctx: nope.txt | gate: false
      2026-08-18T00:00:00Z exit 1 in 1s — same
      2026-08-18T00:01:00Z exit 1 in 1s — same
- [ ] 2. fine | ctx: b.txt | gate: test -f b.txt
EOF
out=$(run --workers 2 --max-rounds 3)
has "the stuck node is named, not retried forever" "$out" "STUCK 1"
check "the independent node still completed" "- [x] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"

echo "== irreversible nodes are held, never auto-run =="
fresh <<'EOF'
## goal: test
- [ ] 1. safe | ctx: a.txt | gate: test -f a.txt
- [ ] 2. deploy | risk: irreversible | gate: true
EOF
out=$(run --workers 2 --max-rounds 3)
has   "held and reported"          "$out" "HELD 2"
check "not dispatched"             "- [ ] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"
check "the safe node still ran"    "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"

echo "== quota is not a task failure =="
cp stub/claude-quota stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(run --workers 1 --max-rounds 3)
has   "quota is recognised"           "$out" "QUOTA"
has   "the run halts rather than thrashing" "$out" "halting run"
check "node returned to pending"      "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
has   "with the reason recorded"      "$(cat plan/t.md)" "rolling Claude usage limit"
hasnt "and NOT counted as a gate failure" "$(cat plan/t.md)" "exit 1"
cp stub/claude.bak stub/claude

echo "== 529 overload is not your quota =="
# Observed live: seven workers halted with $0.00 spent while the API was
# overloaded, and the durable record said "quota" -- a false reason for the next
# session to read.
cat > stub/claude <<'STUB'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"error_during_execution","is_error":true,"api_error_status":529,"num_turns":0,"duration_ms":40,"total_cost_usd":0,"result":"Overloaded","permission_denials":[]}\n'
STUB
chmod +x stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(run --workers 1 --max-rounds 2)
has   "529 is labelled overloaded"        "$out" "OVERLOADED"
has   "and named as transient"            "$out" "transient"
has   "and says it is not your account"   "$out" "nothing to do with your account"
has   "and points at resume"              "$out" "plan resume"
hasnt "it is not called quota"            "$out" "QUOTA"
has   "and the file says overloaded, not quota" "$(cat plan/t.md)" "nothing to do with your account"
check "the node is still released"        "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
cp stub/claude-quota stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(run --workers 1 --max-rounds 2)
has   "429 is still labelled quota" "$out" "QUOTA"
hasnt "and not overloaded"          "$out" "OVERLOADED"
cp stub/claude.bak stub/claude

echo "== the round cap bounds an unproductive loop =="
cp stub/claude-liar stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. never satisfiable | ctx: a.txt | gate: false
EOF
out=$(run --workers 1 --max-rounds 2)
has "the cap actually fires" "$out" "stopped at the 2-round cap"
cp stub/claude.bak stub/claude

# --- fixes from the 2026-08-18 adversarial review ---------------------------

echo "== C2: a SUCCESSFUL worker mentioning quota is not a quota event =="
cat > stub/claude <<'STUB'
#!/usr/bin/env bash
prompt="$2"
printf '%s\n' "$prompt" | sed -n 's/^FILES: //p' | head -1 | tr ',' '\n' | while read -r f; do
  f="$(printf '%s' "$f" | sed 's/ (.*//' | tr -d ' ')"
  [ -n "$f" ] && printf 'done\n' > "$f"
done
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":100,"total_cost_usd":0.01,"result":"Added a retry path for the API usage limit error in client.py","permission_denials":[]}\n'
STUB
chmod +x stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
- [ ] 2. b | ctx: b.txt | gate: test -f b.txt
EOF
out=$(run --workers 2)
hasnt "not misread as quota"        "$out" "QUOTA"
hasnt "the run is not halted"       "$out" "halting run"
check "work is kept, node 1 done"   "- [x] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
check "work is kept, node 2 done"   "- [x] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"
hasnt "no false reason in the record" "$(cat plan/t.md)" "quota or rate limit"
cp stub/claude.bak stub/claude

echo "== H1: a crashing dispatch must not strand nodes under a dead owner =="
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
- [ ] 2. b | ctx: b.txt | gate: test -f b.txt
EOF
mkdir -p "$T/empty"
out=$(PATH="$T/empty:/usr/bin:/bin" "$PLAN" run t --accept-risks --workers 2 --max-rounds 1 2>&1)
has   "the failure is reported"      "$out" "DISPATCH FAILED"
hasnt "no raw traceback"             "$out" "Traceback"
check "node 1 released, not leased"  "- [ ] 1." "$(grep -o '^- \[.\] 1\.' plan/t.md)"
check "node 2 released, not leased"  "- [ ] 2." "$(grep -o '^- \[.\] 2\.' plan/t.md)"
has   "recorded as a dispatch failure, not gate evidence" "$(cat plan/t.md)" "dispatch failed"
hasnt "and NOT as a gate exit"       "$(cat plan/t.md)" "exit 1 in"

echo "== H4: an empty graph is not success =="
fresh <<'EOF'
## goal: test with no nodes at all
EOF
out=$(run --workers 2); rc=$?
has   "says it is empty" "$out" "EMPTY"
hasnt "does not claim done" "$out" "ALL DONE"
check "and exits non-zero" "1" "$rc"

echo "== M2: a node a worker invented is visible but not executed =="
cat > stub/claude <<'STUB'
#!/usr/bin/env bash
prompt="$2"
printf '%s\n' "$prompt" | sed -n 's/^FILES: //p' | head -1 | tr ',' '\n' | while read -r f; do
  f="$(printf '%s' "$f" | sed 's/ (.*//' | tr -d ' ')"
  [ -n "$f" ] && printf 'done\n' > "$f"
done
grep -q 'follow-up I invented' plan/t.md || \
  printf -- '- [ ] 9. follow-up I invented | gate: touch WORKER-AUTHORED-GATE-RAN\n' >> plan/t.md
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":100,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. a | ctx: a.txt | gate: test -f a.txt
EOF
out=$(run --workers 1 --max-rounds 3)
has "the new node is surfaced" "$out" "NEW NODE 9"
[ -f WORKER-AUTHORED-GATE-RAN ] && bad "worker-authored gate must not run" "no side effect" "it ran" || ok "worker-authored gate did not run"
cp stub/claude.bak stub/claude

echo "== M6: a STUCK node is named even when the cap ends the run =="
cat > stub/claude <<'STUB'
#!/usr/bin/env bash
printf '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"duration_ms":90,"total_cost_usd":0.01,"result":"ok","permission_denials":[]}\n'
STUB
chmod +x stub/claude
fresh <<'EOF'
## goal: test
- [ ] 1. already stuck | gate: false
      2026-08-18T00:00:00Z exit 1 in 1s — FAILED test_a in 1.11s
      2026-08-18T00:01:00Z exit 1 in 2s — FAILED test_a in 2.22s
- [ ] 2. keeps failing | gate: false
EOF
out=$(run --workers 1 --max-rounds 2)
has   "the stuck node is named"            "$out" "STUCK 1"
hasnt "and is not counted as dispatchable" "$out" "2 dispatchable"
has   "the summary separates stuck"        "$out" "stuck"
cp stub/claude.bak stub/claude

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
