"""Long-term memory: facts the CEO asks the assistant to remember ("Acme's renewal is in
March", "Priya prefers WhatsApp"), recalled in conversation, meeting prep and decisions.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.db.models import MemoRow

KIND = "fact"
# Facts given to the model with every conversation turn (newest first).
MAX_IN_PROMPT = 60
_WORD = re.compile(r"[a-z0-9']{3,}")
_STOP = {
    "the", "and", "for", "with", "that", "this", "about", "from", "they", "their", "what",
    "should", "would", "could", "whether", "which", "there", "have", "been", "need", "want",
    "will", "our", "your", "you", "are", "was", "not", "but", "can", "how", "why", "when",
}  # fmt: skip


def _stem(word: str) -> str:
    word = word.removesuffix("'s").strip("'")
    return word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word


def keywords(text: str) -> set[str]:
    """Content words, lightly normalised so "Acme's" matches "Acme" and "deals" "deal"."""
    return {s for s in (_stem(w) for w in _WORD.findall(text.lower())) if s and s not in _STOP}


class Memory:
    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._sessions = session_factory
        self._clock = clock

    def remember(self, fact: str) -> str:
        fact = " ".join(fact.split()).rstrip(".") + "."
        key = hashlib.sha256(fact.lower().encode()).hexdigest()[:40]
        with self._sessions.begin() as s:
            row = s.scalars(select(MemoRow).where(MemoRow.kind == KIND, MemoRow.key == key)).first()
            if row is None:
                s.add(MemoRow(kind=KIND, key=key, text=fact, created_at=self._clock()))
            else:
                row.active = True
        return fact

    def all(self, limit: int = MAX_IN_PROMPT) -> list[str]:
        with self._sessions() as s:
            return list(
                s.scalars(
                    select(MemoRow.text)
                    .where(MemoRow.kind == KIND, MemoRow.active.is_(True))
                    .order_by(MemoRow.created_at.desc())
                    .limit(limit)
                )
            )

    def recall(self, about: list[str], limit: int = 8) -> list[str]:
        """Facts sharing a word with any of ``about`` (names, a title, a question)."""
        wanted = set().union(*(keywords(a) for a in about)) if about else set()
        if not wanted:
            return []
        scored = [(len(keywords(f) & wanted), f) for f in self.all(limit=500)]
        return [f for n, f in sorted(scored, key=lambda x: -x[0]) if n][:limit]

    def forget(self, text: str) -> int:
        wanted = keywords(text)
        with self._sessions.begin() as s:
            rows = list(
                s.scalars(select(MemoRow).where(MemoRow.kind == KIND, MemoRow.active.is_(True)))
            )
            gone = 0
            for r in rows:
                if wanted and len(keywords(r.text) & wanted) >= max(1, (len(wanted) + 1) // 2):
                    r.active = False
                    gone += 1
            return gone
