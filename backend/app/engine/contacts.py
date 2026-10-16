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
    """Insert delegates from the file that are not in the database yet.

    Existing rows are left alone: once a delegate exists, the API is the
    source of truth, so edits made there are not overwritten on restart.
    """
    added = 0
    with session_factory.begin() as s:
        existing = set(s.scalars(select(DelegateRow.id)))
        for d in delegates:
            if d.id in existing:
                continue
            s.add(DelegateRow(**d.model_dump(mode="json")))
            added += 1
    return added


def all_delegates(session: Session) -> list[Delegate]:
    return [Delegate.model_validate(r) for r in session.scalars(select(DelegateRow))]
