"""OAuth 2.0 authorization-code flow (with PKCE) and encrypted token storage
for Google and Microsoft."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, utc_now
from app.config import Settings
from app.db.models import OAuthStateRow, OAuthTokenRow
from app.integrations.http import AuthExpiredError, IntegrationError
from app.security import TokenCipher

STATE_TTL = timedelta(minutes=10)
REFRESH_MARGIN = timedelta(seconds=60)

GOOGLE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)
MICROSOFT_SCOPES = (
    "offline_access",
    "User.Read",
    "Calendars.ReadWrite",
    "Mail.ReadWrite",
    "Mail.Send",
)


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    authorize_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    extra_authorize_params: dict[str, str] = field(default_factory=dict)


def google_provider(settings: Settings) -> OAuthProvider:
    return OAuthProvider(
        name="google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106 - URL, not a secret
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        scopes=GOOGLE_SCOPES,
        # offline + consent so Google returns a refresh token.
        extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    )


def microsoft_provider(settings: Settings) -> OAuthProvider:
    base = f"https://login.microsoftonline.com/{settings.ms_tenant_id}/oauth2/v2.0"
    return OAuthProvider(
        name="microsoft",
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        client_id=settings.ms_client_id,
        client_secret=settings.ms_client_secret.get_secret_value(),
        scopes=MICROSOFT_SCOPES,
    )


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scopes: str = ""


class TokenStore:
    def __init__(
        self, session_factory: sessionmaker[Session], cipher: TokenCipher, clock: Clock = utc_now
    ) -> None:
        self._sessions = session_factory
        self._cipher = cipher
        self._clock = clock

    def save(self, provider: str, tokens: TokenSet) -> None:
        with self._sessions.begin() as s:
            row = s.get(OAuthTokenRow, provider) or OAuthTokenRow(provider=provider)
            row.access_token_enc = self._cipher.encrypt(tokens.access_token)
            row.refresh_token_enc = (
                self._cipher.encrypt(tokens.refresh_token) if tokens.refresh_token else None
            )
            row.expires_at = tokens.expires_at
            row.scopes = tokens.scopes
            row.updated_at = self._clock()
            s.add(row)

    def load(self, provider: str) -> TokenSet | None:
        with self._sessions() as s:
            row = s.get(OAuthTokenRow, provider)
            if row is None:
                return None
            return TokenSet(
                access_token=self._cipher.decrypt(row.access_token_enc),
                refresh_token=(
                    self._cipher.decrypt(row.refresh_token_enc) if row.refresh_token_enc else None
                ),
                expires_at=row.expires_at,
                scopes=row.scopes,
            )

    def delete(self, provider: str) -> None:
        with self._sessions.begin() as s:
            s.execute(delete(OAuthTokenRow).where(OAuthTokenRow.provider == provider))


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class OAuthManager:
    def __init__(
        self,
        providers: dict[str, OAuthProvider],
        store: TokenStore,
        session_factory: sessionmaker[Session],
        public_base_url: str,
        *,
        clock: Clock = utc_now,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._providers = providers
        self._store = store
        self._sessions = session_factory
        self._base_url = public_base_url.rstrip("/")
        self._clock = clock
        self._http = httpx.Client(timeout=30.0, transport=transport)

    def provider(self, name: str) -> OAuthProvider:
        try:
            p = self._providers[name]
        except KeyError:
            raise IntegrationError("oauth", f"unknown provider {name!r}") from None
        if not p.client_id or not p.client_secret:
            raise IntegrationError("oauth", f"{name} client id/secret are not configured")
        return p

    def redirect_uri(self, name: str) -> str:
        return f"{self._base_url}/oauth/{name}/callback"

    def authorization_url(self, name: str) -> str:
        p = self.provider(name)
        state = secrets.token_urlsafe(32)
        verifier, challenge = _pkce_pair()
        with self._sessions.begin() as s:
            s.add(
                OAuthStateRow(
                    state=state, provider=name, code_verifier=verifier, created_at=self._clock()
                )
            )
        params = {
            "client_id": p.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri(name),
            "scope": " ".join(p.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **p.extra_authorize_params,
        }
        return f"{p.authorize_url}?{urlencode(params)}"

    def handle_callback(self, name: str, code: str, state: str) -> None:
        p = self.provider(name)
        with self._sessions.begin() as s:
            row = s.get(OAuthStateRow, state)
            if row is not None:
                s.delete(row)  # one-time use, valid or not
            if row is None or row.provider != name or self._clock() - row.created_at > STATE_TTL:
                raise IntegrationError("oauth", "invalid or expired state; start again")
            verifier = row.code_verifier
        tokens = self._token_request(
            p,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(name),
                "code_verifier": verifier,
            },
            previous_refresh=None,
        )
        self._store.save(name, tokens)

    def is_connected(self, name: str) -> bool:
        return self._store.load(name) is not None

    def access_token(self, name: str) -> str:
        tokens = self._store.load(name)
        if tokens is None:
            raise AuthExpiredError(name, "account is not connected")
        if tokens.expires_at is not None and tokens.expires_at - REFRESH_MARGIN <= self._clock():
            tokens = self._refresh(name, tokens)
        return tokens.access_token

    def refresh(self, name: str) -> bool:
        """Force a refresh (after a 401). Returns whether a new token was obtained."""
        tokens = self._store.load(name)
        if tokens is None or not tokens.refresh_token:
            return False
        try:
            self._refresh(name, tokens)
        except AuthExpiredError:
            return False
        return True

    def _refresh(self, name: str, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise AuthExpiredError(name, "access token expired and no refresh token; reconnect")
        p = self.provider(name)
        fresh = self._token_request(
            p,
            {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token},
            previous_refresh=tokens.refresh_token,
        )
        self._store.save(name, fresh)
        return fresh

    def _token_request(
        self, p: OAuthProvider, data: dict[str, str], previous_refresh: str | None
    ) -> TokenSet:
        payload = {**data, "client_id": p.client_id, "client_secret": p.client_secret}
        try:
            response = self._http.post(p.token_url, data=payload)
        except httpx.TransportError as exc:
            raise IntegrationError(p.name, f"token endpoint unreachable: {exc}") from exc
        if response.status_code in (400, 401):
            raise AuthExpiredError(p.name, f"token request rejected: {response.text[:200]}")
        if response.status_code >= 400:
            raise IntegrationError(p.name, f"token endpoint HTTP {response.status_code}")
        body = response.json()
        expires_in = body.get("expires_in")
        return TokenSet(
            access_token=body["access_token"],
            # Google omits the refresh token on refresh; keep the old one.
            refresh_token=body.get("refresh_token") or previous_refresh,
            expires_at=self._clock() + timedelta(seconds=int(expires_in)) if expires_in else None,
            scopes=body.get("scope", ""),
        )
