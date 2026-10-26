"""Thinking partner and writer: think a decision through with the CEO's own context, learn
how the CEO writes from their sent mail, and draft longer pieces in their voice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import ItemRow, MemoRow
from app.domain.enums import ItemStatus
from app.engine.learning import preferences_block
from app.engine.memory import Memory, keywords
from app.integrations.registry import Adapters
from app.llm.client import LLMClient, Task
from app.llm.errors import LLMError
from app.llm.prompts import load_prompt, untrusted

log = logging.getLogger(__name__)

STYLE_KEY = "email"
STYLE_SAMPLE = 40
STYLE_LOOKBACK_DAYS = 60
MAX_RELATED_ITEMS = 8


class Thinking:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm: LLMClient,
        adapters_factory: Callable[[], Adapters],
        memory: Memory,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self._llm = llm
        self._adapters = adapters_factory
        self._memory = memory
        self._clock = clock

    # -- writing style ---------------------------------------------------------------

    def style(self) -> str:
        with self._sessions() as s:
            row = s.scalars(
                select(MemoRow).where(MemoRow.kind == "style", MemoRow.key == STYLE_KEY)
            ).first()
            return row.text if row else ""

    def learn_style(self) -> str:
        """Describe how the CEO writes, from their recent sent mail; stored for drafts."""
        try:
            mailboxes = self._adapters().mail
        except Exception:
            log.exception("could not build mail adapters to learn the writing style")
            return ""
        since = self._clock() - timedelta(days=STYLE_LOOKBACK_DAYS)
        sent = []
        for box in mailboxes:
            try:
                sent += box.list_sent(since, STYLE_SAMPLE)
            except Exception as exc:
                log.info("could not read sent mail: %s", exc)
        sample = "\n\n".join(
            f"Subject: {m.subject}\n{m.snippet[:500]}" for m in sent[:STYLE_SAMPLE]
        )
        if not sample:
            return ""
        try:
            response = self._llm.call(
                Task.BULK,
                "writing_style",
                system=[load_prompt("style").text],
                messages=[{"role": "user", "content": untrusted("sent emails", sample)}],
                max_tokens=400,
            )
        except LLMError as exc:
            log.info("could not learn the writing style: %s", exc)
            return ""
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            return ""
        with self._sessions.begin() as s:
            row = s.scalars(
                select(MemoRow).where(MemoRow.kind == "style", MemoRow.key == STYLE_KEY)
            ).first()
            if row is None:
                s.add(MemoRow(kind="style", key=STYLE_KEY, text=text, created_at=self._clock()))
            else:
                row.text = text
                row.created_at = self._clock()
        return text

    # -- deciding -----------------------------------------------------------------------

    def _context(self, question: str) -> str:
        lines = []
        facts = self._memory.recall([question], limit=10)
        if facts:
            lines.append("Things the CEO asked you to remember:\n" + "\n".join(facts))
        with self._sessions() as s:
            prefs = preferences_block(s)
            wanted = keywords(question)
            related = [
                r.title
                for r in s.scalars(select(ItemRow).where(ItemRow.status == ItemStatus.OPEN))
                if wanted & keywords(f"{r.title} {r.description[:300]}")
            ][:MAX_RELATED_ITEMS]
        if prefs:
            lines.append("The CEO's preferences:\n" + "\n".join(f"- {p}" for p in prefs))
        if related:
            lines.append(untrusted("related open work", "\n".join(related)))
        emails = self._emails(question)
        if emails:
            lines.append(untrusted("related emails", "\n".join(emails)))
        return "\n\n".join(lines) or "No other context is available."

    def _emails(self, question: str) -> list[str]:
        """Recent emails about the question's main words, searched one word at a time."""
        words = sorted(keywords(question), key=len, reverse=True)[:3]
        try:
            mailboxes = self._adapters().mail
        except Exception:
            log.exception("could not build mail adapters for a decision")
            return []
        if not words or not mailboxes:
            return []
        found: dict[str, str] = {}
        for word in words:
            try:
                results = mailboxes[0].search(word, limit=3)
            except Exception as exc:
                log.info("email search for a decision failed: %s", exc)
                continue
            for m in results:
                found.setdefault(m.id, f"From {m.sender}: {m.subject}. {m.snippet[:300]}")
        return list(found.values())[:6]

    def think_through(self, question: str) -> str:
        context = self._context(question)
        response = self._llm.call(
            Task.CONVERSATION,
            "decision",
            system=[load_prompt("decide").text],
            messages=[{"role": "user", "content": f"{context}\n\nThe decision: {question}"}],
            max_tokens=1500,
            effort="high",
        )
        return " ".join(b.text for b in response.content if b.type == "text").strip()

    # -- writing --------------------------------------------------------------------------

    def write(self, kind: str, brief: str) -> str:
        system = [load_prompt("writer").text]
        style = self.style()
        if style:
            system.append("The CEO's writing style:\n" + style)
        facts = self._memory.recall([brief], limit=8)
        context = ("Facts you may use:\n" + "\n".join(facts) + "\n\n") if facts else ""
        response = self._llm.call(
            Task.CONVERSATION,
            "writer",
            system=system,
            messages=[{"role": "user", "content": f"{context}Write a {kind}: {brief}"}],
            max_tokens=2000,
        )
        return " ".join(b.text for b in response.content if b.type == "text").strip()
