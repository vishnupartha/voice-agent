# Build Roadmap

Staged plan for the first-pass spec ("Mac Personal AI Agent — First-Pass Build
Specification"; section numbers below are §N of that spec). Update the checkboxes
as stages land so any session can see where the build is.

## Deliberate deviations from the spec

- **LLM:** Gemini Flash through the free Gemini API (`LLM_PROVIDER=gemini`, the default),
  falling back to Flash-Lite when Flash is overloaded. This is not local: prompts, tool
  results (email, calendar) go to Google, and free-tier data may be used to improve
  Google's products. `LLM_PROVIDER=local` uses `mlx-community/Qwen3.5-9B-4bit`, which
  proved too weak for a voice assistant. Also used when no Gemini key is stored.
- **STT:** `mlx-community/parakeet-tdt-0.6b-v3`.
- **TTS:** Kokoro-82M (default, voice `af_heart`) or Qwen3-TTS 1.7B CustomVoice 6-bit (voice
  `ryan`), switchable from the dashboard's Voice menu. Qwen3-TTS samples its audio and
  sometimes ad-libs laughs or drawn-out words; Kokoro can't.
- **Protocol additions:** `user.message` (typed turns) and `input.speech_started`
  (tells the client to stop playback on barge-in). `/ws/chat` and `/ws/voice`
  share one session type; only spoken turns get audio replies.

- **Speech output:** times/dates/money/numbers normalized to words before TTS
  (`backend/voice/normalize.py`); playback at 1.2× via AVAudioUnitTimePitch; queued
  sentences synthesized together. TTS temperature stays 0.9: lower values cause long
  pauses and loops.
- **Clock:** the current time is stamped on each user message, not put in the system
  prompt, so the system prompt + tools prefix stays cacheable.

## Stages

- [x] **0 — Skeleton.** FastAPI on 127.0.0.1:8765 with token auth, `/ws/chat` streaming
  text chat with cancel, MLX LLM provider (lazy load), SwiftUI menu-bar app that
  launches/health-checks the backend, conversation window.
- [x] **1 — Voice.** Silero VAD, Parakeet STT, Qwen3-TTS via mlx-audio, `/ws/voice`
  (PCM16 16 kHz in, PCM out), barge-in/interrupt (§12–14). Swift AudioCapture /
  AudioPlayback, Option+Space Carbon hotkey, VoiceOverlay, partial transcripts.
  Verified end to end with synthesized speech (`scripts/smoke_voice.py` for model
  latency). Measured warm: STT final ~60 ms, LLM first token ~190–500 ms, TTS first
  chunk ~85 ms, speech-end → reply audio 0.8–2.0 s, barge-in stop ~90 ms.
  Follow-ups: cache the system-prompt KV prefix (first turn prefill ~1.3 s); drop
  the torch dependency by running Silero via ONNX before packaging.
- [~] **2 — Agent core.** *Done:* tool registry with Pydantic args, schema-based coercion,
  timeouts, READ/WRITE/APPROVAL/FORBIDDEN, parallel reads, untrusted-content wrapping;
  Qwen XML tool-call parsing that never speaks call markup; approval flow with
  approval_id (button or spoken "send it"/"cancel"); ToolStatusView, ApprovalView,
  DraftCard; prompt-prefix KV snapshots (first token 3.5 s → ~0.5 s); temperature 0.2
  (tool-call rate 70% → 95%) plus forced continuation when the model announces an
  action without calling it. *Left:* SQLite persistence, memory tools, summarization.
  Originally: SQLite + migrations (§47), persisted conversations/history
  (§74), tool registry with Pydantic schemas and timeouts (§60), permission levels +
  approval flow with approval_id (§58, §72), parallel reads (§61), system.* and
  memory.* tools (§48–50, §57), context management/summarization (§78). Swift
  ToolStatusView, ApprovalView.
- [ ] **3 — Web, documents, files.** Brave search, fetch/extract (untrusted wrapping,
  §70), SourceCard; PyMuPDF docs, FastEmbed + sqlite-vec hybrid retrieval (§53),
  explainer modes (§54); folder-scoped files.* (§52).
- [~] **4 — Google.** *Done:* OAuth via loopback callback, client + token in Keychain,
  all spec Gmail and Calendar tools (live API, no local cache yet), draft editing by
  voice, profile (name) for signing. *Left:* local cache + history sync (§24–25).
  Originally: OAuth loopback flow, Keychain tokens (§21–22), Gmail tools +
  cache + history sync (§23–26), draft session (§26), Calendar tools (§27–28).
- [ ] **5 — Opportunities.** Source registry (§36), opportunities/internships/clubs/
  research/music/recommendations tools, dedup (§80), ranking + feedback (§64–66,
  §81), restaurants via Swift MapKit bridge (§43), location (§42).
- [ ] **6 — Instagram.** Isolated Playwright Chromium profile, read-only tools (§32–35).
- [ ] **7 — Background.** APScheduler jobs (§62), email importance (§79), notifications
  (§82), daily briefing (§63), pause from menu bar.
- [ ] **8 — Product polish.** Settings tabs (§20), onboarding + first-launch tests
  (§93–94), developer panel (§91), launch at login (§83), `scripts/dev.sh` (§104),
  PyInstaller `agentd` packaging (§105).

## Ideas and future work

Pick anything here; open an issue first for the bigger ones.

### Voice and hearing
- [ ] **Echo cancellation toggle.** Apple's echo cancellation may be dampening your voice. Add a setting to turn it off; the plain mode then pauses the mic while the assistant talks.
- [ ] **Show what it heard.** A debug view with the transcript of each utterance and its audio level, to diagnose "it doesn't hear me well" with data.
- [ ] **Tune end-of-speech.** It currently waits 0.7 s of silence before answering. Make it configurable, and consider waiting longer when the sentence sounds unfinished.
- [ ] **Faster first reply.** The very first turn after launch still prefills the whole prompt. It's cached at model warm-up, but warm-up only starts when the mic first opens. Start it at app launch instead.

### Google
- [ ] **Local Gmail cache plus incremental sync** via the History API (spec §24–25). Gives faster answers, offline access to recent mail, and is the basis for background monitoring.
- [ ] **Email importance classifier** (spec §79), so "what's important" means more than Gmail's own Important label.
- [ ] **Attachment awareness.** Mention attachments and optionally read PDFs (pairs with the documents work below).
- [ ] **Reconnect prompt.** When the token expires, show a clear "Reconnect Google" banner instead of a tool error.

### Agent brain (rest of Stage 2)
- [~] **SQLite persistence** (spec §47): conversations survive restarts (done; opening the mic after 5 minutes idle starts a new one). *Left:* history sidebar.
- [x] **Memory** (spec §48–50): "remember that…", "what do you know about me?", "forget that". Explicit only; every memory is listed in the system prompt (no retrieval yet).
- [ ] **Conversation summarization** (spec §78): summarize old turns instead of keeping the last 40 messages.
- [ ] **A test set of real requests** (like the canned-inbox run used during development), run against the real model after every prompt or tool change to catch regressions.

### UI
- [ ] **Settings window** (spec §20): tabs for General, Connections, Models, Permissions, Memory.
- [ ] **Onboarding** (spec §93): mic, notifications and location permissions, the model download, Google connect, and "what should I call you".
- [ ] **Conversation history sidebar** (spec §74): Today / Yesterday / Previous 7 Days.
- [ ] **Richer cards:** email results as tappable cards (open in Gmail), calendar events as a day timeline.
- [ ] **Model status panel** (spec §88): STT / LLM / TTS loading and ready, with download progress.
- [ ] **Clear error states** (spec §85): backend down, model missing, Google disconnected, no internet.
- [ ] **Overlay polish:** show the reply text larger while speaking; support push-to-talk (hold Option+Space).

### Later stages (see ROADMAP.md)
- [ ] PDF and research-paper reading, with explanation modes (simpler, analogy, math).
- [ ] Opportunities, clubs, internships, and events near the user.
- [ ] Background monitoring, notifications, and a morning briefing.
- [ ] Instagram (read-only, isolated browser). Meta Muse offers no API for calling it from another app.
- [ ] Packaging: a standalone `.app` with a bundled backend; launch at login.

### Known limitations
- The dashboard doesn't show earlier messages after a restart, though the assistant still has them.
- Gmail and Calendar are live API calls (no cache), so they need internet.
- The Silero VAD pulls in PyTorch (large). Switch to its ONNX version before packaging.
