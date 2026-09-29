---
name: data-job-trial-run
description: Run data processing, training, migrations or other long or expensive jobs safely by trying them small first. Use before any job that reads or writes many files or rows, runs for more than a few minutes, or costs money or shared resources.
---

# Data job trial run

A full run that fails after an hour costs an hour. A tiny run that fails costs seconds.

## Ladder: tiny, then heavy, then full

1. **Tiny** (seconds): a handful of rows or files, or `--limit 10`. Goal: the code runs end
   to end and the output has the right shape, columns, types and counts.
2. **Heavy** (a minute or two): a slice big enough to hit real-world messiness (the oldest
   file, the largest file, a day with gaps, non-ASCII text). Goal: no errors, and
   throughput measured so you can predict the full run's time, memory and disk.
3. **Full**: only after 1 and 2 pass. State the predicted time and size first. If it is
   long, or uses shared machines or paid services, ask the human before starting.

## At every step

- Write output to a new location; never overwrite the source data or the last good output.
- Check the output, not the exit code: row counts in versus out, nulls, duplicates, a few
  sampled records compared by eye against the input.
- Make the job restartable: process in chunks, skip chunks already done, log progress.
- Watch it while it runs: progress lines, bytes written growing, memory. A process that
  exists is not necessarily a process that is working.
- Record the exact command, input and output paths, and the counts in `state_save`.

## When a step fails

Fix it at the smallest step that reproduces the failure, then climb the ladder again.
If the failure taught something non-obvious, call `lesson_add`.
