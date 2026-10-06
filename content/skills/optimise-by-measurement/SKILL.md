---
name: optimise-by-measurement
description: Speed code up by measurement - pin exact results, time the real workload, fix the complexity, re-measure. Use when code is slow or must be optimised.
---

# Optimise by measurement

A speed-up is a claim with two halves: the same results, in less time. Prove both.

## Steps

1. **Pin the behaviour first.** Keep the old implementation as an oracle (copy it into the test) and
   assert the new one returns exactly the same results on representative inputs AND the edges: empty, one
   item, duplicates, ties, boundaries that touch, unsorted input, the largest size you will time.
2. **Time the real workload.** The size the task names (or a realistic one), the same machine and input
   for old and new, best of several runs (`timeit`, or `time.perf_counter` in a loop). Write the numbers
   down before changing anything.
3. **Find the hot spot** instead of guessing: `python -m cProfile -s cumtime script.py`, or the language's
   profiler.
4. **Fix the complexity first.** A scan inside a loop (O(n^2)) becomes a dict, set, heap, deque, or sort
   plus one pass; an LRU or queue becomes `OrderedDict` / `deque`; repeated work moves out of the loop.
   Micro-tuning comes last, and only if the measurement says it matters.
5. **Re-measure and compare** on the same input: report old time, new time, the input size and the ratio.
   No gain, revert it.
6. **Keep it exact.** Same outputs, ordering, error types and messages, same public API. No approximation,
   caching of wrong answers, or reduced precision unless the user asked for it.

## Never

- Never claim "faster" or "O(n log n)" without the timing that shows it.
- Never change a test or tolerance to make a faster version pass.
- Never benchmark a toy size and extrapolate; one run is noise, not a result.
