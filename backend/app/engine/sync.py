"""One sync pass: ingest from every adapter, then score what changed."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.config import AppConfig
from app.engine.ingest import Ingestor, IngestReport
from app.engine.priority import PriorityEngine, ScoringReport
from app.integrations.registry import Adapters, SyncStateStore


@dataclass
class SyncOutcome:
    ingest: IngestReport
    scoring: ScoringReport


class SyncService:
    def __init__(
        self,
        adapters_factory: Callable[[], Adapters],
        session_factory: sessionmaker[Session],
        app_config: AppConfig,
        priority: PriorityEngine,
        clock: Clock,
    ) -> None:
        self._adapters_factory = adapters_factory
        self._sessions = session_factory
        self._config = app_config
        self._priority = priority
        self._clock = clock

    def run(self) -> SyncOutcome:
        ingestor = Ingestor(
            # Rebuilt per sync so newly connected accounts are picked up without a restart.
            self._adapters_factory(),
            self._sessions,
            SyncStateStore(self._sessions, self._clock),
            self._config,
            self._clock,
        )
        report = ingestor.run()
        return SyncOutcome(report, self._priority.score_pending())
