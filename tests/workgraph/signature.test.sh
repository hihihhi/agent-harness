#!/usr/bin/env bash
# S1: does STUCK work on the output real test runners actually emit?
#
# The last item DEPLOY.md admits open. The signature was a three-line tail of the
# concatenated stdout+stderr blob, which fails in BOTH directions:
#
#   fails CLOSED — jest and maven put the informative line far above the tail,
#     so two GENUINELY DIFFERENT failures produce the same signature and STUCK
#     fires on a node that is making progress.
#   fails OPEN — a duration or count in the tail changes every run, so two
#     IDENTICAL failures look different and the node is dispatched forever at
#     real API cost.
#
# Every fixture below is the shape a real runner prints, not one invented to
# pass. Both directions are asserted for each.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }

T="$(mktemp -d)"
cleanup() { rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/plan"; cd "$T" || exit 1

# same <name> <script>  -- the SAME failure twice must become STUCK
same() {
  name="$1"; script="$2"
  rm -rf plan; mkdir -p plan; rm -f .n
  printf '## goal: s\n- [ ] 1. %s | gate: %s\n' "$name" "$script" > plan/s.md
  "$PLAN" gate s 1 >/dev/null 2>&1; "$PLAN" gate s 1 >/dev/null 2>&1
  if "$PLAN" ready s 2>&1 | grep -q 'STUCK'; then ok "$name: identical failure twice -> STUCK"
  else bad "$name: identical failure twice -> STUCK" "not detected; the loop would dispatch forever"; fi
}

# diff <name> <script>  -- two DIFFERENT failures must NOT become STUCK
diff_() {
  name="$1"; script="$2"
  rm -rf plan; mkdir -p plan; rm -f .n
  printf '## goal: s\n- [ ] 1. %s | gate: %s\n' "$name" "$script" > plan/s.md
  "$PLAN" gate s 1 >/dev/null 2>&1; "$PLAN" gate s 1 >/dev/null 2>&1
  if "$PLAN" ready s 2>&1 | grep -q 'STUCK'; then
    bad "$name: different failures -> not STUCK" "wrongly STUCK; a converging node would be abandoned"
  else ok "$name: different failures -> not STUCK"; fi
}

echo "== pytest =="
same pytest 'sh -c "echo ===== short test summary info =====; echo \"FAILED tests/test_seam.py::test_x - AssertionError: assert 4 == 5\"; echo \"1 failed, 12 passed in 4.$RANDOM""s\"; exit 1"'
diff_ pytest 'sh -c "c=\$(cat .n 2>/dev/null || echo 0); c=\$((c+1)); echo \$c > .n; echo ===== short test summary info =====; if [ \$c = 1 ]; then echo \"FAILED tests/test_a.py::test_one - AssertionError\"; else echo \"FAILED tests/test_b.py::test_two - TypeError\"; fi; echo \"1 failed, 12 passed in 4.86s\"; exit 1"'

echo "== go test =="
same go 'sh -c "echo \"--- FAIL: TestSeam (0.00s)\"; echo \"    seam_test.go:42: expected 4 got 5\"; echo FAIL; echo \"exit status 1\"; echo \"FAIL	example.com/pkg	0.$RANDOM""s\"; exit 1"'
diff_ go 'sh -c "c=\$(cat .n 2>/dev/null || echo 0); c=\$((c+1)); echo \$c > .n; if [ \$c = 1 ]; then echo \"--- FAIL: TestAlpha (0.00s)\"; else echo \"--- FAIL: TestBeta (0.00s)\"; fi; echo FAIL; echo \"exit status 1\"; echo \"FAIL	example.com/pkg	0.123s\"; exit 1"'

echo "== jest — the informative line sits far above the tail =="
same jest 'sh -c "echo \"  ● Seam › adds up\"; echo \"    expect(received).toBe(expected)\"; echo; echo \"Tests:       1 failed, 12 passed, 13 total\"; echo \"Snapshots:   0 total\"; echo \"Time:        2.$RANDOM s\"; echo \"Ran all test suites.\"; exit 1"'
diff_ jest 'sh -c "c=\$(cat .n 2>/dev/null || echo 0); c=\$((c+1)); echo \$c > .n; if [ \$c = 1 ]; then echo \"  ● Alpha › first thing\"; else echo \"  ● Beta › second thing\"; fi; echo; echo \"Tests:       1 failed, 12 passed, 13 total\"; echo \"Snapshots:   0 total\"; echo \"Time:        2.104 s\"; echo \"Ran all test suites.\"; exit 1"'

echo "== cargo test =="
same cargo 'sh -c "echo failures:; echo \"    tests::seam\"; echo; echo \"test result: FAILED. 1 passed; 1 failed; 0 ignored; finished in 0.0$RANDOM""s\"; exit 101"'
diff_ cargo 'sh -c "c=\$(cat .n 2>/dev/null || echo 0); c=\$((c+1)); echo \$c > .n; echo failures:; if [ \$c = 1 ]; then echo \"    tests::alpha\"; else echo \"    tests::beta\"; fi; echo \"test result: FAILED. 1 passed; 1 failed; 0 ignored; finished in 0.01s\"; exit 101"'

echo "== maven =="
same maven 'sh -c "echo \"[ERROR] SeamTest.adds:42 expected:<4> but was:<5>\"; echo \"[ERROR] Tests run: 13, Failures: 1, Errors: 0, Skipped: 0\"; echo \"[ERROR] There are test failures.\"; echo \"[INFO] Total time:  3.$RANDOM s\"; exit 1"'
diff_ maven 'sh -c "c=\$(cat .n 2>/dev/null || echo 0); c=\$((c+1)); echo \$c > .n; if [ \$c = 1 ]; then echo \"[ERROR] AlphaTest.one:10 expected:<1> but was:<2>\"; else echo \"[ERROR] BetaTest.two:20 expected:<3> but was:<4>\"; fi; echo \"[ERROR] Tests run: 13, Failures: 1, Errors: 0, Skipped: 0\"; echo \"[ERROR] There are test failures.\"; echo \"[INFO] Total time:  3.5 s\"; exit 1"'

echo "== ctest =="
same ctest 'sh -c "echo \"The following tests FAILED:\"; echo \"	  3 - SeamTest (Failed)\"; echo \"Errors while running CTest\"; echo \"Total Test time (real) =   1.$RANDOM sec\"; exit 8"'

echo "== a digit inside an identifier is not a duration =="
same ident 'sh -c "echo \"FAILED tests/test_h2s.py::test_utf8 - AssertionError\"; echo \"1 failed in 0.$RANDOM""s\"; exit 1"'
diff_ ident 'sh -c "c=\$(cat .n 2>/dev/null || echo 0); c=\$((c+1)); echo \$c > .n; if [ \$c = 1 ]; then echo \"FAILED tests/test_h2s.py::test_a - AssertionError\"; else echo \"FAILED tests/test_h3s.py::test_a - AssertionError\"; fi; echo \"1 failed in 0.5s\"; exit 1"'

echo "== a shrinking failure count is progress, not repetition =="
diff_ converging 'sh -c "c=\$(cat .n 2>/dev/null || echo 3); echo \$((c-1)) > .n; echo \"FAILED tests/test_x.py::test_seam - AssertionError\"; echo \"\$c failed, 10 passed in 1.2s\"; exit 1"'

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
