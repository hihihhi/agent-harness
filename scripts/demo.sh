#!/usr/bin/env bash
# The demo: install --dry-run, a real install and doctor, all into a throwaway home; then the guard blocks a
# catastrophic command and lets a safe one through. Exits 0 only if every step behaved.
#
#   bash scripts/demo.sh
#
# Standard library only, nothing downloaded, your own home untouched: --home points the installer at a temp
# folder, and with --home it never installs editor extensions (HOME=DIR would; see the README's Limits).
# HARNESS_HOME is unset so the install cannot land in an existing harness store. The temp path is printed as
# $DEMO_HOME.
set -euo pipefail
cd "$(dirname "$0")/.."
unset HARNESS_HOME

demo=$(mktemp -d)
trap 'rm -rf "$demo"' EXIT
show() { sed "s|$demo|\$DEMO_HOME|g"; }

echo '$ harness --home $DEMO_HOME install --dry-run --tools claude-code'
python3 bin/harness --home "$demo" install --dry-run --tools claude-code | show
if [ -n "$(ls -A "$demo")" ]; then
  echo "FAIL: --dry-run wrote into the home"; exit 1
fi

echo
echo '$ harness --home $DEMO_HOME install --yes --tools claude-code && harness --home $DEMO_HOME doctor'
python3 bin/harness --home "$demo" install --yes --tools claude-code > /dev/null
python3 bin/harness --home "$demo" doctor | show

echo
echo '$ python3 content/hooks/guard.py --check "rm -rf ~"'
set +e
python3 content/hooks/guard.py --check "rm -rf ~"
rc=$?
set -e
echo "exit $rc"
if [ "$rc" -ne 2 ]; then
  echo "FAIL: the guard let 'rm -rf ~' through"; exit 1
fi

echo
echo '$ python3 content/hooks/guard.py --check "git status && pytest -q"'
python3 content/hooks/guard.py --check "git status && pytest -q"
echo "exit $?"

echo
echo "DEMO: PASS"
