#!/usr/bin/env bash
# The full check: the test suite, the secret scan, and a demo of the install. Exits 0 only if all pass.
#
#   scripts/check.sh
#
# Needs python3 (3.9+) with pytest. The demo installs into a throwaway home (--home), never your own, and
# nothing is downloaded: with --home the installer skips editor extensions.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== tests (the harness, the guard corpus, the eval instrument)"
python3 -m pytest -q tests eval

echo "== secret scan"
bash scripts/secret-scan.sh

echo "== demo: the guard, then install --dry-run, install and doctor in a throwaway home"
if python3 content/hooks/guard.py --check "rm -rf ~"; then
  echo "FAIL: the guard let 'rm -rf ~' through"
  exit 1
fi
demo=$(mktemp -d)
trap 'rm -rf "$demo"' EXIT
python3 bin/harness --home "$demo" install --dry-run --tools claude-code,codex
python3 bin/harness --home "$demo" install --yes --tools claude-code,codex
python3 bin/harness --home "$demo" doctor

echo "CHECK: PASS"
