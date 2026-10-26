"""Inbox triage: sort the emails that need action into "needs you", "can delegate" and
"FYI", and draft replies to the ones that need the CEO so a reply is one "yes" away.

The sorting uses the priority bands the engine already gave each email; the drafts are
one model call. Drafts are returned, not sent: the conversation turns them into pending
replies with a read-back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ItemRow
from app.domain.enums import ItemKind, ItemStatus, PriorityBand
from app.engine.briefing import clean_title
from app.llm.client import LLMClient, Task
from app.llm.errors import LLMError
from app.llm.prompts import load_prompt, untrusted
from app.llm.schemas import ReplyDrafts

log = logging.getLogger(__name__)

MAX_DRAFTS = 3

# The CEO's writing style, if the drafting-in-your-voice feature is wired.
StyleLookup = Callable[[], str]


@dataclass
class Draft:
    item_id: str
    message_id: str
    source: str
    to: str
    subject: str
    text: str


@dataclass
class Triage:
    needs_you: list[str] = field(default_factory=list)
    delegate: list[str] = field(default_factory=list)
    fyi: list[str] = field(default_factory=list)
    drafts: list[Draft] = field(default_factory=list)

    def summary(self) -> str:
        if not (self.needs_you or self.delegate or self.fyi):
            return "Your inbox is clear: no emails need action."
        parts = []
        if self.needs_you:
            parts.append(f"{len(self.needs_you)} need you")
        if self.delegate:
            parts.append(f"{len(self.delegate)} can be delegated")
        if self.fyi:
            parts.append(f"{len(self.fyi)} are just for your information")
        text = "Of your emails, " + ", ".join(parts) + "."
        if self.needs_you:
            text += " The ones that need you: " + ", ".join(self.needs_you[:5]) + "."
        return text


class InboxTriage:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        style: StyleLookup | None = None,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._style = style

    def set_style(self, style: StyleLookup) -> None:
        self._style = style

    def run(self, draft: bool = True) -> Triage:
        with self._sessions() as s:
            rows = list(
                s.scalars(
                    select(ItemRow)
                    .where(ItemRow.kind == ItemKind.EMAIL_ACTION, ItemRow.status == ItemStatus.OPEN)
                    .order_by(ItemRow.priority_score.desc().nulls_last())
                )
            )
            for r in rows:
                s.expunge(r)
        result = Triage()
        needs: list[ItemRow] = []
        for r in rows:
            label = clean_title(r.title)
            if r.priority_band == PriorityBand.P1_DO_MYSELF:
                result.needs_you.append(label)
                needs.append(r)
            elif r.priority_band == PriorityBand.P2_DELEGATE:
                result.delegate.append(label)
            else:
                result.fyi.append(label)
        if draft and needs:
            result.drafts = self._drafts([r for r in needs if r.requester][:MAX_DRAFTS])
        return result

    def _drafts(self, rows: list[ItemRow]) -> list[Draft]:
        if not rows:
            return []
        emails = "\n\n".join(
            f"item_id: {r.id}\nFrom: {r.requester}\nSubject: {r.title}\n{r.description[:1500]}"
            for r in rows
        )
        system = [load_prompt("triage").text]
        style = self._style() if self._style else ""
        if style:
            system.append("The CEO's writing style:\n" + style)
        try:
            out = self._llm.structured(
                Task.BULK,
                "triage_drafts",
                system=system,
                user=untrusted("emails", emails),
                schema=ReplyDrafts,
                max_tokens=1500,
            )
        except LLMError as exc:
            log.info("could not draft replies: %s", exc)
            return []
        by_id = {r.id: r for r in rows}
        drafts = []
        for d in out.drafts:
            row = by_id.pop(d.item_id, None)
            if row is None or not d.reply.strip() or not row.requester:
                continue
            subject = row.title if row.title.lower().startswith("re:") else f"Re: {row.title}"
            drafts.append(
                Draft(
                    item_id=row.id,
                    message_id=row.source_id.removeprefix("mail:"),
                    source=row.source.value,
                    to=row.requester,
                    subject=subject,
                    text=d.reply.strip(),
                )
            )
        return drafts
