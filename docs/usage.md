# Usage: install, check, update and uninstall

Python 3.9 or newer, standard library only, under 20 MB. `harness` below is `python3 bin/harness`, or the
`harness` command after `pipx install .` or `uvx --from . harness`.

| command | what it does |
|---|---|
| `harness install --dry-run` | show the plan, change nothing |
| `harness install` | install for every tool found; asks first, backs up every file it changes |
| `harness install --tools claude-code,codex` | only these tools |
| `harness install --project .` | set up only the current project, not your user account |
| `harness status` / `harness doctor` | what is installed, and is it healthy |
| `harness update` | refresh the rules and skills; memory and lessons are kept |
| `harness uninstall` | restore every changed file from its backup and remove what was added |

A real install (without `--dry-run`) **replaces `~/.claude/CLAUDE.md`** and the other tools' instruction files
with the harness rules, and merges into their settings. Every file is backed up first, and `harness uninstall`
restores them all. Memory and lessons stay in `~/.agent-harness` after an uninstall; delete that folder to
remove them too. Which tool gets what: [how-it-works.md](how-it-works.md#supported-tools).

## What the demo shows

Output of `bash scripts/demo.sh` (2026-10-06, v0.3, macOS, Python 3.9) with the per-file lines cut to `...`. It
installs into a throwaway home, printed as `$DEMO_HOME`, never into yours.

```console
$ harness --home $DEMO_HOME install --dry-run --tools claude-code
Claude Code:
  write $DEMO_HOME/.claude/CLAUDE.md - agent rules (replaces the file)
  merge into $DEMO_HOME/.claude/settings.json - adds a dangerous-command guard and a run-the-checks reminder; your hooks stay
  merge into $DEMO_HOME/.claude.json - registers the harness MCP server (harness)
  ...
Dry run: nothing was written.

$ harness --home $DEMO_HOME install --yes --tools claude-code && harness --home $DEMO_HOME doctor
[ok] python 3.9.6
[ok] SQLite FTS5 available
[ok] claude-code: $DEMO_HOME/.claude/CLAUDE.md
  ...
[ok] MCP server answers tools/list within 5 s (14 tools)
[ok] disk footprint 0.5 MB (cap 20 MB)

$ python3 content/hooks/guard.py --check "rm -rf ~"
agent-harness guard blocked this: rm -r on ~ (the home directory). If it is really intended, ask the human to run it themselves.
exit 2

$ python3 content/hooks/guard.py --check "git status && pytest -q"
exit 0
```

## Checks

```sh
bash scripts/demo.sh           # about a second; prints DEMO: PASS
bash scripts/check.sh          # tests, secret scan and the demo; needs pytest; prints CHECK: PASS
```

The CI workflow (`.github/workflows/ci.yml`) runs the tests, the secret scan and the demo on Python 3.9 and
3.12 for every push to main; its current status is the CI badge at the top of the README. The eval (a full pass
spends your Claude Code and Codex usage): [eval/README.md](../eval/README.md).

## Install caveats

- **Packaging was checked offline only:** the wheel was built and installed with Python 3.11 (setuptools 83) by
  `pip install --no-index --no-deps --no-build-isolation --target DIR .`; it needs setuptools>=61 (macOS's
  stock Python 3.9 with setuptools 58 installs an empty `UNKNOWN-0.0.0` that way). `pipx` and `uvx` were not
  available.
- **The work graph is on by default (v0.3.3)**, except in Claude desktop and Jupyter AI (no shell of their own). `HARNESS_DISABLE=plan` turns the `plan` MCP tool off; `plan run`
  dispatches Claude Code workers only, and in the other tools the graph is driven one node at a time.
