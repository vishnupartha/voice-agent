"""Local LLM provider. MLX work runs on the shared MLXWorker thread.

Prompt caching: the system prompt plus tool list is ~2.5k tokens, and
re-reading it every turn costs seconds. Qwen3.5's hybrid cache (linear
attention + KV) can't be trimmed back to a shared prefix, so instead the cache
state is snapshotted at reusable points while prefilling, and a later prompt
that starts with a snapshot's tokens resumes from it:

- end of the system block (system prompt + tools): reused by every turn
- end of the conversation before the reply begins: reused by the next turn
- end of the whole prompt: reused by the next tool step in the same turn
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

from backend.preferences import Switchable
from backend.voice.mlx_runtime import MLXWorker

log = structlog.get_logger(__name__)

Message = dict[str, Any]


SNAPSHOTS = 6
PREFILL_CHUNK = 2048
GEN_PROMPT = "<|im_start|>assistant\n<think>\n\n</think>\n\n"


@dataclass
class _Snapshot:
    tokens: list[int]
    state: list[Any]


@dataclass
class GenerationStats:
    first_token_s: float | None = None
    total_s: float = 0.0
    tokens: int = 0
    cancelled: bool = False
    prompt_tokens: int = 0
    cached_tokens: int = 0
    # Structured tool calls, from providers that return them outside the text
    # (Gemini): {"name", "arguments", "thought_signature"}.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class LLMProvider(Protocol):
    name: str

    @property
    def loaded(self) -> bool: ...

    async def load(self) -> None: ...

    def stream(
        self,
        messages: list[Message],
        cancel: threading.Event,
        stats: GenerationStats,
        tools: list[dict[str, Any]] | None = None,
        prefix: str = "",
    ) -> AsyncIterator[str]: ...


class MLXLLM:
    def __init__(
        self,
        model_path: Path,
        worker: MLXWorker,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        top_p: float = 0.8,
    ) -> None:
        self.name = model_path.name
        self._path = model_path
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._top_p = top_p
        self._model: Any = None
        self._tokenizer: Any = None
        self._worker = worker
        self._load_lock = asyncio.Lock()
        self._snapshots: list[_Snapshot] = []  # most recently used last

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def available(self) -> bool:
        return (self._path / "config.json").exists()

    async def load(self) -> None:
        async with self._load_lock:
            if self.loaded:
                return
            if not self.available:
                raise FileNotFoundError(f"LLM weights not found at {self._path}")
            t0 = time.perf_counter()
            self._model, self._tokenizer = await self._worker.run(self._load_sync)
            await self._worker.run(self._warm_up)
            log.info("llm.loaded", model=self.name, seconds=round(time.perf_counter() - t0, 2))

    def _load_sync(self) -> tuple[Any, Any]:
        from mlx_lm import load

        return load(str(self._path))

    def _warm_up(self) -> None:
        # The first generation compiles kernels (~1.5 s); pay that at load time.
        from mlx_lm import generate

        generate(
            self._model, self._tokenizer, self._prompt([{"role": "user", "content": "hi"}]), max_tokens=1
        )

    def _prompt(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> str:
        # Thinking mode is disabled: voice needs a fast first token.
        return self._tokenizer.apply_chat_template(
            messages, tools=tools, add_generation_prompt=True, tokenize=False, enable_thinking=False
        )

    # ---- prompt cache (worker thread only) ----

    def _encode(self, text: str) -> list[int]:
        return list(self._tokenizer.encode(text, add_special_tokens=False))

    def _restore(self, tokens: list[int]) -> tuple[list[Any], int]:
        """A cache primed with the longest snapshot that prefixes `tokens`
        (leaving at least one token to generate from)."""
        from mlx_lm.models.cache import make_prompt_cache

        cache = make_prompt_cache(self._model)
        best: _Snapshot | None = None
        for snap in self._snapshots:
            n = len(snap.tokens)
            if n < len(tokens) and (best is None or n > len(best.tokens)) and tokens[:n] == snap.tokens:
                best = snap
        if best is None:
            return cache, 0
        for c, st in zip(cache, best.state, strict=True):
            c.state = list(st) if isinstance(st, list) else st
        self._snapshots.remove(best)
        self._snapshots.append(best)
        return cache, len(best.tokens)

    def _snapshot(self, tokens: list[int], cache: list[Any]) -> None:
        if any(s.tokens == tokens for s in self._snapshots):
            return
        state = [list(c.state) if isinstance(c.state, list) else c.state for c in cache]
        self._snapshots.append(_Snapshot(list(tokens), state))
        del self._snapshots[:-SNAPSHOTS]

    def _prefill(self, tokens: list[int], stats: GenerationStats) -> list[Any]:
        """Prime a cache with all but the last token, snapshotting on the way."""
        import mlx.core as mx

        cache, start = self._restore(tokens)
        stats.prompt_tokens, stats.cached_tokens = len(tokens), start
        # Points worth snapshotting: end of the system block, and the start of
        # the generation prompt (what the next turn's prompt will share).
        im_start = self._encode("<|im_start|>")[0]
        system_end = next((i for i, t in enumerate(tokens) if t == im_start and i > 0), 0)
        gen = self._encode(GEN_PROMPT)
        reply_start = len(tokens) - len(gen) if tokens[-len(gen) :] == gen else 0
        stops = sorted({p for p in (system_end, reply_start, len(tokens) - 1) if p > start})

        pos = start
        for stop in stops:
            while pos < stop:
                end = min(stop, pos + PREFILL_CHUNK)
                self._model(mx.array(tokens[pos:end])[None], cache=cache)
                mx.eval([c.state for c in cache])
                pos = end
            self._snapshot(tokens[:stop], cache)
        return cache

    async def prime(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> None:
        """Prefill and snapshot a prompt ahead of time (the fixed system block),
        so the first real turn starts fast."""
        await self.load()
        await self._worker.run(
            lambda: self._prefill(self._encode(self._prompt(messages, tools)), GenerationStats())
        )

    async def stream(
        self,
        messages: list[Message],
        cancel: threading.Event,
        stats: GenerationStats,
        tools: list[dict[str, Any]] | None = None,
        prefix: str = "",
    ) -> AsyncIterator[str]:
        """Stream the reply. `prefix` is text the reply is forced to start with
        (it is not yielded)."""
        await self.load()

        def make() -> Any:
            import mlx.core as mx
            from mlx_lm import stream_generate
            from mlx_lm.sample_utils import make_sampler

            tokens = self._encode(self._prompt(messages, tools) + prefix)
            cache = self._prefill(tokens, stats)
            return stream_generate(
                self._model,
                self._tokenizer,
                mx.array(tokens[-1:]),
                prompt_cache=cache,
                max_tokens=self._max_tokens,
                sampler=make_sampler(temp=self._temperature, top_p=self._top_p),
            )

        t0 = time.perf_counter()
        try:
            async with aclosing(self._worker.iterate(make)) as responses:
                async for resp in responses:
                    if cancel.is_set():
                        stats.cancelled = True
                        break
                    if stats.first_token_s is None:
                        stats.first_token_s = time.perf_counter() - t0
                    stats.tokens += 1
                    if resp.text:
                        yield resp.text
        finally:
            stats.total_s = time.perf_counter() - t0


class SwitchableLLM(Switchable[LLMProvider]):
    """The active brain (cloud Gemini or the local model), chosen in the app
    and remembered across restarts."""

    def __init__(self, engines: dict[str, LLMProvider], preferences: Path, default: str) -> None:
        super().__init__(engines, preferences, "brain", default)

    @property
    def name(self) -> str:
        return self.current.name

    @property
    def native_tools(self) -> bool:
        return bool(getattr(self.current, "native_tools", False))

    @property
    def loaded(self) -> bool:
        return self.current.loaded

    @property
    def available(self) -> bool:
        return bool(getattr(self.current, "available", True))

    async def load(self) -> None:
        await self.current.load()

    async def prime(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> None:
        if prime := getattr(self.current, "prime", None):
            await prime(messages, tools)
        else:
            await self.current.load()

    def stream(
        self,
        messages: list[Message],
        cancel: threading.Event,
        stats: GenerationStats,
        tools: list[dict[str, Any]] | None = None,
        prefix: str = "",
    ) -> AsyncIterator[str]:
        return self.current.stream(messages, cancel, stats, tools=tools, prefix=prefix)
