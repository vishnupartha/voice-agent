"""Local speech recognition: Parakeet TDT through MLX (spec §5).

Each utterance is short (VAD caps it at 30 s), so partials simply re-transcribe
the audio so far; Parakeet handles 10 s of audio in well under 100 ms on
Apple Silicon, and a full pass is more accurate than chunked streaming.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import structlog

from backend.voice.mlx_runtime import MLXWorker

log = structlog.get_logger(__name__)


class STTProvider(Protocol):
    name: str

    @property
    def loaded(self) -> bool: ...

    async def load(self) -> None: ...

    async def transcribe(self, audio: np.ndarray) -> str: ...


class ParakeetSTT:
    def __init__(self, model_path: Path, worker: MLXWorker) -> None:
        self.name = model_path.name
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
                raise FileNotFoundError(f"STT weights not found at {self._path}")
            t0 = time.perf_counter()
            self._model = await self._worker.run(self._load_sync)
            log.info("stt.loaded", model=self.name, seconds=round(time.perf_counter() - t0, 2))

    def _load_sync(self) -> Any:
        from parakeet_mlx import from_pretrained

        return from_pretrained(str(self._path))

    async def transcribe(self, audio: np.ndarray) -> str:
        await self.load()
        if len(audio) < 1600:  # < 0.1 s: nothing to hear
            return ""
        return await self._worker.run(self._transcribe_sync, audio)

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        # get_logmel's complex->real view only works on float32 input.
        mel = get_logmel(mx.array(audio, dtype=mx.float32), self._model.preprocessor_config)
        return str(self._model.generate(mel)[0].text).strip()
