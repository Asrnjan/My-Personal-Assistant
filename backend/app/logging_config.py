"""Logging setup with redaction of secrets."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
REDACTED = "[REDACTED]"


class RedactingFilter(logging.Filter):
    """Masks known secret values and common token patterns in log messages."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        text = _BEARER.sub(rf"\g<1>{REDACTED}", text)
        return _ANTHROPIC_KEY.sub(REDACTED, text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
