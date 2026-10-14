# Next Steps

Things you need to do by hand, then the work queue, most valuable first.
[ROADMAP.md](ROADMAP.md) tracks what's already built.

---

## Part 1 — Things you need to do

### 1. Connect Google (about 5 minutes, one time)

Google only gives apps access to your mail through an OAuth client in a Google Cloud project you own.

1. Go to <https://console.cloud.google.com/>, then create a new project (e.g. "Personal Agent").
2. **APIs & Services → Library**: enable **Gmail API**, then enable **Google Calendar API**.
3. **Google Auth Platform → Branding**: enter an app name and your email. User type: **External**.
4. **Google Auth Platform → Audience → Test users**: add your own Gmail address.
5. **Google Auth Platform → Clients → Create client**: type **Desktop app**, then **Download JSON**.
6. In the repo, store it in your Keychain:
   ```bash
   uv run python -m scripts.google_setup ~/Downloads/client_secret_*.json
   ```
   Then delete the downloaded JSON file.
7. Click the **◉ menu bar icon → Connect Google…**. Sign in in your browser, click through
   "Google hasn't verified this app" (it's your own app), and allow every permission.
8. The menu should now say **Google: Connected**.

**7-day expiry:** while the project's publishing status is *Testing*, Google ends the sign-in after 7 days, and you'll have to choose Connect Google… again. To stop that, go to **Audience → Publish app**. It stays private to you; you'll just see the unverified-app warning when signing in.

**If sign-in fails with `redirect_uri_mismatch`:** make sure the client type is *Desktop app*, not *Web application*. Desktop clients accept the app's local callback (`http://127.0.0.1:8765/auth/google/callback`) without configuring it.

### 1b. Gemini API key (the assistant's brain)

Create a free key at <https://aistudio.google.com> → Get API key, copy it, then:
```bash
pbpaste | uv run python -m scripts.gemini_setup
```
Restart the app. Without a key it falls back to the local Qwen3.5-9B model.

### 2. Try Gmail and Calendar by voice

Press **Option+Space** and try these in order. They form one conversation:

1. "What's important in my email?"
2. "What does [sender] want?"
3. "When am I free Thursday afternoon?"
4. "Draft a reply saying Thursday at two works."
5. "Make it a little less formal." (The draft card in the main window should update.)
6. "Send it." A confirmation appears; say "send it" again or click Send. **This really sends.** Use a thread where that's fine, or say "cancel".
7. "What's on my calendar tomorrow?"
8. "Add lunch with Sam Friday at noon." Approve or cancel.

Note anything it gets wrong; that's the input for prompt and tool fixes.

### 3. Report back on voice quality

- Does the voice sound better than before, and is 1.2× speed right? Speed is `AudioPlayback.rate` in `MacApp/Audio/AudioPlayback.swift`.
- Does it hear you reliably? If it misses words or cuts you off mid-sentence, say which. They have different fixes: the echo-cancellation setting, or the 0.7 s end-of-speech wait.

### 4. Approve the first git commit

The repo has no commits yet. A `.gitignore` already excludes `build/`, `.venv/` and caches. Say the word and Claude will make the first commit.

---

## Part 2 — Future work

### Voice and hearing
- [ ] **Echo cancellation toggle.** Apple's echo cancellation may be dampening your voice. Add a setting to turn it off; the plain mode then pauses the mic while the assistant talks.
- [ ] **Show what it heard.** A debug view with the transcript of each utterance and its audio level, to diagnose "it doesn't hear me well" with data.
- [ ] **Tune end-of-speech.** It currently waits 0.7 s of silence before answering. Make it configurable, and consider waiting longer when the sentence sounds unfinished.
- [ ] **Voice A/B test.** Render the same sentences with Qwen3-TTS 6-bit, Qwen3-TTS 8-bit/bf16, and Kokoro-82M (smaller, very smooth English), plus a few speakers, and pick by ear.
- [ ] **Voice and speed settings** in the UI instead of constants.
- [ ] **Faster first reply.** The very first turn after launch still prefills the whole prompt. It's cached at model warm-up, but warm-up only starts when the mic first opens. Start it at app launch instead.

### Google
- [ ] **Local Gmail cache plus incremental sync** via the History API (spec §24–25). Gives faster answers, offline access to recent mail, and is the basis for background monitoring.
- [ ] **Email importance classifier** (spec §79), so "what's important" means more than Gmail's own Important label.
- [ ] **Multiple calendars.** Today it uses only `primary`; include shared and subscribed calendars.
- [ ] **Attachment awareness.** Mention attachments and optionally read PDFs (pairs with the documents work below).
- [ ] **Reconnect prompt.** When the token expires, show a clear "Reconnect Google" banner instead of a tool error.

### Agent brain (rest of Stage 2)
- [ ] **SQLite persistence** (spec §47): conversations survive restarts, plus a history sidebar.
- [ ] **Memory** (spec §48–50): "remember that…", "what do you know about me?", "forget that"; preferences fed into answers.
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
- [ ] Web search (Brave) and page reading, with source cards.
- [ ] PDF and research-paper reading, with explanation modes (simpler, analogy, math).
- [ ] Opportunities, clubs, internships, and events near UCLA.
- [ ] Background monitoring, notifications, and a morning briefing.
- [ ] Instagram (read-only, isolated browser). Meta Muse offers no API for calling it from another app; see the earlier discussion.
- [ ] Packaging: a standalone `.app` with a bundled backend; launch at login.

### Known limitations right now
- Conversations live in memory only; quitting the app forgets them.
- Gmail and Calendar are live API calls (no cache), so they need internet.
- The Silero VAD pulls in PyTorch (large). Switch to its ONNX version before packaging.
