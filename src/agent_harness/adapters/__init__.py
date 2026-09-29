"""Adapter registry. A3's modules are imported lazily and may be absent."""
from __future__ import annotations

import importlib
from typing import Dict, List

from .base import Adapter, Ctx, FileChange  # noqa: F401

# name -> (module, class name). Order is the order shown to the user.
_REGISTRY = [
    ("claude-code", "claude_code", "ClaudeCodeAdapter"),
    ("codex", "codex", "CodexAdapter"),
    ("copilot-vscode", "copilot_vscode", None),
    ("cursor", "cursor", None),
    ("gemini", "gemini", None),
    ("agents-md", "agents_md", None),
    ("claude-desktop", "claude_desktop", None),
    ("jupyter-ai", "jupyter_ai", None),
]


def _find_class(mod, name):
    for obj in vars(mod).values():
        if isinstance(obj, type) and issubclass(obj, Adapter) and obj is not Adapter \
                and getattr(obj, "name", "") == name:
            return obj
    return None


def all_adapters() -> Dict[str, Adapter]:
    """Every adapter whose module imports; missing or broken modules are skipped."""
    out: Dict[str, Adapter] = {}
    for name, modname, clsname in _REGISTRY:
        try:
            mod = importlib.import_module(f"{__name__}.{modname}")
        except ImportError:
            continue
        cls = getattr(mod, clsname, None) if clsname else None
        cls = cls or _find_class(mod, name)
        if cls is not None:
            out[name] = cls()
    return out


def names() -> List[str]:
    return list(all_adapters())
