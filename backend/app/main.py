"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api import (
    auth,
    routes_brief,
    routes_health,
    routes_items,
    routes_oauth,
    routes_usage,
)
from app.clock import Clock, utc_now
from app.config import Settings, get_settings, load_app_config
from app.db.session import make_engine, make_session_factory
from app.engine.briefing import Briefer
from app.engine.contacts import load_contacts, seed_delegates
from app.engine.priority import PriorityEngine
from app.engine.rubric import load_rubric
from app.engine.sync import SyncService
from app.events import EventBus
from app.integrations.oauth import OAuthManager, TokenStore, google_provider, microsoft_provider
from app.integrations.registry import Adapters, build_adapters
from app.llm.client import AnthropicSender, LLMClient, MessageSender
from app.llm.usage import BudgetGuard, UsageLedger
from app.logging_config import configure_logging
from app.scheduler import AssistantJobs, build_scheduler
from app.security import TokenCipher

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    llm_sender: MessageSender | None = None,
    adapters_factory: Callable[[], Adapters] | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        secrets=[
            settings.api_auth_token.get_secret_value(),
            settings.anthropic_api_key.get_secret_value(),
            settings.fernet_key.get_secret_value(),
        ],
    )
    app_config = load_app_config(settings.config_path)
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    ledger = UsageLedger(session_factory, clock)
    budget = BudgetGuard(
        ledger,
        session_factory,
        monthly_cap_usd=app_config.budget.monthly_usd,
        tz=app_config.tz,
        clock=clock,
    )
    sender = llm_sender or AnthropicSender(
        settings.anthropic_api_key.get_secret_value(), app_config.models.max_retries
    )

    oauth: OAuthManager | None = None
    fernet_key = settings.fernet_key.get_secret_value()
    if fernet_key:
        oauth = OAuthManager(
            {"google": google_provider(settings), "microsoft": microsoft_provider(settings)},
            TokenStore(session_factory, TokenCipher(fernet_key), clock),
            session_factory,
            settings.public_base_url,
            clock=clock,
        )

    rubric = load_rubric(settings.rubric_path)
    contacts = load_contacts(settings.contacts_path)
    llm = LLMClient(sender, app_config.models, ledger, budget)
    priority = PriorityEngine(llm, rubric, session_factory, clock)
    sync_service = SyncService(
        adapters_factory or (lambda: build_adapters(settings, app_config, oauth)),
        session_factory,
        app_config,
        priority,
        clock,
    )
    briefer = Briefer(session_factory, app_config, clock)
    bus = EventBus()
    jobs = AssistantJobs(sync_service, briefer, bus, session_factory)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            seed_delegates(session_factory, contacts)
        except SQLAlchemyError as exc:
            # Keep serving so /health can report the problem.
            log.error("could not seed delegates (has `alembic upgrade head` run?): %s", exc)
        scheduler = build_scheduler(jobs, app_config) if settings.scheduler_enabled else None
        if scheduler is not None:
            scheduler.start()
            log.info("scheduler started (%s)", app_config.timezone)
        try:
            yield
        finally:
            if scheduler is not None:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="CEO Assistant", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.app_config = app_config
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.usage_ledger = ledger
    app.state.budget = budget
    app.state.llm = llm
    app.state.rubric = rubric
    app.state.priority = priority
    app.state.oauth = oauth
    app.state.sync = sync_service
    app.state.briefer = briefer
    app.state.events = bus
    app.state.jobs = jobs
    app.include_router(routes_health.router)
    app.include_router(auth.router)
    app.include_router(routes_usage.router)
    app.include_router(routes_oauth.router)
    app.include_router(routes_items.router)
    app.include_router(routes_brief.router)
    return app
