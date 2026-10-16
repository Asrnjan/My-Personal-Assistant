"""Errors raised by the LLM layer."""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class for LLM-layer failures."""


class BudgetExceededError(LLMError):
    """The monthly budget cap is reached; callers fall back to rules-only behaviour."""


class LLMRefusalError(LLMError):
    """The model declined the request."""


class LLMTruncatedError(LLMError):
    """The response hit ``max_tokens`` before finishing."""


class LLMOutputError(LLMError):
    """The model did not return output matching the requested schema."""
