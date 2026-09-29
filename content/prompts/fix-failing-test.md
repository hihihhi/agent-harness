# Fix the failing test

Make the failing test(s) pass by fixing the code, not the test.

1. Run the failing test alone and capture the exact error.
2. Search lessons for the error's keywords.
3. Find the root cause: read the assertion, then the code it exercises (only the lines
   needed). State the cause in one sentence before changing anything.
4. Fix the cause with the smallest change.
5. Run the test, then the whole suite. Show both results.

If the test itself is wrong (it asserts behaviour that was deliberately changed), do not
edit it: explain why and ask the human. Never skip, delete or loosen a test to get green.
If the fix taught something non-obvious, record it with `lesson_add`.

Failing test (name or output):
