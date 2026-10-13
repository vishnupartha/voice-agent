# Build Roadmap

Staged plan for the first-pass spec ("Mac Personal AI Agent — First-Pass Build
Specification"; section numbers below are §N of that spec). Update the checkboxes
as stages land so any session can see where the build is.

## Deliberate deviations from the spec

- **LLM:** `mlx-community/Qwen3.5-9B-4bit` instead of Qwen3-4B-Instruct-2507 (the dev Mac
  has 48 GB; swappable via `LLM_REPO` in `.env.local`).
- **STT/TTS repos:** `mlx-community/parakeet-tdt-0.6b-v3`,
  `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit` (voice `ryan`).
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
