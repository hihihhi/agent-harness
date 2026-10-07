---
name: production-code
description: Production bar for code you write or change - errors, logging without secrets, tests that catch breakage, lint, and the checks before saying done.
---

# Production code

Correct is the floor. Done also means: fails loudly, logs without leaking, tests that would catch a
regression, linter clean, and each shown with a command.

- **Errors:** catch only what you handle, by type; never `except: pass` or a default (`None`, `{}`) the
  caller cannot tell from success. The message names the input, file or variable. Keep going past a failure
  only where asked, and then log and return it.
- **Logs:** the logger the task names; WARNING skipped or retried, ERROR failed, with the id, path or line;
  `log.exception` keeps the traceback. Never log a headers, env or config dict whole: replace secrets
  (`Authorization: <redacted>`) and test the secret is absent from the logs.
- **Tests** (see `write-tests-first`): one test per sentence of the request; what must NOT happen too
  (`KeyboardInterrupt` passes an `except Exception`, no retry of other errors); assert the facts a message
  carries (`in`, regex), not its whole wording.
- **Lint** the files you changed: the project's linter, else `ruff check --select E,F,B <files>`.
- **Secrets:** never copy a value from `.env` or the environment into code, tests or docs; tests use fakes.
- **Before done:** run the tests, the linter, `git status` (only the files you meant; delete scratch
  scripts), and report the commands and results.
