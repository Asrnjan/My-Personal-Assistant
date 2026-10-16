"""Outbound recipients come from the contacts list or the item's own people,
never from addresses that merely appear in email or task content."""

from __future__ import annotations

from collections.abc import Sequence

from app.db.models import ItemRow
from app.domain.delegate import Delegate


class RecipientGuard:
    def __init__(self, delegates: Sequence[Delegate]) -> None:
        self._contacts = {str(d.email).lower() for d in delegates}

    def allowed(self, address: str, item: ItemRow | None = None) -> bool:
        addr = address.strip().lower()
        if not addr:
            return False
        if addr in self._contacts:
            return True
        if item is None:
            return False
        people = {p.lower() for p in item.participants or []}
        if item.requester:
            people.add(item.requester.lower())
        return addr in people
