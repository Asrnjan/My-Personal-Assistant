from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.llm.schemas import AckClassification, DelegationBrief, ScoreBatch, strict_json_schema


def _objects(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(_objects(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_objects(value))
    return found


def test_every_object_is_closed_and_fully_required() -> None:
    for model in (ScoreBatch, DelegationBrief, AckClassification):
        schema = strict_json_schema(model)
        objects = _objects(schema)
        assert objects
        for obj in objects:
            assert obj["additionalProperties"] is False
            assert set(obj["required"]) == set(obj["properties"])


def test_unsupported_keywords_removed_but_field_names_kept() -> None:
    class WithTitleField(BaseModel):
        title: str = Field(min_length=1, max_length=10)
        count: int = Field(ge=0, le=5, default=1)

    schema = strict_json_schema(WithTitleField)
    assert set(schema["properties"]) == {"title", "count"}
    text = str(schema)
    for keyword in ("minLength", "maxLength", "minimum", "maximum", "'default'"):
        assert keyword not in text
    assert "title" not in schema  # the model-level title keyword is gone


def test_original_model_schema_untouched() -> None:
    strict_json_schema(ScoreBatch)
    assert "additionalProperties" not in ScoreBatch.model_json_schema()
