"""Choices made in the app (voice engine, brain) that persist across restarts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_preferences(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return {}


class Switchable[T]:
    """One of several interchangeable engines, chosen in the app and saved
    under `key` in the preferences file."""

    def __init__(self, engines: dict[str, T], preferences: Path, key: str, default: str) -> None:
        self.engines = engines
        self._prefs = preferences
        self._key = key
        saved = self._read().get(key)
        self.engine = saved if saved in engines else default

    def _read(self) -> dict[str, Any]:
        return read_preferences(self._prefs)

    def select(self, engine: str) -> None:
        if engine not in self.engines:
            raise ValueError(f"unknown engine {engine!r}")
        self.engine = engine
        self._prefs.write_text(json.dumps({**self._read(), self._key: engine}))

    @property
    def current(self) -> T:
        return self.engines[self.engine]
