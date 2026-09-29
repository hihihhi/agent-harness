---
name: write-tests-first
description: Write a failing test before implementing a feature or fixing a bug, then make it pass. Use when adding behaviour, fixing a reported bug, or changing logic that has no test yet.
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

## Good tests

- Test behaviour through public interfaces, not private helpers or call counts.
- Deterministic: no real network, no wall-clock sleeps, no shared global state; use temp
  directories for files.
- Fast: the whole suite should be cheap enough to run after every change.
- A mock that always agrees with the code hides bugs; prefer real objects or small fakes.

## Never

- Never edit or delete an existing test to make your change pass. If it is wrong, say why.
- Never loosen an assertion (tolerance, `assert True`, broad `except`) to get green.
