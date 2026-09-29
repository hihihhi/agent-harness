---
name: verify-before-done
description: Check work with real evidence before saying it is done, fixed or working. Use before any completion claim, before handing work back, and before a commit or pull request.
---

# Verify before done

"Done" is a claim. The evidence is a command that ran and its result.

## Checklist

1. **Restate the goal as a check.** Which command proves the change works? If the project has
   none, write the smallest one that can fail (a test, a script, an assertion on the output).
2. **Run it now,** after the last edit, not an earlier run. Show the command and the lines of
   output that matter.
3. **Run the project's own checks:** tests, type checker, linter, build. The `run_checks` tool,
   when present, finds them and returns only the failures; otherwise use the commands the project
   documents (README, Makefile, package.json, pyproject.toml, CI config).
4. **Check the control.** A check that cannot fail proves nothing. Where cheap, confirm it
   fails without your change (revert, or feed it bad input), then passes with it.
5. **Check the edges** that the change touched: empty input, missing file, large input,
   the second run (idempotence).
6. **Read your diff once** (`git diff`), looking for debug prints, stray files, secrets and
   unrelated changes. Remove them. Once the checks pass and the diff is read, stop: more rounds
   of verification find nothing.

## Rules

- Never edit or weaken a test or check to make it pass. If the check is wrong, say so and
  let the human decide.
- If something could not be verified (no access, too slow, needs a human), say exactly
  what was not verified. Do not round it up to "done".
- Report in a few lines: what changed, the command, the result.
