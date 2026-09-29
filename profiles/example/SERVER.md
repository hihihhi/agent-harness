# Example research server

A placeholder descriptor. Replace every section with the facts of your own machine. Keep each
section short: the agent fetches sections one at a time, never the whole file.

## Hardware

- 32 CPU cores, 256 GB RAM, 2 GPUs with 24 GB each.
- Local scratch disk mounted at `/scratch` (fast, not backed up, cleaned monthly).

## Storage and data

- Shared datasets: `/data` (read-only for users). Each dataset has a `README.md`.
- Your home directory is backed up nightly; keep large outputs in `/scratch`.

## Software

- Python via `uv`: create a project environment with `uv venv` and `uv pip install`.
- CUDA toolkit is preinstalled; check the version with `nvcc --version`.

## Etiquette

- Check `nvidia-smi` before starting GPU work; use at most one GPU without asking.
- Long jobs go in `tmux` or a job script, so they survive a disconnect.

## Getting help

- Ask the machine's administrator. Do not change system settings yourself.
