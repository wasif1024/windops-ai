"""Remove secrets and personal data before anything is logged or exported (OBS-003).

Redaction is a safety net. Code should still avoid passing sensitive values to a log.
"""

import re
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"(^|_)(password|passwd|pwd|secret|token|api_key|apikey|private_key|secret_key|access_key"
    r"|signing_key|authorization|cookie|set_cookie|credential|credentials|session_id|email)$"
)

_SENSITIVE_VALUES = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)?"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(lsv2|tvly)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(
        r"(?i)(?<![a-z0-9_])(?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;&]+"
    ),
)


def redact_text(text: str) -> str:
    """Replace sensitive values found inside free text."""
    for pattern in _SENSITIVE_VALUES:
        text = pattern.sub(REDACTED, text)
    return text


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.search(key.lower().replace("-", "_")))


def redact(value: Any) -> Any:
    """Return a copy of a value with sensitive keys and text replaced."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item) for item in value]
    return value
