from __future__ import annotations

import asyncio
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import orjson
import structlog
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.agent.agent import Agent
from backend.agent.sessions import Conversation, ConversationStore
from backend.agent.tools import ApprovalRequest, ToolError
from backend.api.auth import websocket_authorized
from backend.api.schemas import (
    ApprovalResponse,
    AudioStart,
    AudioStop,
    BridgeResponse,
    ConversationCancel,
    EventType,
    SessionStart,
    UserMessage,
    client_message_adapter,
    event,
)
from backend.voice.stt import STTProvider
from backend.voice.tts import SpeechOutput, TTSProvider
from backend.voice.vad import SAMPLE_RATE, Segmenter, load_vad_model, pcm16_to_float

log = structlog.get_logger(__name__)

PARTIAL_EVERY_S = 0.5
# While the assistant talks, require more speech before treating it as barge-in,
# so residual echo the client's AEC misses doesn't cut the reply off.
BARGE_IN_MIN_SPEECH_MS = 400
NORMAL_MIN_SPEECH_MS = 200
APPROVAL_TIMEOUT_S = 120
# Long enough for a first-use permission prompt (e.g. Reminders access) to be answered.
BRIDGE_TIMEOUT_S = 60

# Spoken answers to a pending approval (spec §19): "Send it." / "No, wait."
_YES = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok(ay)?|confirm(ed)?|approve(d)?|go ahead|do it|send it|send|"
    r"add it|delete it|move it|change it|sounds good|that's fine|please do)\b"
)
_NO = re.compile(r"^(no|nope|nah|cancel|don't|do not|stop|wait|hold on|not yet|never ?mind)\b")


def spoken_decision(text: str) -> bool | None:
    """True/False if a short utterance answers yes/no; None if it's something else."""
    words = re.sub(r"[^\w' ]", " ", text.lower()).split()
    if not words or len(words) > 6:
        return None
    phrase = " ".join(words)
    if _NO.match(phrase):
        return False
    if _YES.match(phrase):
        return True
    return None


@dataclass
class Services:
    agent: Agent
    conversations: ConversationStore
    stt: STTProvider
    tts: TTSProvider
    vad_factory: Callable[[], Any] = load_vad_model


