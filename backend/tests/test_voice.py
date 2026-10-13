from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import numpy as np
import orjson
import pytest
from fastapi.testclient import TestClient

from backend.agent.agent import Agent
from backend.api.auth import TOKEN_HEADER
from backend.api.server import app
from backend.config import Settings
from backend.tests.test_chat import FakeLLM
from backend.voice.tts import SpeechOutput, speakable
from backend.voice.vad import SAMPLE_RATE, Segmenter, float_to_pcm16


class _Prob:
    def __init__(self, p: float) -> None:
        self.p = p

    def item(self) -> float:
        return self.p


class LoudnessVAD:
    """Stands in for Silero: loud windows are speech."""

    def __call__(self, x, sr) -> _Prob:
        return _Prob(1.0 if float(x.abs().mean()) > 0.1 else 0.0)

    def reset_states(self) -> None:
        pass


class FakeSTT:
    name = "fake-stt"
    loaded = True

    def __init__(self, text: str = "hello there") -> None:
        self.text = text
        self.calls: list[int] = []

    async def load(self) -> None:
        pass

    async def transcribe(self, audio: np.ndarray) -> str:
        self.calls.append(len(audio))
        return self.text


class FakeTTS:
    name = "fake-tts"
    loaded = True
    sample_rate = 24_000

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def load(self) -> None:
        pass

    async def synthesize(self, text: str) -> AsyncIterator[np.ndarray]:
        self.spoken.append(text)
        for _ in range(3):
            yield np.full(2400, 0.1, dtype=np.float32)


def speech(seconds: float) -> bytes:
    return float_to_pcm16(np.full(int(seconds * SAMPLE_RATE), 0.5, dtype=np.float32))


def silence(seconds: float) -> bytes:
    return float_to_pcm16(np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32))


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    settings = Settings(data_dir=tmp_path, model_dir=tmp_path / "models", log_dir=tmp_path / "logs")
    monkeypatch.setattr("backend.api.server.get_settings", lambda: settings)
    with TestClient(app) as c:
        services = app.state.services
        services.stt = FakeSTT()
        services.tts = FakeTTS()
        services.vad_factory = LoudnessVAD
        yield c


def send(ws, **msg) -> None:
    ws.send_text(orjson.dumps(msg).decode())


def recv_until(ws, type_: str) -> tuple[list[dict], int]:
    """Collect JSON events until `type_`; also count binary audio frames."""
    events, frames = [], 0
    while True:
        msg = ws.receive()
        if msg.get("bytes") is not None:
            frames += 1
            continue
        e = orjson.loads(msg["text"])
        events.append(e)
        if e["type"] in (type_, "error"):
            return events, frames


def open_voice(client: TestClient):
    ws = client.websocket_connect("/ws/voice", headers={TOKEN_HEADER: app.state.token})
    return ws


def test_segmenter_finds_one_utterance_with_pre_roll() -> None:
    seg = Segmenter(LoudnessVAD())
    pcm = np.concatenate([np.zeros(SAMPLE_RATE), np.full(SAMPLE_RATE, 0.5), np.zeros(SAMPLE_RATE)]).astype(
        np.float32
    )
    events = seg.feed(pcm)
    assert [e.kind for e in events] == ["start", "end"]
    audio = events[1].audio
    assert audio is not None
    # 1 s of speech + 300 ms pre-roll + 700 ms trailing silence, to window precision
    assert 1.9 * SAMPLE_RATE < len(audio) < 2.1 * SAMPLE_RATE


def test_speakable_strips_markup() -> None:
    assert speakable("**Hi** see [docs](https://x.y) or https://a.b/c") == "Hi see docs or the link"


async def test_speech_output_splits_and_batches_sentences() -> None:
    import threading

    tts = FakeTTS()
    sent: list[bytes] = []
    events: list[str] = []

    async def emit(type, **data) -> None:
        events.append(type)

    async def send_audio(b: bytes) -> None:
        sent.append(b)

    # Sentences that queue up before the TTS is free are synthesized together.
    out = SpeechOutput(tts, send_audio, emit, threading.Event())
    for d in ["Sure thing. The meeting", " is at 3 PM tomorrow! And", " that's all"]:
        out.feed(d)
    await out.finish()
    await out.close()
    assert tts.spoken == ["Sure thing. The meeting is at three P M tomorrow! And that's all"]
    assert events[-2:] == ["assistant.audio.start", "assistant.audio.end"]


async def test_first_sentence_is_not_held_for_batching() -> None:
    import threading

    tts = FakeTTS()

    async def emit(type, **data) -> None:
        pass

    async def send_audio(b: bytes) -> None:
        pass

    out = SpeechOutput(tts, send_audio, emit, threading.Event())
    out.feed("Sure thing. The meeting")
    await asyncio.sleep(0.01)
    assert tts.spoken == ["Sure thing."]
    out.feed(" is at noon.")
    await out.finish()
    await out.close()
    assert tts.spoken == ["Sure thing.", "The meeting is at noon."]


def test_voice_turn_end_to_end(client: TestClient) -> None:
    app.state.services.agent = Agent(FakeLLM(["You have ", "two meetings. ", "Both are short."]))
    with open_voice(client) as ws:
        send(ws, type="session.start", session_id="v")
        recv_until(ws, "session.ready")
        send(ws, type="audio.start")
        for chunk in (silence(0.3), speech(1.0), silence(1.0)):
            ws.send_bytes(chunk)

        events, _ = recv_until(ws, "transcript.final")
        types = [e["type"] for e in events]
        assert types[0] == "input.speech_started" and events[0]["interrupted"] is False
        assert "transcript.partial" in types
        assert events[-1]["text"] == "hello there"

        events, frames = recv_until(ws, "assistant.audio.end")
        types = [e["type"] for e in events]
        assert "assistant.text.final" in types and "assistant.audio.start" in types
        start = next(e for e in events if e["type"] == "assistant.audio.start")
        assert start["sample_rate"] == 24_000
        assert events[-1]["interrupted"] is False
        assert frames == 3 * len(app.state.services.tts.spoken)  # three chunks per synthesis
    spoken = app.state.services.tts.spoken
    assert spoken[0] == "You have two meetings."
    assert " ".join(spoken) == "You have two meetings. Both are short."


def test_barge_in_interrupts_reply(client: TestClient) -> None:
    app.state.services.agent = Agent(FakeLLM(["word. "] * 400, delay=0.01))
    with open_voice(client) as ws:
        send(ws, type="session.start", session_id="v")
        recv_until(ws, "session.ready")
        send(ws, type="audio.start")
        ws.send_bytes(speech(0.5) + silence(1.0))
        recv_until(ws, "assistant.audio.start")

        ws.send_bytes(speech(0.6))
        events, _ = recv_until(ws, "input.speech_started")
        assert events[-1]["interrupted"] is True
        more, _ = recv_until(ws, "assistant.audio.end")
        events += more
        assert events[-1]["interrupted"] is True
        final = next(e for e in events if e["type"] == "assistant.text.final")
        assert final["cancelled"] is True


def test_push_to_talk_release_finalizes(client: TestClient) -> None:
    app.state.services.agent = Agent(FakeLLM(["Okay."]))
    with open_voice(client) as ws:
        send(ws, type="session.start", session_id="v")
        recv_until(ws, "session.ready")
        send(ws, type="audio.start")
        ws.send_bytes(speech(0.8))  # no trailing silence
        recv_until(ws, "input.speech_started")
        send(ws, type="audio.stop")
        events, _ = recv_until(ws, "transcript.final")
        assert events[-1]["text"] == "hello there"
