#!/usr/bin/env bash
# Concurrency stress. The plan file is a line-oriented markdown document edited
# by read-modify-write, and `append_note` INSERTS a line — which shifts every
# subsequent node's cached line index. Several sessions (and several orchestrator
# threads) do this at once. If the lock does not cover every read-modify-write,
# the symptom is silent corruption: a node line overwritten by another node's
# text, a lost mark, or a vanished node.
#
# These tests are deliberately hostile and run many iterations. A single pass
# proves nothing about a race; the loop is the point.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected $2, got $3"; fi; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/plan"; cd "$T" || exit 1

mkplan() {
  n=$1; { echo "## goal: stress"; echo;
    for i in $(seq 1 "$n"); do echo "- [ ] $i. node $i | gate: true"; done; } > plan/t.md
}

echo "== N concurrent gates on N distinct nodes: every node must survive =="
for round in 1 2 3; do
  mkplan 12
  for i in $(seq 1 12); do ( "$PLAN" gate t "$i" >/dev/null 2>&1 ) & done
  wait
  nodes=$(grep -c '^- \[' plan/t.md)
  done_n=$(grep -c '^- \[x\]' plan/t.md)
  if [ "$nodes" -eq 12 ] && [ "$done_n" -eq 12 ]; then ok "round $round: 12/12 nodes intact and done"
  else bad "round $round: 12 nodes intact and done" "nodes=$nodes done=$done_n"; fi
done

echo "== no node line may be corrupted or duplicated =="
mkplan 12
for i in $(seq 1 12); do ( "$PLAN" gate t "$i" >/dev/null 2>&1 ) & done
wait
ids=$(grep -oE '^- \[.\] [0-9]+\.' plan/t.md | grep -oE '[0-9]+' | sort -n | tr '\n' ' ')
check "every id present exactly once" "1 2 3 4 5 6 7 8 9 10 11 12 " "$ids"
mangled=$(grep -c 'node [0-9]*.*node [0-9]*' plan/t.md || true)
check "no line carries two nodes' text" "0" "$mangled"

echo "== concurrent claims never double-issue a node =="
for round in 1 2; do
  mkplan 12
  rm -f claims
  for i in $(seq 1 12); do ( "$PLAN" claim t --owner "w$i" >> claims 2>/dev/null ) & done
  wait
  tot=$(sort claims 2>/dev/null | wc -l | tr -d ' ')
  uniq=$(sort -u claims 2>/dev/null | wc -l | tr -d ' ')
  # Asserting tot = uniq compares a value to itself: it passes on zero claims,
  # so a cmd_claim that always returned NONE READY would keep this green.
  check "round $round: all 12 claims were issued" "12" "$tot"
  check "round $round: none issued twice"         "12" "$uniq"
done

echo "== interleaved claim + gate + note on the SAME node =="
# the nastiest case: append_note shifts line indices under a concurrent writer
for round in 1 2 3; do
  mkplan 4
  for i in 1 2 3 4; do
    ( "$PLAN" gate t "$i" >/dev/null 2>&1 ) &
    ( "$PLAN" note t "$i" "concurrent note $round" >/dev/null 2>&1 ) &
    ( "$PLAN" claim t --owner "c$i" >/dev/null 2>&1 ) &
  done
  wait
  nodes=$(grep -c '^- \[' plan/t.md)
  if [ "$nodes" -eq 4 ]; then ok "round $round: all 4 node lines survived interleaving"
  else bad "round $round: 4 node lines survive" "found $nodes"; fi
  # the file must still be parseable — a corrupted line shows up as BROKEN/does-not-parse
  out=$("$PLAN" show t 2>&1)
  if printf '%s' "$out" | grep -qF "does not parse"; then
    bad "round $round: file still parses" "$(printf '%s' "$out" | grep 'does not parse')"
  else ok "round $round: file still parses cleanly"; fi
done

echo "== notes are never lost under concurrency =="
mkplan 1
for i in $(seq 1 20); do ( "$PLAN" note t 1 "note-$i" >/dev/null 2>&1 ) & done
wait
got=$(grep -c 'note-' plan/t.md)
check "all 20 concurrent notes retained" "20" "$got"

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
