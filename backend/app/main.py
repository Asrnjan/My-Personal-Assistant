"""FastAPI application factory."""

from __future__ import annotations

import logging
import mimetypes
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.api import (
    auth,
    routes_accounts,
    routes_brief,
    routes_converse,
    routes_health,
    routes_items,
    routes_oauth,
    routes_people,
    routes_push,
    routes_usage,
    routes_voice,
    ws,
)
from app.api.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.clock import Clock, utc_now
from app.config import Settings, get_settings, load_app_config
from app.db.session import make_engine, make_session_factory
from app.engine.actions import ActionService
from app.engine.approval import load_policy
from app.engine.briefing import Briefer
from app.engine.contacts import load_contacts, seed_delegates
from app.engine.conversation import ConversationAgent
from app.engine.delegation import DelegationService
from app.engine.followup import FollowUpService
from app.engine.learning import LearningService
from app.engine.notices import Notices
from app.engine.priority import PriorityEngine
from app.engine.rubric import load_rubric
from app.engine.sync import SyncService
from app.events import EventBus
from app.integrations.notify.base import Notifier
from app.integrations.notify.registry import build_notifiers, telegram_api
from app.integrations.notify.telegram import TelegramApi
from app.integrations.notify.telegram_bot import TelegramBot
from app.integrations.oauth import (
    AppKeyStore,
    OAuthManager,
    TokenStore,
    google_provider,
    microsoft_provider,
)
from app.integrations.registry import Adapters, build_adapters
from app.llm.client import AnthropicSender, LLMClient, MessageSender
from app.llm.usage import BudgetGuard, UsageLedger
from app.logging_config import configure_logging
from app.push import PushSend, WebPusher, pywebpush_sender
from app.scheduler import AssistantJobs, build_scheduler
from app.security import TokenCipher
from app.voice import SpeechToText, TextToSpeech, WhisperSTT, build_tts

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    llm_sender: MessageSender | None = None,
    adapters_factory: Callable[[], Adapters] | None = None,
    notifiers_factory: Callable[[Adapters], list[Notifier]] | None = None,
    clock: Clock = utc_now,
    stt: SpeechToText | None = None,
    tts: TextToSpeech | None = None,
    push_sender: PushSend | None = None,
    telegram: TelegramApi | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, secrets=settings.secret_values())
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
        cipher = TokenCipher(fernet_key)
        oauth = OAuthManager(
            {"google": google_provider(settings), "microsoft": microsoft_provider(settings)},
            TokenStore(session_factory, cipher, clock),
            session_factory,
            settings.public_base_url,
            app_keys=AppKeyStore(session_factory, cipher, clock),
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
    adapters = adapters_factory or (lambda: build_adapters(settings, app_config, oauth))
    notifiers = notifiers_factory or (lambda built: build_notifiers(settings, app_config, built))
    policy = load_policy(settings.approval_policy_path)
    tz = app_config.tz
    bus = EventBus()
    notices = Notices(bus, adapters, notifiers)

    def budget_exceeded(_spend: float, cap: float) -> None:
        notices.tell(
            f"I've reached this month's AI budget of {cap:.0f} dollars, so I'm prioritizing "
            "with rules only until next month."
        )

    budget.set_on_exceeded(budget_exceeded)
    briefer = Briefer(session_factory, app_config, clock)
    learning = LearningService(session_factory, clock)
    delegation = DelegationService(llm, adapters, notifiers, tz, clock)
    actions = ActionService(session_factory, delegation, policy, tz, clock, learning.on_decision)
    followups = FollowUpService(
        session_factory, llm, adapters, notifiers, policy, notices, tz, clock
    )
    agent = ConversationAgent(
        session_factory,
        llm,
        actions,
        briefer,
        delegation,
        learning,
        policy,
        rubric,
        tz,
        clock,
        name=app_config.assistant_name,
    )
    jobs = AssistantJobs(sync_service, briefer, bus, session_factory, followups, actions)

    stt = stt or WhisperSTT(app_config.stt)
    tts = tts or build_tts(app_config.tts, settings.piper_voices_dir)
    pusher: WebPusher | None = None
    vapid_key = settings.vapid_private_key.get_secret_value()
    if push_sender is not None or (vapid_key and settings.vapid_public_key):
        pusher = WebPusher(
            session_factory, push_sender or pywebpush_sender(vapid_key, settings.vapid_subject)
        )
        bus.subscribe(pusher.on_event)
    bot: TelegramBot | None = None
    telegram = telegram or telegram_api(settings)
    if telegram is not None and settings.telegram_ceo_chat_id:
        bot = TelegramBot(
            telegram, settings.telegram_ceo_chat_id, agent, followups, stt=stt, tts=tts
        )
        bot.subscribe(bus)

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
        stop = threading.Event()
        if bot is not None and settings.telegram_polling:
            bot.start(stop)
        try:
            yield
        finally:
            stop.set()
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
    app.state.adapters_factory = adapters
    app.state.notifiers_factory = notifiers
    app.state.policy = policy
    app.state.notices = notices
    app.state.learning = learning
    app.state.delegation = delegation
    app.state.actions = actions
    app.state.followups = followups
    app.state.agent = agent
    app.state.stt = stt
    app.state.tts = tts
    app.state.pusher = pusher
    app.state.telegram_bot = bot
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_limit_per_minute,
        auth_per_minute=settings.auth_rate_limit_per_minute,
        trust_proxy=settings.trust_proxy_headers,
    )
    # Added last so it wraps everything, 429s and CORS preflights included.
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(routes_health.router)
    app.include_router(auth.router)
    app.include_router(routes_usage.router)
    app.include_router(routes_oauth.router)
    app.include_router(routes_accounts.router)
    app.include_router(routes_items.router)
    app.include_router(routes_brief.router)
    app.include_router(routes_converse.router)
    app.include_router(routes_people.router)
    app.include_router(routes_voice.router)
    app.include_router(routes_push.router)
    app.include_router(ws.router)
    if settings.pwa_dir is not None and (settings.pwa_dir / "index.html").exists():
        # Last, so every API route above wins over a file of the same name.
        mimetypes.add_type("application/manifest+json", ".webmanifest")
        app.mount("/", StaticFiles(directory=settings.pwa_dir, html=True), name="pwa")
    return app
