# Overview (SYNTHETIC)

## Cluster

The Lumen cluster has 6 worker nodes. Each worker has 16 CPU cores and 64 GB of RAM. A job always runs on exactly one worker: Lumen does not spread one job
across machines.

## Memory on a worker

8 GB of each worker's RAM is reserved for the operating system and the Lumen agent, so a job
can use at most 56 GB. No queue may grant more than that (see jobs.md, "Queues").

## Who runs it

The platform owner is the person who approves exceptions to quotas and queue limits. Day to
day questions go to the support desk (policies.md, "Support hours").
