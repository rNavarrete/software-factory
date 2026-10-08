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
    r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    # Linear API keys.
    r"\blin_(?:api|oauth)_[A-Za-z0-9]{20,}",
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
# Labelled forms ("Authorization: Bearer x", "token=x", "password: 'a b c'"):
# keep the label, drop the whole value. A quoted value is dropped up to its
# closing quote, spaces and all; an unquoted one up to the end of the line or
# the next , ; & } or ]. Any length counts: short passwords are still secrets.
_LABEL = (
    r"authorization|x-api-key|api[_-]?key|client[_-]?secret"
    r"|(?:access|refresh|auth|bearer|fire|routine)[_-]?token|token|secret"
    r"|password|passwd|passphrase"
)
_LABELLED_RE = re.compile(
    # The label may follow an underscore or hyphen ("db_password=").
    rf"(?i)((?<![A-Za-z0-9])(?:{_LABEL})[\"']?\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)"
    # Quoted: to the closing quote, or to the end of the line if it never closes.
    r"(?:(?P<q>[\"'])(?:\\.|(?!(?P=q))[^\n\r])*(?:(?P=q)|(?=[\n\r])|\Z)"
    # Unquoted: to the end of the line or the next separator.
    # An earlier "[REDACTED]" inside it is taken whole, so redacting twice
    # changes nothing.
    r"|(?=[^\s\"'])(?:\[REDACTED\]|[^\n\r,;&}\]])+)"
)
# A bare auth scheme with no label ("... Bearer abc123...").
_SCHEME_RE = re.compile(r"(?i)(\b(?:bearer|basic)\s+)([A-Za-z0-9._~+/=-]{8,})")


# A value stored under one of these keys is a secret whatever it looks like.
_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:authorization|password|passwd|passphrase|pwd|secret|client[_-]?secret"
    r"|api[_-]?key|x-api-key|(?:access|refresh|auth|bearer|fire|routine)?[_-]?token|private[_-]?key)$"
)


def redact(text: str) -> str:
    """``text`` with anything that looks like a credential replaced."""
    text = _LABELLED_RE.sub(_drop_value, text)
    text = _SECRET_RE.sub(REDACTED, text)
    return _SCHEME_RE.sub(lambda m: m[1] + REDACTED, text)


def _drop_value(m: re.Match[str]) -> str:
    quote = m["q"] or ""
    return f"{m[1]}{quote}{REDACTED}{quote}"


def redact_json(value: object) -> object:
    """A copy of a JSON value with every string redacted, keys included, and
    every value under a secret-named key (``password``, ``token``...) replaced.

    Raises ValueError if redacting two keys makes them the same, rather than
    silently dropping one of the values.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for k, v in value.items():
            key = redact(k)
            if key in out:
                raise ValueError(f"redacting keys would merge two entries into {key!r}")
            secret = _SECRET_KEY_RE.match(k) and v is not None
            out[key] = REDACTED if secret else redact_json(v)
        return out
    if isinstance(value, list | tuple):
        return [redact_json(v) for v in value]
    return value
