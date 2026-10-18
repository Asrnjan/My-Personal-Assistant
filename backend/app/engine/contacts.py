"""Delegates loaded from ``contacts.yaml`` and kept in the database."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import DelegateRow
from app.domain.delegate import Delegate


class ContactsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    delegates: list[Delegate] = []


def load_contacts(path: Path) -> list[Delegate]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    contacts = ContactsFile.model_validate(raw)
    ids = [d.id for d in contacts.delegates]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate delegate ids in {path}: {dupes}")
    return contacts.delegates


def seed_delegates(session_factory: sessionmaker[Session], delegates: list[Delegate]) -> int:
    """Bring delegates in line with ``contacts.yaml``: add new people and update the
    ones whose details in the file changed. Returns how many rows were added or changed.

    The file wins for everyone it lists, so editing it and restarting is enough.
    People added only through the API (not in the file) are left alone.
    """
    changed = 0
    with session_factory.begin() as s:
        existing = {r.id: r for r in s.scalars(select(DelegateRow))}
        for d in delegates:
            values = d.model_dump(mode="json")
            row = existing.get(d.id)
            if row is None:
                s.add(DelegateRow(**values))
                changed += 1
                continue
            updates = {k: v for k, v in values.items() if getattr(row, k) != v}
            for key, value in updates.items():
                setattr(row, key, value)
            changed += bool(updates)
    return changed


def all_delegates(session: Session) -> list[Delegate]:
    return [Delegate.model_validate(r) for r in session.scalars(select(DelegateRow))]


def display_names(delegates: list[Delegate]) -> dict[str, str]:
    """Delegate id → how to say their name: first name, or full name when first names clash."""
    firsts = [d.full_name.split()[0] if d.full_name.split() else d.id for d in delegates]
    return {
        d.id: first if firsts.count(first) == 1 else d.full_name
        for d, first in zip(delegates, firsts, strict=True)
    }
