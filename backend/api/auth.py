"""Local client authentication.

Binding to 127.0.0.1 keeps other machines out, but any web page open in the
user's browser can still reach localhost. So every request must carry a
random token that only local processes running as this user can read.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket, status

TOKEN_HEADER = "x-agent-token"


def load_or_create_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token


def _valid(expected: str, provided: str | None) -> bool:
    return provided is not None and secrets.compare_digest(expected, provided)


async def require_token(request: Request) -> None:
    if not _valid(request.app.state.token, request.headers.get(TOKEN_HEADER)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid agent token")


def websocket_authorized(ws: WebSocket) -> bool:
    return _valid(ws.app.state.token, ws.headers.get(TOKEN_HEADER))
