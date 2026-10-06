"""Which tool gets which harness feature, and why not where it does not: `harness status --matrix`.

One table, so the README and the code cannot disagree. A cell is "yes" or "no: <reason>"; a test fails if an
adapter is missing a row or a cell is blank.
"""
from __future__ import annotations

from typing import Dict

FEATURES = ["guard", "plan", "memory", "review", "run", "discover", "capture", "improve"]

NO_HOOK = "no: the harness installs no hook in %s (Claude Code's PreToolUse/Stop hooks are the only ones wired)"
TERMINAL = "no: run `harness %s` in a terminal; it drives Claude Code or Codex, not %s"

_NOTES = {
    "claude-code": {},
    "codex": {"guard": NO_HOOK % "Codex; its own sandbox and approvals apply", "capture": NO_HOOK % "Codex"},
    "cursor": {"guard": NO_HOOK % "Cursor", "capture": NO_HOOK % "Cursor",
               "review": TERMINAL % ("review", "Cursor"), "run": TERMINAL % ("run", "Cursor"),
               "discover": "no: Open VSX results install with `code`; Cursor's own CLI is not wired yet"},
    "gemini": {"guard": NO_HOOK % "Gemini CLI", "capture": NO_HOOK % "Gemini CLI",
               "review": TERMINAL % ("review", "Gemini CLI"), "run": TERMINAL % ("run", "Gemini CLI"),
               "discover": "no: Gemini extensions are not searched yet"},
    "copilot-vscode": {"guard": NO_HOOK % "VS Code", "capture": NO_HOOK % "VS Code",
                       "review": TERMINAL % ("review", "Copilot"), "run": TERMINAL % ("run", "Copilot")},
    "claude-desktop": {"guard": "no: Claude desktop runs no shell commands of its own to guard",
                       "plan": "no: it has no shell of its own, so its server runs with --no-shell",
                       "review": TERMINAL % ("review", "Claude desktop"), "run": TERMINAL % ("run", "Claude desktop"),
                       "discover": "no: desktop extensions are installed from the app's own directory",
                       "capture": NO_HOOK % "Claude desktop"},
    "jupyter-ai": {"guard": "no: Jupyter AI runs no shell commands of its own to guard",
                   "plan": "no: it has no shell of its own, so its server runs with --no-shell",
                   "review": TERMINAL % ("review", "Jupyter AI"), "run": TERMINAL % ("run", "Jupyter AI"),
                   "discover": "no: Jupyter extensions are not searched", "capture": NO_HOOK % "Jupyter AI"},
    "agents-md": {f: "no: AGENTS.md is rules only; the tool reading it gets no MCP server or hooks"
                  for f in FEATURES},
}


def matrix(names) -> Dict[str, Dict[str, str]]:
    out = {}
    for n in names:
        notes = _NOTES.get(n)
        out[n] = {f: (notes or {}).get(f, "yes") if notes is not None else "no: not described in matrix.py"
                  for f in FEATURES}
    return out


def render(m: Dict[str, Dict[str, str]]) -> str:
    w = max(len(n) for n in m) if m else 10
    lines = [" " * w + "  " + " ".join("%-8s" % f for f in FEATURES)]
    for n, row in m.items():
        lines.append("%-*s  %s" % (w, n, " ".join("%-8s" % ("yes" if row[f] == "yes" else "no") for f in FEATURES)))
    reasons = sorted({v for row in m.values() for v in row.values() if v != "yes"})
    return "\n".join(lines + ["", "why not:"] + ["  " + r[4:] for r in reasons])
