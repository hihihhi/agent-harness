# Runbook (SYNTHETIC)

## A shard was killed with exit code 137

Exit code 137 means the shard ran out of memory and the worker killed it. Two fixes:

1. Rerun only that shard on the largest queue: `lumen-run job.py --shard N --queue bigmem`.
2. Split it into smaller pieces with `--split 4`, so each piece needs about a quarter of the
   memory.

Do not simply resubmit the whole job: the other shards already finished.

## A job is stuck waiting

A job waits when its owner already has 12 jobs running. Check `lumen-run --list`.

## The home folder is full

Delete old files, or ask for a quota increase (policies.md).
