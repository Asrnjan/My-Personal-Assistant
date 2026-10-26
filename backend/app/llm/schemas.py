"""Schemas for machine-read model output, plus conversion to strict JSON Schema."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.domain.enums import PriorityBand


class ScoredItem(BaseModel):
    item_id: str
    score: int = Field(ge=0, le=100)
    band: PriorityBand
    reason: str = Field(description="One short spoken sentence, no markdown.")
    confidence: float = Field(ge=0, le=1)
    ceo_required: bool
    suggested_delegate_id: str | None


class ScoreBatch(BaseModel):
    scores: list[ScoredItem]


class DelegationBrief(BaseModel):
    subject: str
    task_summary: str
    why_it_matters: str
    expected_outcome: str
    deadline: str | None
    context_links: list[str]
    handoff_type: Literal["full_ownership", "keep_me_updated"]


class ReplyDraft(BaseModel):
    item_id: str
    reply: str = Field(description="The reply body in the CEO's voice, plain text.")


class ReplyDrafts(BaseModel):
    drafts: list[ReplyDraft]


class AckClassification(BaseModel):
    acknowledged: bool
    has_question: bool
    confidence: float = Field(ge=0, le=1)


# Keywords the structured-output validator does not accept; pydantic still
# enforces them when the response is parsed.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "default",
        "title",
    }
)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return ``model``'s JSON Schema in the strict form structured outputs require.

    Every object gets ``additionalProperties: false`` and lists all of its
    properties as required (optional fields stay nullable), and keywords the
    API rejects are dropped.
    """
    schema = copy.deepcopy(model.model_json_schema())
    _strictify(schema)
    return schema


def _strictify(node: Any) -> None:
    if isinstance(node, list):
        for value in node:
            _strictify(value)
        return
    if not isinstance(node, dict):
        return
    for key in _UNSUPPORTED_KEYWORDS & node.keys():
        del node[key]
    properties = node.get("properties")
    if node.get("type") == "object" and isinstance(properties, dict):
        node["additionalProperties"] = False
        node["required"] = list(properties)
        # Keys of ``properties`` are field names, not keywords: recurse into values only.
        for value in properties.values():
            _strictify(value)
    for key, value in node.items():
        if key != "properties":
            _strictify(value)
