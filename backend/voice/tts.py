"""Local speech synthesis through MLX Audio: Kokoro (default) or Qwen3-TTS.

`SpeechOutput` turns the agent's streamed text into streamed audio: it cuts the
text at sentence boundaries and synthesizes each sentence as soon as it is
complete, so speech starts before the reply is finished.
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import aclosing
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import structlog

from backend.preferences import Switchable
from backend.voice.mlx_runtime import MLXWorker
from backend.voice.normalize import spoken_form
from backend.voice.vad import float_to_pcm16

log = structlog.get_logger(__name__)


class TTSProvider(Protocol):
    name: str
    sample_rate: int

    @property
    def loaded(self) -> bool: ...

    async def load(self) -> None: ...

    def synthesize(self, text: str) -> AsyncIterator[np.ndarray]: ...


class QwenTTS:
    def __init__(self, model_path: Path, worker: MLXWorker, voice: str) -> None:
        self.name = model_path.name
        self.voice = voice
        self.sample_rate = 24_000
        self._path = model_path
        self._worker = worker
        self._model: Any = None
        self._load_lock = asyncio.Lock()

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
                raise FileNotFoundError(f"TTS weights not found at {self._path}")
            t0 = time.perf_counter()
            self._model = await self._worker.run(self._load_sync)
            self.sample_rate = int(self._model.sample_rate)
            # The first synthesis compiles kernels (~3 s); pay that now, not mid-reply.
            await self._worker.run(lambda: list(self._model.generate(text="Ready.", voice=self.voice)))
            log.info("tts.loaded", model=self.name, seconds=round(time.perf_counter() - t0, 2))

    def _load_sync(self) -> Any:
        from mlx_audio.tts.utils import load_model

        return load_model(self._path)

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        await self.load()

        def make() -> Any:
            return self._model.generate(
                text=text, voice=self.voice, lang_code="english", stream=True, streaming_interval=0.32,
                # The library's defaults (temperature 0.9, top_p 1.0, 4096 tokens) occasionally
                # sample stray vocalizations like a shout; sample more conservatively and cap the
                # length so a bad sample can't run on. 12 codec tokens per second of speech is
                # about one per character, so twice the text length leaves ample room.
                temperature=0.5, top_p=0.9, max_tokens=48 + 2 * len(text),
            )  # fmt: skip

        async with aclosing(self._worker.iterate(make)) as chunks:
            async for result in chunks:
                yield np.asarray(result.audio, dtype=np.float32)


class KokoroTTS:
    """Kokoro-82M: small and deterministic (no sampling), so it never ad-libs
    laughs or drawn-out words the way an autoregressive TTS can."""

    def __init__(self, model_path: Path, worker: MLXWorker, voice: str) -> None:
        self.name = model_path.name
        self.voice = voice
        self.sample_rate = 24_000
        self._path = model_path
        self._worker = worker
        self._model: Any = None
        self._load_lock = asyncio.Lock()

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
                raise FileNotFoundError(f"TTS weights not found at {self._path}")
            t0 = time.perf_counter()
            self._model = await self._worker.run(self._load_sync)
            self.sample_rate = int(self._model.sample_rate)
            # Builds the text pipeline and loads the voice now, not mid-reply.
            await self._worker.run(lambda: list(self._generate("Ready.")))
            log.info("tts.loaded", model=self.name, seconds=round(time.perf_counter() - t0, 2))

    def _load_sync(self) -> Any:
        from mlx_audio.tts.utils import load_model

        return load_model(self._path)

    def _generate(self, text: str) -> Iterator[np.ndarray]:
        # Kokoro's audio is a lazy MLX array; convert it here, on the worker
        # thread, since MLX can't evaluate it from another thread.
        for result in self._model.generate(text=text, voice=self.voice, speed=1.0, lang_code="a"):
            yield np.asarray(result.audio, dtype=np.float32).reshape(-1)

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        await self.load()
        async with aclosing(self._worker.iterate(lambda: self._generate(text))) as chunks:
            async for chunk in chunks:
                yield chunk


class SwitchableTTS(Switchable[TTSProvider]):
    """The active voice engine, chosen in the app and remembered across restarts."""

    def __init__(self, engines: dict[str, TTSProvider], preferences: Path, default: str) -> None:
        super().__init__(engines, preferences, "voice_engine", default)

    @property
    def name(self) -> str:
        return self.current.name

    @property
    def sample_rate(self) -> int:
        return self.current.sample_rate

    @property
    def loaded(self) -> bool:
        return self.current.loaded

    @property
    def available(self) -> bool:
        return bool(getattr(self.current, "available", True))

    async def load(self) -> None:
        await self.current.load()

    def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        return self.current.synthesize(text)


# ---- streaming text -> speech ----

_SENTENCE_END = re.compile(r"(?<=[.!?…:;])[\"')\]]*\s+|\n+")
_MIN_CHARS = 10  # don't synthesize fragments like "Dr." on their own
_MAX_BATCH_CHARS = 400
_MARKDOWN = re.compile(r"[*_#`>|]+")
_URL = re.compile(r"https?://\S+")


def speakable(text: str) -> str:
    """Strip markup that shouldn't be read aloud."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](url) -> label
    text = _URL.sub("the link", text)
    text = _MARKDOWN.sub("", text)
    text = spoken_form(text)
    return re.sub(r"\s+", " ", text).strip()


