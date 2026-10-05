---
name: standards
description: Quality bar for tests, evals, gates and review - four test tiers, what makes a gate real, tests that cannot fail. Load before writing a test or eval, or reviewing a diff.
---

# Standards

The floor: give the agent a check it can run, and have a fresh context try to refute the result,
so the one doing the work is not the one grading it.

## Code
- Simplest change that works; every changed line traces to the request; match the local idiom.
- A comment says WHY and names the failure it prevents, not what the next line does.

## Four test tiers, one mark each (`--strict-markers`)
| tier | answers | must |
|---|---|---|
| case | does this unit behave | stated edge cases, one behaviour per test |
| integration | do the seams hold | cross a real boundary: process, file, network |
| property | true for inputs I did not think of | assert the invariant, never the mechanism |
| eval | is end-to-end good enough | measure a **control in the same run** |

A tier that collects zero tests must FAIL. Empty-and-green is a missing artefact passing.

## Every assertion must be able to fail
Probe it: break the code on purpose, re-run, see red. Shapes that have shipped green:
- a needle that also appears in the command under test
- `grep '[x]'` read as a character class (use `grep -F` for literals)
- a value compared with itself; a suite pointed at a renamed binary
- a size or word-count floor, which rewards the padding it means to reject
- a keyword search for a property that is semantic ("No tests anywhere" contains "tests")
- an absent tool returning empty, read as success
- only the easy case covered, so the corpus shares the code's blind spot

## A real gate has eight properties
Subject; claim class; a NAMED failure it catches; a mutant that turns it red AND a control
that stays green; enough resolution to separate the cases that matter; a missing artefact
FAILS; block-or-inform decided in advance; scope limited to the files its node owns. A node
may never own the file its own gate runs: that is editing its acceptance criteria.

## Evals
- The control is the gate: measure the untouched baseline in the same run, and believe it.
- Report effects with their uncertainty; a difference inside noise is not a result.

## Review
| stakes | do |
|---|---|
| any non-trivial diff | review in a fresh context that sees the diff, not the reasoning |
| auth, data, public interface, money | also a second model from another vendor |
| a safety layer (a guard, the settings that load it) | never delegate; read the diff yourself |
| "is it ready?" | an adversarial pass where each finding is handed to a skeptic told to refute it |

A reviewer asked for gaps finds some: act on what breaks correctness or a requirement. Never
weaken a gate to make it pass. Before building, record what already exists (`existing:`).
