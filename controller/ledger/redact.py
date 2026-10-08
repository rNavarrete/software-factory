"""Best-effort secret redaction for everything the ledger stores (G-D11).

Pattern-based, so it only catches secrets with a recognisable shape. The
ledger runs it on every string in every event before writing, so a token
that turns up in a fire response or a pasted error never reaches disk.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Anthropic API keys and routine fire tokens (sk-ant-api03-..., sk-ant-oat01-...).
    r"sk-ant-[A-Za-z0-9_-]{8,}",
    # GitHub tokens: classic (ghp_, gho_, ghu_, ghs_, ghr_) and fine-grained.
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"github_pat_[A-Za-z0-9_]{20,}",
    # AWS access key ids.
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    # Slack tokens.
    r"xox[abposr]-[A-Za-z0-9-]{10,}",
    # Google API keys.
    r"\bAIza[0-9A-Za-z_-]{35}\b",
    # PEM private keys, whole block.
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    # Credentials in URLs: https://user:secret@host
    r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)",
]
_SECRET_RE = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.DOTALL)
# Header or assignment forms ("Authorization: Bearer x", "token=x"): keep the
# name, drop the value.
_LABELLED_RE = re.compile(
    r"(?i)(\b(?:authorization|x-api-key|api[_-]?key|access[_-]?token|token|secret|password)"
    r"\"?\s*[:=]\s*\"?(?:bearer\s+)?|\bbearer\s+)([^\s\"',;]{8,})"
)


def redact(text: str) -> str:
    """``text`` with anything that looks like a credential replaced."""
    text = _SECRET_RE.sub(REDACTED, text)
    return _LABELLED_RE.sub(lambda m: m[1] + REDACTED, text)


def redact_json(value: object) -> object:
    """A copy of a JSON value with every string (keys included) redacted."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {redact(k): redact_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_json(v) for v in value]
    return value
