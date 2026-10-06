---
name: debug-systematically
description: Find a bug's root cause before changing code. Use when something fails, throws or misbehaves and the cause is not obvious.
---

# Debug systematically

Guessing and patching wastes tokens and hides the real cause. Work the loop below.

## 1. Reproduce

- Get one command that shows the failure every time (a test, a script, a curl call).
- Record the exact error text and the command. If it is intermittent, loop it until it fails
  and note how often.
- `lesson_search` with the error's key words: this may have happened before.

## 2. Narrow

- Read the stack trace from the bottom frame that is in this project's code.
- Read only the lines around that frame, not the whole file.
- Halve the search space: comment out, bisect inputs, or `git bisect` between a good and a
  bad commit. Each step should cut the suspects roughly in half.

## 3. Hypothesise, then test the hypothesis

- Write one sentence: "It fails because X." Predict what you will see if X is true.
- Test the prediction with a print, a log line or a tiny script. Do not change behaviour yet.
- If the prediction is wrong, the hypothesis is wrong. Discard it; do not patch around it.

## 4. Fix the cause

- Turn the reproduction into a test that fails for the reason you found.
- Make the smallest change that makes it pass. Do not touch the test to make it pass.
- Run the whole relevant test suite, not only the new test.

## 5. Close

- Show the failing-then-passing evidence.
- If the cause was a trap others could fall into, call `lesson_add` (mistake, fix, trigger).

## Stop rule

If the same approach fails twice in the same way, the approach is wrong. Change approach, or
report honestly what you tried and what you observed.
