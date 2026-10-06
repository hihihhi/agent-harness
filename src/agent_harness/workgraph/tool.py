"""The `plan` MCP tool: the work graph's command line, run in the project, for any tool the harness serves.

One tool rather than one per subcommand, so it costs a single small definition in every tool's context.
ON by default since v0.3.3 (the owner's decision, src/agent_harness/mcp/checks.py): goals and checks live on disk,
so multi-step work survives context loss. HARNESS_DISABLE=plan turns it off. The engine's own protections do not
depend on this switch.
"""
import os
import shlex
import subprocess
import sys

PLAN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.py")
CAP = 8000          # a node briefing (`plan context`) runs to a few KB; this keeps a whole one
TIMEOUT = 600


def run_cli(args: str, project: str = "") -> dict:
    try:
        argv = shlex.split(args or "")
    except ValueError as e:
        return {"exit": 2, "output": "could not parse the arguments: %s" % e}
    if argv[:1] == ["run"] or (argv[:1] == ["resume"] and "--run" in argv):
        # The orchestrator dispatches worker sessions for as long as the graph takes. That belongs in a
        # terminal the human can watch and stop, not inside one tool call with a timeout.
        return {"exit": None, "output": "`plan run` dispatches worker sessions until the graph drains; start it "
                                        "in a terminal you can watch and stop: plan run <slug> --workers 3"}
    if __package__ in (None, ""):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
        from agent_harness.mcp.state import project_root  # type: ignore
    else:
        from ..mcp.state import project_root
    root = project_root(project or None)
    env = dict(os.environ, PLAN_QUIET="1")
    try:
        p = subprocess.run([sys.executable, PLAN_PY] + argv, cwd=str(root), capture_output=True, text=True,
                           timeout=TIMEOUT, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return {"exit": 124, "output": "plan %s timed out after %ds" % (args, TIMEOUT)}
    out = (p.stdout + p.stderr).strip()
    if len(out) > CAP:
        out = out[:CAP // 4] + "\n... %d bytes cut ...\n" % (len(out) - CAP) + out[-(CAP * 3 // 4):]
    return {"exit": p.returncode, "output": out}