class AgentSession:
    """One WebSocket connection (spec §11–13).

    Text frames carry JSON control messages; binary frames carry 16 kHz mono
    PCM16 microphone audio, and the backend answers voice turns with binary
    PCM16 at the TTS sample rate. At most one agent turn runs at a time; a new
    user message, detected speech, or a cancel interrupts it.
    """

    def __init__(self, ws: WebSocket, services: Services) -> None:
        self.ws = ws
        self.s = services
        self.session_id = ""
        self.conversation: Conversation | None = None
        self._turn: asyncio.Task[None] | None = None
        self._cancel = threading.Event()
        self._send_lock = asyncio.Lock()
        self._closed = False
        # voice input
        self._listening = False
        self._segmenter: Segmenter | None = None
        self._partial_task: asyncio.Task[None] | None = None
        self._last_partial_samples = 0
        self._utterance_task: asyncio.Task[None] | None = None
        self._level_samples, self._level_peak, self._level_sumsq = 0, 0.0, 0.0
        # voice output: the client is probably still playing audio until then
        self._speaking_until = 0.0
        # approvals awaiting the user's decision, by approval_id
        self._approvals: dict[str, asyncio.Future[bool]] = {}
        # requests to the Mac app awaiting its bridge.response, by request_id
        self._bridge: dict[str, asyncio.Future[BridgeResponse]] = {}

    # ---- sending ----

    async def emit(self, type: EventType, **data: Any) -> None:
        async with self._send_lock:
            if not self._closed:
                await self.ws.send_text(orjson.dumps(event(type, self.session_id, **data)).decode())

    async def send_audio(self, pcm: bytes) -> None:
        async with self._send_lock:
            if not self._closed:
                await self.ws.send_bytes(pcm)

    # ---- receiving ----

    async def run(self) -> None:
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if (text := msg.get("text")) is not None:
                    await self.handle(text)
                elif (data := msg.get("bytes")) is not None:
                    await self.handle_audio(data)
        except WebSocketDisconnect:
            pass
        finally:
            # Work still winding down must not write to the closed socket.
            self._closed = True
            await self.stop_listening(finalize=False)
            await self.cancel_turn()

    async def handle(self, raw: str) -> None:
        try:
            msg = client_message_adapter.validate_json(raw)
        except ValidationError as e:
            await self.emit("error", code="bad_message", message=str(e.errors()[0]["msg"]))
            return

        if isinstance(msg, SessionStart):
            self.session_id = msg.session_id
            self.conversation = self.s.conversations.get_or_create(msg.conversation_id)
            if self.conversation.idle:
                self.conversation = self.s.conversations.new()
            await self.emit("session.ready", conversation_id=self.conversation.id)
        elif self.conversation is None:
            await self.emit("error", code="no_session", message="send session.start first")
        elif isinstance(msg, UserMessage):
            await self.start_turn(msg.text, speak=False)
        elif isinstance(msg, ConversationCancel):
            await self.cancel_turn()
        elif isinstance(msg, AudioStart):
            if self.conversation.idle and not self._assistant_active():
                # Opening the mic after a long pause starts afresh; the client
                # clears its transcript when the conversation ID changes.
                self.conversation = self.s.conversations.new()
                await self.emit("session.ready", conversation_id=self.conversation.id)
            await self.start_listening()
        elif isinstance(msg, AudioStop):
            await self.stop_listening(finalize=True)
        elif isinstance(msg, ApprovalResponse):
            self._resolve_approval(msg.approval_id, msg.approved)
        elif isinstance(msg, BridgeResponse):
            if (fut := self._bridge.get(msg.request_id)) is not None and not fut.done():
                fut.set_result(msg)

    # ---- approvals (spec §19, §72) ----

    async def request_approval(self, req: ApprovalRequest) -> bool:
        """Ask the user; only an answer to this exact approval_id counts, and
        it expires after APPROVAL_TIMEOUT_S or when the turn is cancelled."""
        approval_id = uuid.uuid4().hex
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._approvals[approval_id] = fut
        await self.emit(
            "approval.required",
            approval_id=approval_id,
            tool=req.tool,
            title=req.title,
            # An ordered list, so the sheet shows To/Subject/Body in order.
            fields=[{"label": k, "value": str(v)} for k, v in req.details.items() if v not in (None, "")],
            expires_in=APPROVAL_TIMEOUT_S,
        )
        cancel = self._cancel
        approved = False
        try:
            deadline = time.monotonic() + APPROVAL_TIMEOUT_S
            while not fut.done() and not cancel.is_set() and time.monotonic() < deadline:
                await asyncio.wait([fut], timeout=0.2)
            approved = fut.done() and fut.result()
        finally:
            self._approvals.pop(approval_id, None)
            await self.emit("approval.resolved", approval_id=approval_id, approved=approved)
            log.info("approval", tool=req.tool, approved=approved)
        return approved

    def _resolve_approval(self, approval_id: str, approved: bool) -> bool:
        fut = self._approvals.get(approval_id)
        if fut is None or fut.done():
            return False  # unknown or expired: nothing happens
        fut.set_result(approved)
        return True

    # ---- Mac app bridge ----

    async def call_mac(self, method: str, params: dict[str, Any]) -> Any:
        """Ask the Mac app to run `method` (EventKit and other things only it
        can do) and wait for its answer, like an approval: until it replies,
        the turn is cancelled, or BRIDGE_TIMEOUT_S passes."""
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[BridgeResponse] = asyncio.get_running_loop().create_future()
        self._bridge[request_id] = fut
        cancel = self._cancel
        try:
            await self.emit("bridge.request", request_id=request_id, method=method, params=params)
            deadline = time.monotonic() + BRIDGE_TIMEOUT_S
            while not fut.done() and not cancel.is_set() and not self._closed:
                if time.monotonic() > deadline:
                    raise ToolError("the Mac app didn't answer")
                await asyncio.wait([fut], timeout=0.2)
            if not fut.done():
                raise ToolError("cancelled")
        finally:
            self._bridge.pop(request_id, None)
        resp = fut.result()
        if not resp.ok:
            raise ToolError(resp.error or "the Mac app couldn't do that")
        return resp.result

    # ---- voice input ----

    async def start_listening(self) -> None:
        if self._listening:
            return
        if self._segmenter is None:
            self._segmenter = Segmenter(self.s.vad_factory())
        self._segmenter.reset()
        self._listening = True
        log.info("voice.listening")
        # Warm the models in the background so the first reply isn't slowed
        # by loading (spec §88: load lazily, on first voice use).
        asyncio.create_task(self._warm_models())

    async def _warm_models(self) -> None:
        try:
            await self.s.stt.load()
            await self.s.agent.warm()
            await self.s.tts.load()
        except FileNotFoundError as e:
            await self.emit("error", code="model_unavailable", message=str(e))
        except Exception:
            log.exception("voice.warm_failed")

    async def stop_listening(self, finalize: bool) -> None:
        if not self._listening or self._segmenter is None:
            return
        self._listening = False
        if finalize and (end := self._segmenter.flush()) is not None and end.audio is not None:
            self._finish_utterance(end.audio)
        self._segmenter.reset()

    async def handle_audio(self, data: bytes) -> None:
        if not self._listening or self._segmenter is None or self.conversation is None:
            return
        seg = self._segmenter
        samples = pcm16_to_float(data)
        self._log_input_level(samples)
        seg.min_speech_ms = BARGE_IN_MIN_SPEECH_MS if self._assistant_active() else NORMAL_MIN_SPEECH_MS
        for ev in seg.feed(samples):
            if ev.kind == "start":
                await self._on_speech_start()
            elif ev.audio is not None:
                self._finish_utterance(ev.audio)
        if seg.in_speech:
            n = len(seg.utterance_audio)
            idle = self._partial_task is None or self._partial_task.done()
            if idle and n - self._last_partial_samples >= PARTIAL_EVERY_S * SAMPLE_RATE:
                self._last_partial_samples = n
                self._partial_task = asyncio.create_task(self._partial(seg.utterance_audio))

    def _log_input_level(self, samples: np.ndarray) -> None:
        """Every ~2 s while listening, log mic level and peak speech probability,
        so "I talk and nothing happens" can be told apart from silent input."""
        self._level_samples += len(samples)
        self._level_peak = max(self._level_peak, float(np.abs(samples).max(initial=0.0)))
        self._level_sumsq += float(np.square(samples).sum())
        if self._level_samples >= 2 * SAMPLE_RATE and self._segmenter is not None:
            rms = (self._level_sumsq / self._level_samples) ** 0.5
            log.info(
                "voice.input",
                seconds=round(self._level_samples / SAMPLE_RATE, 1),
                rms_db=round(20 * np.log10(max(rms, 1e-9)), 1),
                peak=round(self._level_peak, 3),
                max_speech_prob=round(self._segmenter.max_prob, 2),
            )
            self._segmenter.max_prob = 0.0
            self._level_samples, self._level_peak, self._level_sumsq = 0, 0.0, 0.0

    def _assistant_active(self) -> bool:
        turn_running = self._turn is not None and not self._turn.done()
        return turn_running or time.monotonic() < self._speaking_until

    async def _on_speech_start(self) -> None:
        self._last_partial_samples = 0
        # Barge-in (spec §13): stop speaking and generating; the client stops
        # playback when it sees this event. While an approval is pending, the
        # user is probably answering it, so the turn is kept alive.
        interrupted = self._assistant_active()
        await self.emit("input.speech_started", interrupted=interrupted)
        if interrupted and not self._approvals:
            self._speaking_until = 0.0
            await self.cancel_turn()

    async def _partial(self, audio: Any) -> None:
        try:
            text = await self.s.stt.transcribe(audio)
            if text and self._segmenter is not None and self._segmenter.in_speech:
                await self.emit("transcript.partial", text=text)
        except FileNotFoundError:
            pass  # reported once by _warm_models
        except Exception:
            log.exception("stt.partial_failed")

    def _finish_utterance(self, audio: Any) -> None:
        self._last_partial_samples = 0
        prev = self._utterance_task
        self._utterance_task = asyncio.create_task(self._final(audio, prev))

    async def _final(self, audio: Any, prev: asyncio.Task[None] | None) -> None:
        if prev is not None:
            await asyncio.wait([prev])  # keep utterances in order
        t0 = time.perf_counter()
        try:
            text = await self.s.stt.transcribe(audio)
        except FileNotFoundError as e:
            await self.emit("error", code="model_unavailable", message=str(e))
            return
        except Exception as e:
            log.exception("stt.final_failed")
            await self.emit("error", code="stt_error", message=type(e).__name__)
            return
        stt_ms = round((time.perf_counter() - t0) * 1000)
        await self.emit("transcript.final", text=text, metrics={"stt_ms": stt_ms})
        log.info("stt.final", ms=stt_ms, audio_s=round(len(audio) / SAMPLE_RATE, 2))
        if text and self._approvals and (decision := spoken_decision(text)) is not None:
            # "Send it." answers only the approval currently on screen.
            approval_id = next(reversed(self._approvals))
            self._resolve_approval(approval_id, decision)
        elif text:
            await self.start_turn(text, speak=True)

    # ---- agent turns ----

    async def start_turn(self, text: str, speak: bool) -> None:
        assert self.conversation is not None
        await self.cancel_turn()
        self._cancel = threading.Event()
        self._turn = asyncio.create_task(self._run_turn(self.conversation, text, self._cancel, speak))

    async def _run_turn(
        self, conversation: Conversation, text: str, cancel: threading.Event, speak: bool
    ) -> None:
        speech = SpeechOutput(self.s.tts, self.send_audio, self.emit, cancel) if speak else None
        started = time.monotonic()

        async def emit(type: EventType, **data: Any) -> None:
            if speech is not None and type == "assistant.text.delta":
                speech.feed(data["delta"])
            await self.emit(type, **data)

        try:
            await self.s.agent.run_turn(
                conversation, text, emit, cancel, self.request_approval, mac=self.call_mac
            )
            if speech is not None and not cancel.is_set():
                await speech.finish()
        except FileNotFoundError as e:
            await self.emit("error", code="model_unavailable", message=str(e))
        except Exception as e:
            log.exception("agent.turn_failed")
            await self.emit("error", code="agent_error", message=type(e).__name__)
        finally:
            if speech is not None:
                await speech.close()
                if not cancel.is_set():
                    self._speaking_until = started + (speech.first_audio_s or 0) + speech.seconds_sent
                if speech.first_audio_s is not None:
                    log.info("tts.turn", first_audio_ms=round(speech.first_audio_s * 1000))

    async def cancel_turn(self) -> None:
        self._cancel.set()
        if self._turn and not self._turn.done():
            await asyncio.wait([self._turn])


async def agent_endpoint(ws: WebSocket) -> None:
    if not websocket_authorized(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    await AgentSession(ws, ws.app.state.services).run()
