"""Общие проверки чувствительных данных перед сохранением в памяти."""

from __future__ import annotations

import re

_SENSITIVE_SECRET_PATTERNS = (
    r"(?i)\b(api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S+",
    r"(?i)\bbearer\s+\S+",
    r"(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    r"(?i)\b(?:postgres(?:ql)?|mysql|redis)://[^:\s/@]+:[^@\s]+@",
    r"(?i)\bhttps?://[^/\s:@]+:[^@\s/]+@",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
)
_SENSITIVE_LOCATION_PATTERNS = (
    r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b",
    r"(?i)(?:[A-Z]:\\|/)(?:[^\s,;]+[\\/])*[^\s,;]+",
)


def contains_sensitive_secret(value: str) -> bool:
    """Находит распространённые токены, пароли и приватные ключи."""
    return any(re.search(pattern, value) for pattern in _SENSITIVE_SECRET_PATTERNS)


def contains_sensitive_text(value: str) -> bool:
    """Находит секреты, контактные данные и абсолютные пути в тексте."""
    if contains_sensitive_secret(value):
        return True
    without_web_urls = re.sub(r"(?i)https?://[^\s<>()]+", " ", value)
    return any(
        re.search(pattern, without_web_urls) for pattern in _SENSITIVE_LOCATION_PATTERNS
    )
