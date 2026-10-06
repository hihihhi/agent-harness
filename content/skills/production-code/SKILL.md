---
name: production-code
description: Production bar for code you write or change - errors, logging, conventions, scope, secrets, docs, and the checks before saying done.
---

# Production code

Correct is the floor. The code is done when it also fails loudly, explains itself in logs, fits the codebase
and leaks nothing, and you have shown each with a command.

## Errors
- Catch only what you can handle, by type. Never `except:` or `except Exception: pass`, and never turn a
  failure into a value the caller cannot tell from success (`None`, `{}`, `0`, `[]`).
- Raise the most specific built-in or a named subclass. The message names the input, file or variable and
  what was expected. Translate with `raise X(...) from e`.
- Validate inputs once, at the boundary. Keep going past a failure only where the task says to, and then
  record each failure (log it, return or count it).

## Logging
- `logging.getLogger(name)` with the name the task gives (else the module's); never `print` for diagnostics.
- WARNING: something skipped or retried; ERROR: an operation failed; DEBUG: detail. One line per event, with
  its context (id, path, line, attempt). `log.exception(...)` keeps the traceback.
- Never log or print a credential, token, cookie or personal data. Never log a headers, env or config dict
  whole: log a copy with secrets replaced (`Authorization: <redacted>`), and test that the secret is absent.

## Conventions and scope
- Match the file's style, naming and structure; keep the public API (names, signatures, return types)
  unless asked to change it. Change only the files the task needs: no drive-by refactors or new dependencies.
- Run the project's linter on the files you changed and fix what it reports. With no project config, use at
  least `ruff check --select E,F,B <files>` (bugbear catches real bugs: `zip()` without `strict=`, `raise`
  without `from` inside `except`).

## Secrets
- Never copy a real value from `.env`, config or the environment into code, tests, fixtures, docs, logs or a
  commit. Tests use obviously fake values (`"test-token"`) set by the test itself.

## Docs
- A new option, behaviour or limit goes into the README, usage text or docstring in the same change: what it
  does and one example.

## Tests
- Follow `write-tests-first`. Every branch you add, every error path and every edge the task names gets a
  test; then prove the tests can fail (break the code on purpose, see red, restore).

## Before you say done
Run and report: the tests, the linter on the changed files, `git status` and `git diff --stat` (only the
files you meant), and a search of your diff for any secret value you saw. A claim without its command and
output is not done.
