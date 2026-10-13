"""One Google OAuth connection for Gmail + Calendar (spec §21–22).

The OAuth client (a "Desktop app" client from the user's own Google Cloud
project) and the resulting tokens live in the macOS Keychain via `keyring`.
Neither ever reaches the model or the logs.

Flow: POST /auth/google/start opens the system browser at Google's consent
page; Google redirects to http://127.0.0.1:8765/auth/google/callback, where the
code is exchanged for tokens.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import webbrowser
from pathlib import Path
from typing import Any

import keyring
import structlog
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

log = structlog.get_logger(__name__)

# Google may return the granted scopes in a different form or order (or add
# previously granted ones); don't treat that as an error.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

KEYCHAIN_SERVICE = "PersonalAgent"
CLIENT_KEY = "google_oauth_client"
TOKEN_KEY = "google_token"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

PENDING_TTL_S = 600


class GoogleNotConnected(Exception):
    pass


class GoogleAuth:
    def __init__(self, redirect_uri: str, profile_path: Path | None = None) -> None:
        self.redirect_uri = redirect_uri
        # Name and email of the connected account. Not secret, so a plain file.
        self.profile_path = profile_path
        self._pending: dict[str, tuple[Flow, float]] = {}
        self._creds: Credentials | None = None
        self._lock = asyncio.Lock()

    # ---- client config ----

    @staticmethod
    def save_client_config(raw: str) -> None:
        """Store the client_secret JSON downloaded from Google Cloud."""
        config = json.loads(raw)
        if "installed" not in config and "web" not in config:
            raise ValueError("not an OAuth client file (expected an 'installed' or 'web' key)")
        keyring.set_password(KEYCHAIN_SERVICE, CLIENT_KEY, json.dumps(config))

    @staticmethod
    def _client_config() -> dict[str, Any] | None:
        raw = keyring.get_password(KEYCHAIN_SERVICE, CLIENT_KEY)
        return json.loads(raw) if raw else None

    @property
    def configured(self) -> bool:
        return self._client_config() is not None

    # ---- connect ----

    def start(self, open_browser: bool = True) -> str:
        config = self._client_config()
        if config is None:
            raise GoogleNotConnected(
                "Google isn't set up: run `uv run python -m scripts.google_setup <client_secret.json>` first"
            )
        flow = Flow.from_client_config(config, scopes=SCOPES, redirect_uri=self.redirect_uri)
        url, state = flow.authorization_url(
            access_type="offline", prompt="consent", include_granted_scopes="true"
        )
        now = time.monotonic()
        self._pending = {s: v for s, v in self._pending.items() if now - v[1] < PENDING_TTL_S}
        self._pending[state] = (flow, now)
        if open_browser:
            webbrowser.open(url)
        return url

    async def finish(self, state: str, code: str) -> str:
        """Exchange the code; returns the connected account's email."""
        entry = self._pending.pop(state, None)
        if entry is None:
            raise GoogleNotConnected("this sign-in link expired; start again from the app")
        flow, _ = entry
        await asyncio.to_thread(flow.fetch_token, code=code)
        creds = flow.credentials
        self._save(creds)
        info = await asyncio.to_thread(self._userinfo, creds)
        if self.profile_path is not None:
            self.profile_path.write_text(
                json.dumps({"name": info.get("name", ""), "email": info.get("email", "")})
            )
        log.info("google.connected")
        return str(info.get("email", ""))

    def profile(self) -> dict[str, str]:
        if self.profile_path is None or not self.profile_path.exists():
            return {}
        try:
            return dict(json.loads(self.profile_path.read_text()))
        except (OSError, ValueError):
            return {}

    def disconnect(self) -> None:
        self._creds = None
        if self.profile_path is not None:
            self.profile_path.unlink(missing_ok=True)
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, TOKEN_KEY)
        except keyring.errors.PasswordDeleteError:
            pass

    # ---- use ----

    @property
    def connected(self) -> bool:
        return keyring.get_password(KEYCHAIN_SERVICE, TOKEN_KEY) is not None

    async def credentials(self) -> Credentials:
        async with self._lock:
            if self._creds is None:
                raw = keyring.get_password(KEYCHAIN_SERVICE, TOKEN_KEY)
                if raw is None:
                    raise GoogleNotConnected(
                        "Google isn't connected. Connect it from the menu bar: Connect Google."
                    )
                self._creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
            if not self._creds.valid:
                if not self._creds.refresh_token:
                    raise GoogleNotConnected("Google sign-in expired. Reconnect from the menu bar.")
                try:
                    await asyncio.to_thread(self._creds.refresh, Request())
                except Exception as e:
                    raise GoogleNotConnected(
                        f"Google sign-in couldn't be refreshed ({type(e).__name__})."
                    ) from e
                self._save(self._creds)
            return self._creds

    def _save(self, creds: Credentials) -> None:
        self._creds = creds
        keyring.set_password(KEYCHAIN_SERVICE, TOKEN_KEY, creds.to_json())

    @staticmethod
    def _userinfo(creds: Credentials) -> dict[str, Any]:
        from googleapiclient.discovery import build

        return dict(
            build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
        )
