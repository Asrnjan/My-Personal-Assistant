"""The priority rubric loaded from ``rubric.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.enums import ItemKind, PriorityBand, Source

CRITERIA = (
    "strategic_alignment",
    "revenue_customer_impact",
    "requester_weight",
    "deadline_proximity",
    "ceo_uniquely_required",
    "cost_of_skipping",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Goal(_Strict):
    name: str
    keywords: list[str] = Field(default_factory=list)


class RequesterTier(_Strict):
    weight: float = Field(ge=0, le=1)
    emails: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class RuleCondition(_Strict):
    requester_tier: list[str] = Field(default_factory=list)
    requester: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    source: list[Source] = Field(default_factory=list)
    kind: list[ItemKind] = Field(default_factory=list)


class HardRule(_Strict):
    name: str
    when: RuleCondition
    band: PriorityBand
    min_score: int | None = Field(default=None, ge=0, le=100)
    max_score: int | None = Field(default=None, ge=0, le=100)


class RulesOnlyThresholds(_Strict):
    p1_min_score: int = Field(default=65, ge=0, le=100)
    drop_below: int = Field(default=25, ge=0, le=100)


class Rubric(_Strict):
    weights: dict[str, float] = Field(default_factory=lambda: dict.fromkeys(CRITERIA, 1.0))
    company_goals: list[Goal] = Field(default_factory=list)
    revenue_keywords: list[str] = Field(default_factory=list)
    urgency_keywords: list[str] = Field(default_factory=list)
    requester_tiers: dict[str, RequesterTier] = Field(default_factory=dict)
    unknown_requester_weight: float = Field(default=0.2, ge=0, le=1)
    hard_rules: list[HardRule] = Field(default_factory=list)
    rules_only_thresholds: RulesOnlyThresholds = Field(default_factory=RulesOnlyThresholds)

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = sorted(set(value) - set(CRITERIA))
        if unknown:
            raise ValueError(f"unknown criteria {unknown}; allowed: {list(CRITERIA)}")
        if any(w < 0 for w in value.values()) or sum(value.values()) <= 0:
            raise ValueError("weights must be non-negative and not all zero")
        return value

    def as_prompt_text(self) -> str:
        """Deterministic text form for the (cached) scoring prompt."""
        data: dict[str, Any] = self.model_dump(mode="json", exclude={"rules_only_thresholds"})
        return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)


def load_rubric(path: Path) -> Rubric:
    if not path.exists():
        return Rubric()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping at the top level")
    return Rubric.model_validate(raw)
