---
name: write-tests-first
description: Write a failing test first, make it pass, then prove the tests catch breakage. Use when adding behaviour, fixing a bug, or changing untested logic.
---

# Write tests first

A test written first proves two things: the behaviour was missing, and your change added it.

## Steps

1. **Find the project's test setup.** Look for an existing test next to the code you will
   change and copy its style, runner and fixtures. Do not add a new framework.
2. **Write one test for one behaviour.** Name it after the behaviour
   (`test_rejects_empty_email`), with concrete inputs and the exact expected output.
3. **Run it and watch it fail** for the expected reason (an assertion, not an import error or
   a typo). A test that passes before the change is testing nothing.
4. **Make it pass** with the smallest reasonable change to the real code.
5. **Run the whole suite** to catch what you broke elsewhere.
6. **Add the edge cases** that matter: empty, missing, very large, wrong type, the error path.
   One assertion of intent per test.
7. **Refactor** only while everything is green, and rerun afterwards.

## Prove the tests are strong enough

A handful of happy-path cases is not a test suite. For each behaviour you changed:
- **Every sentence of the request becomes a test** ("input without an offset is UTC" -> a test with an
  offset-free input). Then each input class (empty, one, many, duplicates, touching boundaries, wrong type,
  unicode), each error path, the exact output ordering.
- **Test what must not happen too:** errors that must propagate (`KeyboardInterrupt` past an
  `except Exception`), what must not be retried, files that must not be deleted, secrets not in logs.
- **Assert facts, not wording:** a log line or message must carry the id, path or line number: check those
  with `in` or a regex, never the whole sentence, or the test breaks on a harmless rewording.
- **Many inputs, one invariant:** for arithmetic or data rules, loop over ranges or use a property test
  and assert what must always hold (sums, ordering, round trips), not a few hand-picked values.
- **Break it on purpose:** change one thing in the code (flip a comparison, drop a branch, return early,
  change a constant, delete the log call), run the tests, and see red. A mutation nothing catches is a
  missing test: add it, then restore the code.

## Good tests

- Test behaviour through public interfaces, not private helpers or call counts.
- Deterministic: no real network, no wall-clock sleeps, no shared global state; use temp
  directories for files.
- Fast: the whole suite should be cheap enough to run after every change.
- A mock that always agrees with the code hides bugs; prefer real objects or small fakes.

## Never

- Never edit or delete an existing test to make your change pass. If it is wrong, say why.
- Never loosen an assertion (tolerance, `assert True`, broad `except`) to get green.
