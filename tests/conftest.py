"""Shared test setup: the tests never read the machine they run on.

The guard reads protected trees from HARNESS_GUARD_PROTECTED_FILE (default /etc/agent-harness/protected-paths). On a
machine that has that file -- a research server running this harness -- the guard is stricter, and a test written
for the plain guard failed there and nowhere else (2026-10-06: `rm file.txt /` blocked by the server's protected
list). Every test therefore starts from an ABSENT list; tests that exercise protected paths set their own and restore
it (tests/test_guard.py).
"""
import os

os.environ["HARNESS_GUARD_PROTECTED_FILE"] = os.path.join(os.path.dirname(__file__), "no-such-protected-paths")
