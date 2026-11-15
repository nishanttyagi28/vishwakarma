"""Secret detection and exclusion for agent context.

Nothing containing a credential ever reaches a model context: sensitive files
are excluded from candidates outright, and secret-shaped strings inside
otherwise-safe files are redacted before assembly.
"""

import re
from pathlib import Path

SECRET_PATTERNS = [
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|secret|token|password|passwd|access[_-]?key|"
        r"client[_-]?secret)\b\s*[:=]\s*[^\s,;\"]{6,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\b(?:sk|rk|ghp|gho)_[A-Za-z0-9]{16,}"),
]

SENSITIVE_NAMES = {".env", ".env.local", ".env.production", "credentials", "secrets"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".pfx", ".p12", ".kdbx"}
REDACTED = "[REDACTED]"


def is_sensitive_path(path: Path) -> bool:
    name = path.name.lower()
    if name in SENSITIVE_NAMES or name.startswith(".env."):
        return True
    return path.suffix.lower() in SENSITIVE_SUFFIXES


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def redact_text(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text
