## Rules for this machine

- This is a shared machine. Before any job longer than 10 minutes or using more than one GPU,
  check what else is running (`nvidia-smi`, `uptime`) and ask the human.
- Large datasets are read-only. Write results under your own project folder, never next to
  the source data.
- Use the project's virtual environment; never install packages system-wide.