SendAudio = Callable[[bytes], Awaitable[None]]
Emit = Callable[..., Awaitable[None]]


class SpeechOutput:
    """Speaks one assistant turn. Feed it text deltas, then `finish()`."""

    def __init__(self, tts: TTSProvider, send_audio: SendAudio, emit: Emit, cancel: threading.Event) -> None:
        self._tts = tts
        self._send_audio = send_audio
        self._emit = emit
        self._cancel = cancel
        self._buffer = ""
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._speak())
        self._started = False
        self._ended = False
        self.first_audio_s: float | None = None
        self.seconds_sent = 0.0
        self._t0 = time.perf_counter()

    def feed(self, delta: str) -> None:
        self._buffer += delta
        while True:
            cut = next(
                (m.end() for m in _SENTENCE_END.finditer(self._buffer) if m.start() >= _MIN_CHARS), None
            )
            if cut is None:
                return
            self._enqueue(self._buffer[:cut])
            self._buffer = self._buffer[cut:]

    async def finish(self) -> None:
        self._enqueue(self._buffer)
        self._buffer = ""
        self._queue.put_nowait(None)
        await self._task

    async def close(self) -> None:
        """Stop speaking now if still going (barge-in or error). Always ends
        the turn's audio with `assistant.audio.end`, even if nothing was said,
        so the client knows to go back to listening."""
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass  # a synthesis error was already raised through finish()
        if not self._ended:
            self._ended = True
            await self._emit("assistant.audio.end", interrupted=True)

    def _enqueue(self, text: str) -> None:
        if text := speakable(text):
            self._queue.put_nowait(text)

    def _take_batch(self, first: str) -> tuple[str, bool]:
        """Join `first` with whatever else is already queued, so the TTS speaks
        flowing text instead of restarting intonation every sentence. The very
        first sentence still goes alone, because nothing else is queued yet."""
        parts, done = [first], False
        while not self._queue.empty() and sum(map(len, parts)) < _MAX_BATCH_CHARS:
            item = self._queue.get_nowait()
            if item is None:
                done = True
                break
            parts.append(item)
        return " ".join(parts), done

    async def _speak(self) -> None:
        done = False
        while not done and (sentence := await self._queue.get()) is not None:
            text, done = self._take_batch(sentence)
            async with aclosing(self._tts.synthesize(text)) as chunks:
                async for chunk in chunks:
                    if self._cancel.is_set():
                        return
                    if not self._started:
                        self._started = True
                        self.first_audio_s = time.perf_counter() - self._t0
                        await self._emit(
                            "assistant.audio.start", sample_rate=self._tts.sample_rate, format="pcm_s16le"
                        )
                    await self._send_audio(float_to_pcm16(chunk))
                    self.seconds_sent += len(chunk) / self._tts.sample_rate
        self._ended = True
        await self._emit("assistant.audio.end", interrupted=False)
