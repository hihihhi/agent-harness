# Running jobs (SYNTHETIC)

## Submitting a job

Submit a job with `lumen-run job.py --queue standard`. The job file must define a top-level
function `run_shard(shard_id)`. Lumen calls it once per shard, with shard ids from 0 up.

`lumen-run job.py --plan` prints the plan (shards, queue, memory) and runs nothing. Use it
before every first submission.

## Queues

| queue    | memory cap | longest walltime |
|----------|-----------:|-----------------:|
| quick    |       8 GB |       30 minutes |
| standard |      24 GB |          4 hours |
| bigmem   |      56 GB |         12 hours |

If you do not pass `--queue`, the job goes to `standard`. If you do not set a walltime, the
default is 4 hours, which is also the longest `standard` allows.

## Limits

One user may run at most 12 jobs at the same time. Extra jobs wait in line.

## Retries

A shard that fails is retried twice (`--retries`, default 2) before the job is marked failed.

## Rerunning one shard

`lumen-run job.py --shard 7 --queue bigmem` runs only shard 7. `--split 4` cuts every
selected shard into 4 smaller ones.
