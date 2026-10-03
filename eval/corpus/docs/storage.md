# Storage (SYNTHETIC)

## Where to write

Write only under your home folder, `~`. Put datasets in `~/datasets`. The shared reference
data in `/srv/lumen/shared/reference` is read-only for everyone.

## Scratch

Files under `~/scratch` are deleted automatically 14 days after they were last modified. Do not
keep anything there you cannot regenerate.

## Quota

Every home folder has a quota of 200 GB. Writes fail once it is full. To ask for more, see
policies.md, "Quota increases".

## Logs

Each job writes its logs to `~/lumen-logs/<job name>/`. Logs are kept for 30 days.
