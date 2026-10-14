"""Real-model smoke test: TTS -> STT round trip and LLM latency.

uv run python -m scripts.smoke_voice
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np

from backend.config import get_settings
from backend.voice.llm import MLXLLM, GenerationStats
from backend.voice.mlx_runtime import MLXWorker
from backend.voice.stt import ParakeetSTT
from backend.voice.tts import QwenTTS


async def main() -> None:
    s = get_settings()
    worker = MLXWorker()
    stt = ParakeetSTT(s.stt_model_path, worker)
    llm = MLXLLM(s.llm_model_path, worker, max_tokens=120)
    tts = QwenTTS(s.tts_model_path, worker, s.tts_voice)

    for name, m in (("stt", stt), ("llm", llm), ("tts", tts)):
        t0 = time.perf_counter()
        await m.load()
        print(f"load {name}: {time.perf_counter() - t0:.1f}s")

    text = "What's happening around campus this week?"
    for attempt in range(2):  # second pass shows warm latency
        t0 = time.perf_counter()
        first = None
        chunks = []
        async for c in tts.synthesize(text):
            first = first or time.perf_counter() - t0
            chunks.append(c)
        audio = np.concatenate(chunks)
        total = time.perf_counter() - t0
        dur = len(audio) / tts.sample_rate
        print(f"tts[{attempt}]: first chunk {first * 1000:.0f} ms, {dur:.1f}s audio in {total:.2f}s")

    # Resample 24k -> 16k for STT.
    idx = np.arange(0, len(audio), tts.sample_rate / 16_000)
    audio16 = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
    for attempt in range(2):
        t0 = time.perf_counter()
        heard = await stt.transcribe(audio16)
        print(f"stt[{attempt}]: {(time.perf_counter() - t0) * 1000:.0f} ms -> {heard!r}")

    for attempt in range(2):
        stats = GenerationStats()
        parts = []
        msgs = [{"role": "system", "content": "Be concise."}, {"role": "user", "content": heard}]
        async for d in llm.stream(msgs, threading.Event(), stats):
            parts.append(d)
        tps = stats.tokens / stats.total_s
        print(
            f"llm[{attempt}]: first token {stats.first_token_s * 1000:.0f} ms, "
            f"{stats.tokens} tok, {tps:.0f} tok/s"
        )
    print("reply:", "".join(parts))
    worker.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
