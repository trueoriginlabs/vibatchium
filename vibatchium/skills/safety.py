"""Skill-note safety: injection scan on read, secret scan on write/import.

Skills get injected into the agent's context, so they're a prompt-injection
surface (a malicious shared note could smuggle "ignore previous instructions").
We reuse the daemon's ``safety.classify`` heuristic on read. On write/import we
refuse notes containing secret-like material, enforcing the "notes are
shareable, never contain secrets" convention mechanically.
"""
from __future__ import annotations

import re

from .. import safety as _safety

# Conservative secret signatures. Each is (compiled regex, reason). Tight enough
# to avoid flagging ordinary prose, broad enough to catch the obvious leaks a
# note author would accidentally paste.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
     "private_key_block"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key_id"),
    (re.compile(r"\bASIA[0-9A-Z]{16}\b"), "aws_temp_key_id"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "github_token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "slack_token"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "openai_key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
     "jwt"),
    # auth cookies / bearer assignments: a secret-class key set to a long value
    (re.compile(r"(?i)\b(auth[_-]?token|access[_-]?token|refresh[_-]?token|"
                r"session[_-]?id|api[_-]?key|secret|password|passwd|pwd|"
                r"bearer)\b\s*[:=]\s*[\"']?[A-Za-z0-9._\-/+]{12,}"),
     "credential_assignment"),
]


def scan_injection(text: str) -> dict:
    """Run the daemon injection classifier over note text.

    Returns ``{risk, signals}`` — ``risk`` ∈ {none, low, high}.
    """
    result = _safety.classify(text or "")
    return {"risk": result["risk"], "signals": result["signals"]}


def scan_secrets(text: str) -> dict:
    """Detect secret-like material. Returns ``{has_secret, reasons}``."""
    reasons: list[str] = []
    for pattern, reason in _SECRET_PATTERNS:
        if pattern.search(text or "") and reason not in reasons:
            reasons.append(reason)
    return {"has_secret": bool(reasons), "reasons": reasons}
