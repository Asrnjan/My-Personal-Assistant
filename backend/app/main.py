"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api import auth, routes_health
from app.config import Settings, get_settings, load_app_config
from app.db.session import make_engine, make_session_factory
from app.logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        settings.log_level,
        secrets=[
            settings.api_auth_token.get_secret_value(),
            settings.anthropic_api_key.get_secret_value(),
            settings.fernet_key.get_secret_value(),
        ],
    )
    app = FastAPI(title="CEO Assistant", version=__version__)
    engine = make_engine(settings.database_url)
    app.state.settings = settings
    app.state.app_config = load_app_config(settings.config_path)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.include_router(routes_health.router)
    app.include_router(auth.router)
    return app
