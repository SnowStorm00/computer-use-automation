from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field
from yaml import safe_load


class Policy(BaseModel):
    id: str
    allowed_origins: list[str]
    allowed_path_prefixes: list[str] = Field(default_factory=list)
    allowed_actions: list[str]
    blocked_actions: list[str] = Field(default_factory=list)
    risky_url_patterns: list[str] = Field(default_factory=list)
    irreversible_text: list[str] = Field(default_factory=list)
    secret_keys: list[str] = Field(default_factory=list)

    def origin_allowed(self, url: str) -> bool:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.allowed_origins:
            return False
        if not self.allowed_path_prefixes:
            return True
        path = parsed.path or "/"
        return any(path.startswith(p) for p in self.allowed_path_prefixes)

    def action_allowed(self, kind: str) -> bool:
        if kind in self.blocked_actions:
            return False
        return kind in self.allowed_actions

    def is_risky_url(self, url: str) -> bool:
        return any(re.search(p, url) for p in self.risky_url_patterns)

    def is_risky_text(self, text: str | None) -> bool:
        if not text:
            return False
        lowered = text.lower()
        return any(token.lower() in lowered for token in self.irreversible_text)


def load_policy(path) -> Policy:
    with open(path, encoding="utf-8") as f:
        return Policy.model_validate(safe_load(f))


class PolicyViolation(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
