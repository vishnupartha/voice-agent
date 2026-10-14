"""Gemini as the agent's brain, through the Gemini API (generativelanguage).

Unlike the local model, Gemini returns tool calls as structured parts rather
than markup in the text, so they're reported through `GenerationStats.tool_calls`
and the agent runs them directly. Gemini attaches a "thought signature" to each
call that must be sent back with it on the next request; it rides along in the
conversation history as the call's `thought_signature`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import keyring
import orjson
import structlog

from backend.integrations.google_auth import KEYCHAIN_SERVICE
from backend.voice.llm import GenerationStats, Message

log = structlog.get_logger(__name__)

GEMINI_KEY = "gemini_api_key"
# How long to wait for the main model to start answering before falling back.
FIRST_BYTE_S = 6.0
# After the main model fails, go straight to the fallback for this long.
BUSY_COOLDOWN_S = 300.0
API = "https://generativelanguage.googleapis.com/v1beta/models"
# Schema keywords Gemini's function declarations accept (an OpenAPI subset).
_SCHEMA_KEYS = {
    "type", "format", "description", "nullable", "enum", "items", "properties", "required",
    "minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength", "pattern", "default",
}  # fmt: skip


class _Busy(Exception):
    """The model is overloaded or slow to respond; another may do."""


def api_key() -> str | None:
    return keyring.get_password(KEYCHAIN_SERVICE, GEMINI_KEY)


def _schema(s: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in s.items() if k in _SCHEMA_KEYS}
    if "properties" in out:
        out["properties"] = {k: _schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _schema(out["items"])
    return out


def _declarations(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decls = []
    for t in tools:
        fn = t["function"]
        decl = {"name": fn["name"], "description": fn["description"]}
        if fn["parameters"].get("properties"):  # Gemini rejects an empty object schema
            decl["parameters"] = _schema(fn["parameters"])
        decls.append(decl)
    return decls


def _contents(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Split off the system prompt and convert the rest to Gemini's format.
    Tool results carry no name, so they're paired with the preceding calls in order."""
    system, contents = "", []
    pending: list[str] = []  # names of calls still waiting for a result
    for m in messages:
        role, text = m["role"], m.get("content") or ""
        if role == "system":
            system = text
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": text}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = [{"text": text}] if text else []
            pending = []
            for call in m.get("tool_calls", []):
                fn = call["function"]
                part: dict[str, Any] = {"functionCall": {"name": fn["name"], "args": fn["arguments"]}}
                if sig := call.get("thought_signature"):
                    part["thoughtSignature"] = sig
                parts.append(part)
                pending.append(fn["name"])
            if parts:
                contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            name = pending.pop(0) if pending else "tool"
            part = {"functionResponse": {"name": name, "response": {"content": text}}}
            # Results for one batch of calls go back together in a single turn.
            if contents and contents[-1]["role"] == "user" and "functionResponse" in contents[-1]["parts"][0]:
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    return system, contents


class GeminiLLM:
    native_tools = True

    def __init__(
        self,
        model: str,
        fallback: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        thinking: str = "low",
    ) -> None:
        self.name = model
        self._models = [model, *([fallback] if fallback else [])]
        self._busy_until: dict[str, float] = {}
        self._thinking = thinking
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = httpx.AsyncClient()

    @property
    def loaded(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return api_key() is not None

    async def load(self) -> None:
        if not api_key():
            raise RuntimeError("No Gemini API key. Run: pbpaste | uv run python -m scripts.gemini_setup")

    async def stream(
        self,
        messages: list[Message],
        cancel: threading.Event,
        stats: GenerationStats,
        tools: list[dict[str, Any]] | None = None,
        prefix: str = "",
    ) -> AsyncIterator[str]:
        key = api_key()
        if not key:
            raise RuntimeError("No Gemini API key. Run: pbpaste | uv run python -m scripts.gemini_setup")
        system, contents = _contents(messages)
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self._temperature,
                "maxOutputTokens": self._max_tokens,
                # Voice needs a fast first word, so think as little as the model allows.
                "thinkingConfig": {"thinkingLevel": self._thinking},
            },
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            body["tools"] = [{"functionDeclarations": _declarations(tools)}]

        t0 = time.perf_counter()
        try:
            now = time.monotonic()
            models = [m for m in self._models[:-1] if self._busy_until.get(m, 0) <= now] + self._models[-1:]
            for i, model in enumerate(models):
                last = i == len(models) - 1
                started = False
                try:
                    async for text in self._attempt(model, body, key, cancel, stats, t0, last):
                        started = True
                        yield text
                    self.name = model
                    return
                except _Busy as e:
                    # Free-tier Flash is often overloaded; fall back rather than
                    # leave the user in silence, unless the reply already began.
                    if started or last:
                        raise RuntimeError(f"Gemini is busy right now ({e}); try again in a moment.") from e
                    self._busy_until[model] = time.monotonic() + BUSY_COOLDOWN_S
                    log.warning("gemini.fallback", model=model, reason=str(e), to=models[i + 1])
        finally:
            stats.total_s = time.perf_counter() - t0

    async def _attempt(
        self,
        model: str,
        body: dict[str, Any],
        key: str,
        cancel: threading.Event,
        stats: GenerationStats,
        t0: float,
        last: bool,
    ) -> AsyncIterator[str]:
        url = f"{API}/{model}:streamGenerateContent?alt=sse"
        # Waiting longer than this for a model that may fall back wastes the user's time.
        timeout = httpx.Timeout(60 if last else FIRST_BYTE_S, connect=5)
        try:
            async with self._client.stream(
                "POST", url, json=body, headers={"x-goog-api-key": key}, timeout=timeout
            ) as r:
                if r.status_code in (429, 500, 503, 504):
                    raise _Busy(f"{model} returned {r.status_code}")
                if r.status_code != 200:
                    detail = (await r.aread()).decode(errors="replace")[:500]
                    log.error("gemini.error", model=model, status=r.status_code, detail=detail)
                    raise RuntimeError(f"Gemini returned {r.status_code}: {detail}")
                async for line in r.aiter_lines():
                    if cancel.is_set():
                        stats.cancelled = True
                        break
                    if not line.startswith("data:"):
                        continue
                    chunk = orjson.loads(line[5:])
                    if usage := chunk.get("usageMetadata"):
                        stats.prompt_tokens = usage.get("promptTokenCount", 0)
                        stats.cached_tokens = usage.get("cachedContentTokenCount", 0)
                        stats.tokens = usage.get("candidatesTokenCount", stats.tokens)
                    for cand in chunk.get("candidates", [])[:1]:
                        for part in cand.get("content", {}).get("parts", []):
                            if stats.first_token_s is None:
                                stats.first_token_s = time.perf_counter() - t0
                            if call := part.get("functionCall"):
                                stats.tool_calls.append(
                                    {
                                        "name": call["name"],
                                        "arguments": call.get("args", {}),
                                        "thought_signature": part.get("thoughtSignature"),
                                    }
                                )
                            elif (text := part.get("text")) and not part.get("thought"):
                                yield text
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            raise _Busy(f"{model}: {type(e).__name__}") from e
