"""Secret redaction for anything the harness stores from text it did not write (past conversations, learned
skills). The credential patterns are scripts/secret-scan.sh's CRED list, so what the repo's own scan would
refuse to publish is never stored here either (tests/test_redact.py runs that scan on redacted output);
two looser forms are added for conversations, where secrets appear unquoted (`API_KEY=...`, `Bearer ...`).
"""
from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS = [
    # a whole private-key block, or its header when the block is cut off
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]*"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]
# password = "value", API_KEY=value, token: value -> keep the name, mask the value (8+ chars, as the scan)
_ASSIGN = re.compile(r"(?i)((?:password|passwd|secret|api_?key|access_?token|auth_?token|token)[A-Za-z0-9_]*"
                     r"[\"']?\s*[:=]\s*)([\"']?)([^\"'\s]{8,})\2")


def redact(text: str) -> str:
    """`text` with every credential-looking span replaced by [REDACTED]."""
    if not text:
        return text
    for p in _PATTERNS:
        text = p.sub(MASK, text)
    return _ASSIGN.sub(lambda m: m.group(1) + MASK, text)   # quotes dropped: `x = "[REDACTED]"` still looks like a secret
