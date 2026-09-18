from __future__ import annotations

import re
from typing import Any


SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
LONG_ACCOUNT = re.compile(r"\b\d{10,17}\b")
BEARER = re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]+")
PASSWORD_LINE = re.compile(r"(?i)(password\s*[:=]\s*)\S+")

SECRET_KEYS = {
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "ssn",
    "tin",
    "account_number",
    "operator_password",
}


def redact_text(text: str | None) -> str:
    if not text:
        return ""
    out = SSN.sub("[SSN]", text)
    out = LONG_ACCOUNT.sub("[ACCOUNT]", out)
    out = BEARER.sub(r"\1[TOKEN]", out)
    out = PASSWORD_LINE.sub(r"\1[SECRET]", out)
    return out


def looks_secret_key(key: str) -> bool:
    return key.lower() in SECRET_KEYS or any(s in key.lower() for s in ("password", "secret", "token", "ssn"))


def redact_value(key: str, value: Any) -> Any:
    if looks_secret_key(key):
        return "[SECRET]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return redact_obj(value)
    if isinstance(value, list):
        return [redact_value(key, v) for v in value]
    return value


def redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_value(k, v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj


def parameterize_secret(value: str, secrets: dict[str, str]) -> str | None:
    for name, secret in secrets.items():
        if secret and value == secret:
            return "{{secrets." + name + "}}"
    return None
