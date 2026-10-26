"""Two-stage priority scoring.

1. Deterministic pre-scoring from the rubric and item metadata.
2. Claude Haiku scores items in batches (at most ``BATCH_SIZE`` per call)
   with structured output. Items with low confidence, and the top
   ``ESCALATE_TOP`` items, get a second opinion from Claude Sonnet.

Hard rules from the rubric always win. Suggested delegates are validated
against the delegate list. When the monthly budget is exhausted, or Claude
fails, scoring degrades to the deterministic rules alone.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, utc_now
from app.db.models import ItemRow
from app.domain.delegate import Delegate
from app.domain.enums import ItemKind, ItemStatus, PriorityBand
from app.engine.contacts import all_delegates
from app.engine.learning import preferences_block
from app.engine.rubric import CRITERIA, HardRule, Rubric
from app.llm.client import LLMClient, Task
from app.llm.errors import BudgetExceededError, LLMError
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import ScoreBatch, ScoredItem

log = logging.getLogger(__name__)

BATCH_SIZE = 25
ESCALATE_TOP = 5
LOW_CONFIDENCE = 0.6
MAX_REASON_WORDS = 30
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class PreScore:
    score: int
    criteria: dict[str, float]
    requester_tier: str | None
    hard_rule: HardRule | None
    delegate_id: str | None


@dataclass
class ScoringReport:
    scored: int = 0
    escalated: int = 0
    rules_only: bool = False
    errors: list[str] = field(default_factory=list)


def _text(row: ItemRow) -> str:
    return f"{row.title}\n{row.description}".lower()


def _has_any(text: str, keywords: Iterable[str]) -> bool:
    return any(k.lower() in text for k in keywords)


def requester_tier(rubric: Rubric, requester: str | None) -> tuple[str | None, float]:
    if requester:
        email = requester.lower()
        domain = email.rsplit("@", 1)[-1]
        for name, tier in rubric.requester_tiers.items():
            if email in (e.lower() for e in tier.emails):
                return name, tier.weight
        for name, tier in rubric.requester_tiers.items():
            if domain in (d.lower() for d in tier.domains):
                return name, tier.weight
    return None, rubric.unknown_requester_weight


def _deadline(row: ItemRow, now: datetime) -> float:
    when = row.start if row.kind == ItemKind.MEETING else row.due
    if when is None:
        return 0.1
    hours = (when - now).total_seconds() / 3600
    if hours < 0:
        return 1.0 if row.kind != ItemKind.MEETING else 0.0
    if hours <= 24:
        return 0.9
    if hours <= 72:
        return 0.6
    if hours <= 24 * 7:
        return 0.3
    return 0.1


def _ceo_required(row: ItemRow) -> float:
    people = len(row.participants or [])
    if row.kind == ItemKind.MEETING:
        return 0.8 if people <= 3 else 0.4 if people <= 8 else 0.2
    if row.kind == ItemKind.EMAIL_ACTION:
        return 0.5
    return 0.4


def match_delegate(row: ItemRow, delegates: Sequence[Delegate]) -> str | None:
    """Best delegate by overlap between item words and ownership areas."""
    words = set(_WORD.findall(_text(row)))
    best: tuple[int, str] | None = None
    for d in delegates:
        area_words = {w for area in d.ownership_areas for w in _WORD.findall(area.lower())}
        overlap = len(words & area_words)
        if overlap and (best is None or overlap > best[0]):
            best = (overlap, d.id)
    return best[1] if best else None


def _rule_matches(rule: HardRule, row: ItemRow, tier: str | None) -> bool:
    w = rule.when
    if w.requester_tier and tier not in w.requester_tier:
        return False
    if w.requester and (row.requester or "").lower() not in (r.lower() for r in w.requester):
        return False
    if w.keywords and not _has_any(_text(row), w.keywords):
        return False
    if w.source and row.source not in w.source:
        return False
    return not (w.kind and row.kind not in w.kind)


def prescore(
    row: ItemRow, rubric: Rubric, delegates: Sequence[Delegate], now: datetime
) -> PreScore:
    text = _text(row)
    tier, tier_weight = requester_tier(rubric, row.requester)
    goal_keywords = [k for g in rubric.company_goals for k in g.keywords]
    criteria = {
        "strategic_alignment": 1.0 if _has_any(text, goal_keywords) else 0.2,
        "revenue_customer_impact": 1.0 if _has_any(text, rubric.revenue_keywords) else 0.2,
        "requester_weight": tier_weight,
        "deadline_proximity": _deadline(row, now),
        "ceo_uniquely_required": _ceo_required(row),
        "cost_of_skipping": 1.0 if _has_any(text, rubric.urgency_keywords) else 0.3,
    }
    weights = {c: rubric.weights.get(c, 0.0) for c in CRITERIA}
    total = sum(weights.values())
    score = round(100 * sum(criteria[c] * weights[c] for c in CRITERIA) / total)
    rule = next((r for r in rubric.hard_rules if _rule_matches(r, row, tier)), None)
    return PreScore(
        score=_clamp(score, rule),
        criteria=criteria,
        requester_tier=tier,
        hard_rule=rule,
        delegate_id=match_delegate(row, delegates),
    )


def _clamp(score: int, rule: HardRule | None) -> int:
    if rule is None:
        return score
    if rule.min_score is not None:
        score = max(score, rule.min_score)
    if rule.max_score is not None:
        score = min(score, rule.max_score)
    return score


def rules_only_score(row: ItemRow, pre: PreScore, rubric: Rubric) -> ScoredItem:
    t = rubric.rules_only_thresholds
    if pre.hard_rule is not None:
        band = pre.hard_rule.band
        reason = f"Rule applied: {pre.hard_rule.name}."
    elif pre.score >= t.p1_min_score:
        band, reason = PriorityBand.P1_DO_MYSELF, "It scores high on the rubric and needs you."
    elif pre.score < t.drop_below:
        band = PriorityBand.P3_DECLINE if row.kind == ItemKind.MEETING else PriorityBand.P4_DROP
        reason = "It scores low on the rubric."
    else:
        band, reason = PriorityBand.P2_DELEGATE, "It matters but someone else can own it."
    return ScoredItem(
        item_id=row.id,
        score=pre.score,
        band=band,
        reason=reason,
        confidence=0.5,
        ceo_required=band == PriorityBand.P1_DO_MYSELF,
        suggested_delegate_id=pre.delegate_id if band == PriorityBand.P2_DELEGATE else None,
    )


def speakable(reason: str) -> str:
    """One short plain sentence: strip markdown and newlines, cap length."""
    text = re.sub(r"[*_`#>\[\]]", "", reason)
    text = re.sub(r"\s+", " ", text).strip()
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    words = first.split()
    if len(words) > MAX_REASON_WORDS:
        first = " ".join(words[:MAX_REASON_WORDS]).rstrip(",;:") + "."
    if first and first[-1] not in ".!?":
        first += "."
    return first[:300] or "No reason given."


class PriorityEngine:
    def __init__(
        self,
        llm: LLMClient,
        rubric: Rubric,
        session_factory: sessionmaker[Session],
        clock: Clock = utc_now,
    ) -> None:
        self._llm = llm
        self._rubric = rubric
        self._sessions = session_factory
        self._clock = clock

    def _system(self, delegates: Sequence[Delegate], preferences: Sequence[str]) -> list[str]:
        delegation_map = json.dumps(
            [
                {"id": d.id, "name": d.full_name, "ownership_areas": d.ownership_areas}
                for d in sorted(delegates, key=lambda d: d.id)
            ],
            sort_keys=True,
        )
        prefs = "\n".join(f"- {p}" for p in preferences) or "- none yet"
        return [
            load_prompt("priority").text,
            f"Rubric:\n{self._rubric.as_prompt_text()}",
            f"Delegation map:\n{delegation_map}",
            f"CEO's learned preferences:\n{prefs}",
        ]

    def score_pending(self) -> ScoringReport:
        """Score open items that have no score or whose content changed."""
        report = ScoringReport()
        now = self._clock()
        with self._sessions.begin() as s:
            rows = list(
                s.scalars(
                    select(ItemRow)
                    .where(ItemRow.status == ItemStatus.OPEN, ItemRow.scored_at.is_(None))
                    .order_by(ItemRow.id)
                )
            )
            if not rows:
                return report
            delegates = all_delegates(s)
            preferences = preferences_block(s)
            pre = {r.id: prescore(r, self._rubric, delegates, now) for r in rows}
            results = self._score(rows, pre, delegates, preferences, report)
            valid_ids = {d.id for d in delegates}
            for row in rows:
                self._apply(row, results[row.id], pre[row.id], valid_ids, now)
                report.scored += 1
        log.info(
            "priority: scored %d (escalated %d, rules_only=%s)",
            report.scored,
            report.escalated,
            report.rules_only,
        )
        return report

    def _score(
        self,
        rows: list[ItemRow],
        pre: dict[str, PreScore],
        delegates: Sequence[Delegate],
        preferences: Sequence[str],
        report: ScoringReport,
    ) -> dict[str, ScoredItem]:
        fallback = {r.id: rules_only_score(r, pre[r.id], self._rubric) for r in rows}
        system = self._system(delegates, preferences)
        results: dict[str, ScoredItem] = {}
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            try:
                results.update(self._ask(Task.BULK, "priority.score", system, batch, pre, None))
            except BudgetExceededError:
                report.rules_only = True
                return {**fallback, **results}
            except LLMError as exc:
                log.warning("priority batch failed, using rules: %s", exc)
                report.errors.append(str(exc))
        for row_id, item in fallback.items():
            results.setdefault(row_id, item)

        scored_by_llm = [r for r in rows if r.id in results and results[r.id] is not fallback[r.id]]
        top = sorted(scored_by_llm, key=lambda r: results[r.id].score, reverse=True)[:ESCALATE_TOP]
        low = [r for r in scored_by_llm if results[r.id].confidence < LOW_CONFIDENCE]
        to_escalate = list({r.id: r for r in [*top, *low]}.values())
        for i in range(0, len(to_escalate), BATCH_SIZE):
            batch = to_escalate[i : i + BATCH_SIZE]
            try:
                second = self._ask(Task.REASONING, "priority.escalate", system, batch, pre, results)
            except BudgetExceededError:
                report.rules_only = True
                break
            except LLMError as exc:
                log.warning("priority escalation failed, keeping first pass: %s", exc)
                report.errors.append(str(exc))
                continue
            results.update(second)
            report.escalated += len(second)
        return results

    def _ask(
        self,
        task: Task,
        purpose: str,
        system: list[str],
        rows: list[ItemRow],
        pre: dict[str, PreScore],
        first_pass: dict[str, ScoredItem] | None,
    ) -> dict[str, ScoredItem]:
        blocks = []
        for r in rows:
            meta: dict[str, object] = {
                "id": r.id,
                "kind": r.kind.value,
                "source": r.source.value,
                "requester": r.requester,
                "requester_tier": pre[r.id].requester_tier,
                "participants": len(r.participants or []),
                "start": r.start.isoformat() if r.start else None,
                "due": r.due.isoformat() if r.due else None,
                "pre_score": pre[r.id].score,
            }
            if first_pass is not None:
                fp = first_pass[r.id]
                meta["first_pass"] = {
                    "score": fp.score,
                    "band": fp.band.value,
                    "confidence": fp.confidence,
                }
            content = untrusted(r.source.value, f"Title: {r.title}\n{r.description[:2000]}")
            blocks.append(f"Item {json.dumps(meta, sort_keys=True)}\n{content}")
        intro = (
            "Give a careful second opinion on these items; a faster model's first pass is "
            "included for each."
            if first_pass is not None
            else "Score these items."
        )
        user = f"Now: {self._clock().isoformat()}\n{intro}\n\n" + "\n\n".join(blocks)
        batch = self._llm.structured(
            task, purpose, system=system, user=user, schema=ScoreBatch, max_tokens=8000
        )
        wanted = {r.id for r in rows}
        return {s.item_id: s for s in batch.scores if s.item_id in wanted}

    def _apply(
        self,
        row: ItemRow,
        scored: ScoredItem,
        pre: PreScore,
        valid_delegates: set[str],
        now: datetime,
    ) -> None:
        band, score = scored.band, scored.score
        if pre.hard_rule is not None:
            band = pre.hard_rule.band
            score = _clamp(score, pre.hard_rule)
        delegate = scored.suggested_delegate_id
        if delegate not in valid_delegates:
            delegate = pre.delegate_id if band == PriorityBand.P2_DELEGATE else None
        row.priority_score = score
        row.priority_band = band
        row.reason = speakable(scored.reason)
        row.confidence = scored.confidence
        row.ceo_required = scored.ceo_required or band == PriorityBand.P1_DO_MYSELF
        row.suggested_delegate_id = delegate if band == PriorityBand.P2_DELEGATE else None
        row.scored_at = now


__all__ = [
    "BATCH_SIZE",
    "PriorityEngine",
    "ScoringReport",
    "match_delegate",
    "prescore",
    "requester_tier",
    "rules_only_score",
    "speakable",
]
