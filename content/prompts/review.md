# Review the current changes

Review the current diff (`git diff` plus staged changes, or the branch against its base).

Look for, in this order:
1. Correctness: logic errors, unhandled cases, wrong assumptions, broken callers.
2. Safety: secrets in code or logs, injection, unsafe file or shell handling, data loss.
3. Tests: is the new behaviour tested, could the tests fail, were any tests weakened?
4. Simplicity: dead code, needless abstraction, duplicated logic, unrelated changes.

For each finding give: file and line, what is wrong, why it matters, and a concrete fix.
Mark each as must-fix or suggestion. Say plainly if you found nothing important.
Do not restate the diff. Do not change code unless asked.
