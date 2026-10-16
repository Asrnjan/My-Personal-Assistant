"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api import auth, routes_health, routes_usage
from app.config import Settings, get_settings, load_app_config
from app.db.session import make_engine, make_session_factory
from app.llm.client import AnthropicSender, LLMClient, MessageSender
from app.llm.usage import BudgetGuard, Clock, UsageLedger, utc_now
from app.logging_config import configure_logging


def create_app(
    settings: Settings | None = None,
    *,
    llm_sender: MessageSender | None = None,
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

    app = FastAPI(title="CEO Assistant", version=__version__)
    app.state.settings = settings
    app.state.app_config = app_config
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.clock = clock
    app.state.usage_ledger = ledger
    app.state.budget = budget
    app.state.llm = LLMClient(sender, app_config.models, ledger, budget)
    app.include_router(routes_health.router)
    app.include_router(auth.router)
    app.include_router(routes_usage.router)
    return app
