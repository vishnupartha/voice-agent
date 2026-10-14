"""Voice activity detection: Silero VAD plus an utterance segmenter.

Silero runs on CPU via torch and costs well under a millisecond per 32 ms
window, so it runs inline on the event loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

SAMPLE_RATE = 16_000
WINDOW = 512  # Silero's required window at 16 kHz (32 ms)
WINDOW_MS = WINDOW * 1000 // SAMPLE_RATE


def load_vad_model() -> Any:
    import torch
    from silero_vad import load_silero_vad

    torch.set_num_threads(1)
    return load_silero_vad()


@dataclass
class VADEvent:
    kind: Literal["start", "end"]
    audio: np.ndarray | None = None  # the whole utterance, on "end"


class Segmenter:
    """Splits a PCM stream into utterances with hysteresis.

    Speech starts after `min_speech_ms` of windows above `start_threshold` and
    ends after `min_silence_ms` below `end_threshold`. The utterance keeps
    `pre_roll_ms` of audio from before the start so first syllables survive.
    """

    def __init__(
        self,
        model: Any,
        *,
        start_threshold: float = 0.5,
        end_threshold: float = 0.35,
        min_speech_ms: int = 200,
        min_silence_ms: int = 700,
        pre_roll_ms: int = 300,
        max_utterance_s: float = 30.0,
    ) -> None:
        self.model = model
        self.start_threshold = start_threshold
        self.end_threshold = end_threshold
        self.min_speech_ms = min_speech_ms
        self.min_silence_ms = min_silence_ms
        self._pre_roll = pre_roll_ms * SAMPLE_RATE // 1000
        self._max_samples = int(max_utterance_s * SAMPLE_RATE)
        self.reset()

    def reset(self) -> None:
        import torch

        self.model.reset_states()
        self._torch = torch
        self._pending = np.zeros(0, dtype=np.float32)
        self._history = np.zeros(0, dtype=np.float32)  # pre-roll ring while idle
        self._utterance: list[np.ndarray] = []
        self._speech_ms = 0
        self._silence_ms = 0
        self.in_speech = False
        self.max_prob = 0.0  # diagnostics: reset by whoever reads it

    @property
    def utterance_audio(self) -> np.ndarray:
        return np.concatenate(self._utterance) if self._utterance else np.zeros(0, dtype=np.float32)

    def feed(self, samples: np.ndarray) -> list[VADEvent]:
        events: list[VADEvent] = []
        self._pending = np.concatenate([self._pending, samples])
        while len(self._pending) >= WINDOW:
            window, self._pending = self._pending[:WINDOW], self._pending[WINDOW:]
            if (event := self._step(window)) is not None:
                events.append(event)
        return events

    def flush(self) -> VADEvent | None:
        """End the current utterance now (push-to-talk release)."""
        if not self.in_speech:
            return None
        return self._end()

    def _step(self, window: np.ndarray) -> VADEvent | None:
        prob = float(self.model(self._torch.from_numpy(window), SAMPLE_RATE).item())
        self.max_prob = max(self.max_prob, prob)
        if not self.in_speech:
            self._history = np.concatenate([self._history, window])[-self._pre_roll - WINDOW * 16 :]
            self._speech_ms = self._speech_ms + WINDOW_MS if prob >= self.start_threshold else 0
            if self._speech_ms >= self.min_speech_ms:
                self.in_speech = True
                self._silence_ms = 0
                speech_samples = self._speech_ms * SAMPLE_RATE // 1000
                self._utterance = [self._history[-(speech_samples + self._pre_roll) :]]
                return VADEvent("start")
            return None

        self._utterance.append(window)
        self._silence_ms = self._silence_ms + WINDOW_MS if prob < self.end_threshold else 0
        if self._silence_ms >= self.min_silence_ms or sum(map(len, self._utterance)) >= self._max_samples:
            return self._end()
        return None

    def _end(self) -> VADEvent:
        audio = self.utterance_audio
        self.in_speech = False
        self._utterance = []
        self._history = np.zeros(0, dtype=np.float32)
        self._speech_ms = 0
        self._silence_ms = 0
        return VADEvent("end", audio)


def pcm16_to_float(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
