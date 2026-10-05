#!/usr/bin/env bash
# The front half's gates were `grep -q '^## research'` — satisfied by writing the
# HEADING. So analysis, research and plan could each be marked done with no
# thought recorded, in this project's own scaffold.
#
# Measured cost: 8 of 169 node records across ten real graphs mention a skill or
# plugin. ~150 skills sit in context every session and were reached for on 5% of
# real work, because nothing ever required a look. That is the
# unattributed-capability shape: exists, listed, never reached, indistinguishable
# from absent.
#
# The load-bearing property here is NOT "rejects empty". It is "cannot be
# satisfied by writing plausibly", because fluent filler is what a language model
# produces when a gate asks for words.
set -u
# Vendored from agentic-os; PLAN is the engine under test (tests/test_plan_component.py sets it).
PLAN="${PLAN:-$(cd "$(dirname "$0")/../.." && pwd)/src/agent_harness/workgraph/bin/plan}"
CHK="$(dirname "$PLAN")/phase-check"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); [ -n "${QUIET_TESTS:-}" ] || printf '  ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "$2"; }

T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T" || exit 1; git init -q .
fresh() { rm -rf plan; "$PLAN" new d "ship it" >/dev/null 2>&1; cat >> plan/d.md; }
rej() { if "$CHK" d "$2" >/dev/null 2>&1; then bad "$1" "it PASSED"; else ok "$1"; fi; }
acc() { if "$CHK" d "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1" "$("$CHK" d "$2" 2>&1 | head -1)"; fi; }

echo "== a heading alone is not a phase =="
fresh <<'EOF'

## analysis

## research

## plan
EOF
rej "empty analysis"  analysis
rej "empty research"  research
rej "empty plan"      plan

echo "== fluent filler is not a phase either — the real failure mode =="
fresh <<'EOF'

## analysis
We need to ship the thing. It is important for the client and we should do it
well. There are several considerations to keep in mind as we approach this and
we will handle them carefully as we proceed through the implementation phase.

## research
I looked into this and there are a few approaches available. The common way is
generally considered best practice and we will follow it since it is well
established across the industry and widely adopted by many teams today, so it
is the sensible default for our situation here.

## plan
We will build it step by step, testing as we go, and verify each piece works
before moving on to the next one in sequence.
EOF
rej "300 words of filler with no done-condition" analysis
rej "research that never says what already exists" research
rej "a plan with no nodes" plan

echo "== the skill/plugin survey is REQUIRED, and 'none' is a valid answer =="
fresh <<'EOF'

## research
existing: checked ListSkills and SearchPlugins for a markdown-graph runner, and
this repo's own `bin/` — none applicable. Closest is `bin/recall` but it reads
memory, it does not gate. Upstream: reviewed https://example.com/foo and the
`plan.py` scaffold itself.
Constraint: the gate must exit non-zero on filler, so word count alone is not
enough and a structural marker is needed.
EOF
acc "an honest 'none applicable' with what was checked" research
fresh <<'EOF'

## research
There is prior art at https://example.com/a and https://example.com/b which both
solve adjacent problems, and the approach they share is the one we will take
since it is proven at scale and handles our constraints correctly in practice.
EOF
rej "two real sources but NO existing: line still fails" research

echo "== genuine work passes — strict, not hostile =="
fresh <<'EOF'

## analysis
Asked: gate the front half on substance rather than a heading. Out of scope:
changing the node format or the orchestrator. done when: `./bin/phase.test.sh`
exits 0, and a heading-only plan file is refused by `phase-check.py`.

## research
existing: checked ListSkills, SearchPlugins, and `bin/` in this repo — nothing
gates prose substance. `bin/strict.test.sh` gates the node FORMAT, which is the
adjacent problem, and its `reject`/`accept` helper shape is worth copying.
Source: `plan.py` FRONT_TEMPLATE, and the measured 8/169 record count.

## plan
Write `phase-check.py`, gate it with `phase.test.sh` written first, then swap the
FRONT_TEMPLATE gates over. Tests come before the implementation node.
EOF
printf -- '- [ ] 5. write phase-check.py | needs: 4 | gate: ./bin/phase.test.sh\n' >> plan/d.md
acc "a real analysis with a done-condition command" analysis
acc "a real research section naming what it checked" research
acc "a real plan with a gated implementation node" plan

echo "== the deleted no-test check must STAY deleted =="
# Three versions of "the plan must name a test" each could not fail: the
# scaffold's own title matched, then "No tests anywhere" matched, then the shell
# builtin `test` in `test -f out.txt` matched. It is semantic, not lexical.
# This asserts the file carries that record, so the next person reads it before
# writing v4.
if grep -qF 'Do not add a fourth version' "$(dirname "$CHK")/../phase_check.py"; then ok "the reason is recorded in phase_check.py"
else bad "the reason is recorded" "the warning against re-adding it is gone"; fi
fresh <<'EOF'

## plan
Ship it.
EOF
printf -- '- [ ] 5. ship | needs: 4 | gate: test -f out.txt\n' >> plan/d.md
acc "a gated plan passes without a test-naming check" plan

echo "== F2: it must find the plan file from a subdirectory =="
# `plan` walks up to the repo root; this used a bare relative "plan", so
# `cd bin && plan gate t 1` reported "no plan file" on a plan that exists.
mkdir -p deep/deeper && cd deep/deeper || exit 1
if "$CHK" d plan >/dev/null 2>&1; then ok "found from two levels down"
else bad "found from two levels down" "$("$CHK" d plan 2>&1 | head -1)"; fi
cd "$T" || exit 1

echo "== F3: an honest survey must not need markdown formatting =="
# Requiring a URL, a backtick span or a file extension refused the complete
# answer "existing: none applicable - checked ListSkills, SearchPlugins".
fresh <<'EOF'

## research
existing: none applicable - checked ListSkills, SearchPlugins and the bin folder
EOF
acc "plain-text tool names count as sources" research
fresh <<'EOF'

## research
existing: none
I had a look around and it seems fine to build from scratch here.
EOF
rej "but a survey naming nothing still fails" research

echo "== a node with no gate fails the plan phase =="
printf -- '- [ ] 6. do a thing with no gate\n' >> plan/d.md
rej "an ungated node is caught" plan

echo
printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
