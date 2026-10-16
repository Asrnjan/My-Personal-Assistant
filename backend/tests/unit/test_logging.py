from __future__ import annotations

import logging

import pytest

from app.logging_config import REDACTED, RedactingFilter, configure_logging


def test_redacts_known_secrets_and_patterns() -> None:
    f = RedactingFilter(["s3cret", ""])
    out = f.redact("key=s3cret auth=Bearer abc.def-123 api=sk-ant-api03-XYZ_9")
    assert "s3cret" not in out
    assert "abc.def-123" not in out
    assert "sk-ant" not in out
    assert out.count(REDACTED) == 3


def test_filter_applies_to_formatted_args(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.redact")
    logger.addFilter(RedactingFilter(["hunter2"]))
    with caplog.at_level(logging.INFO, logger="test.redact"):
        logger.info("password is %s", "hunter2")
    assert "hunter2" not in caplog.text
    assert REDACTED in caplog.text


def test_configure_logging_sets_level() -> None:
    configure_logging("warning", ["x"])
    root = logging.getLogger()
    assert root.level == logging.WARNING
    assert any(isinstance(f, RedactingFilter) for f in root.handlers[0].filters)
