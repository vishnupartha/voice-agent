# Personal Agent

A voice assistant for your Mac. Press **Option+Space** anywhere, talk, and it answers out loud. It can check your Gmail and Google Calendar, search the web, set Apple Reminders, keep notes in Apple Notes, and remember things you tell it. Anything that sends, changes or deletes your stuff asks you first.

Speech recognition and the voice run on your Mac. The brain is Google's Gemini Flash (free with a Google account) by default, or, if you prefer everything local, a Qwen3.5 model running on your Mac.

## Requirements

- A Mac with Apple Silicon (M1 or later) running macOS 26 (Tahoe) or later
- Xcode, from the App Store (to build the app)
- About 5 GB of disk space (15 GB with the local brain)
- A free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey), unless you use the local brain

## Install

```bash
git clone https://github.com/vishnupartha/voice-agent.git
cd voice-agent
./install.sh
```

The script installs what's missing ([uv](https://docs.astral.sh/uv/), and XcodeGen via Homebrew), downloads the speech models, builds the app and puts it in Applications. Keep the folder: the app runs its backend from it. To update later, `git pull` and run `./install.sh` again.

On first launch the **Settings** window opens. Paste your Gemini key. Then press **Option+Space** and allow microphone access.

Want it fully local? Run `./install.sh --with-local` and choose **Qwen3.5 9B (local)** from the dashboard's **Brain** menu. It's private but noticeably less capable, and wants 24 GB of memory or more.

## Use it

- **Option+Space** from any app opens the voice panel. Speak, pause, and it answers aloud. Talk over it to interrupt. Press Option+Space again to close the mic.
- It picks up where you left off: Option+Space continues the current conversation, and after five quiet minutes starts a new one. Conversations are saved, so quitting the app doesn't lose them.
- The **◉ menu bar icon** has Talk to Assistant, Open Dashboard, Settings and Quit. (On a MacBook with a notch, a crowded menu bar can hide it behind the notch.)
- The **dashboard** shows the conversation and what the assistant did (searched Gmail, added a reminder…). You can type there too. Its toolbar switches the **Brain** (Gemini or local) and the **Voice** (Kokoro, steady; or Qwen3-TTS, more expressive but occasionally ad-libs a laugh).

What it can do:

| | Without asking | Asks you first |
|---|---|---|
| **Gmail** | Search, read, summarize, draft and edit replies | Sending |
| **Google Calendar** | Read events across your calendars, find free time | Adding, changing, deleting events |
| **Reminders** | List, add (with an alert when there's a time), mark done | Deleting |
| **Notes** | Create, read and add to notes in its own **Personal Agent** folder | Searching, reading or adding to any of *your* other notes |
| **Memory** | Remember and forget what you tell it to ("Remember my advisor is Dr. Lee") | |
| **Web** | Search and read pages | |

Approve on screen (Send / Cancel) or by voice ("send it", "yes", "cancel"). To let it use one of your notes without asking each time, drag the note into the Personal Agent folder.

Things to try: "What's on my calendar tomorrow?", "When am I free Friday afternoon?", "What's important in my email?", "Draft a reply saying Thursday after two works", "Make it shorter", "Send it", "Remind me to submit the form tomorrow at nine", "Make a grocery list note with eggs, milk and bread", "Add coffee to the grocery list", "Remember that I'm vegetarian", "What do you know about me?", "Who won the game last night?"

## Connect Google (optional)

Google only lets apps read your mail through an OAuth client in a Google Cloud project, so each user creates a free one of their own:

1. Go to <https://console.cloud.google.com/> and create a project (e.g. "Personal Agent").
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **Google Auth Platform → Branding**: enter an app name and your email (User type: External).
4. **Audience → Test users**: add your own Gmail address.
5. **Clients → Create client → Desktop app**, then **Download JSON**.
6. In Personal Agent's **Settings**, click **Choose Client File…** and pick the downloaded file. It's stored in your Keychain; you can delete the download afterwards.
7. Click **Connect Google…**. Sign in in your browser, click through "Google hasn't verified this app" (it's your own app), and allow access.

While the project's publishing status is *Testing*, Google ends the sign-in after 7 days. To avoid that, choose **Audience → Publish app**; it stays private to you.

The assistant reads every calendar that's ticked in Google Calendar's sidebar. To hide one from it (say, a calendar someone shared with you), add it to `hidden_calendars` in `~/Library/Application Support/PersonalAgent/preferences.json`.

## Privacy

- Speech recognition (NVIDIA Parakeet) and the voice (Kokoro or Qwen3-TTS) always run on your Mac. Audio is never stored.
- With **Gemini** as the brain, what you say and whatever the assistant reads to answer you (email, calendar events, web pages) is sent to Google. On the free tier, Google may use it to improve its products. Choose the local brain if that's not OK.
- Conversations and memories are stored on your Mac in `~/Library/Application Support/PersonalAgent/database.sqlite`. Memories are included with every request to the brain, so with Gemini they go to Google too. Say "forget that" to remove one, or delete the database to start over.
- Reminders and Notes access is granted to the app by macOS and can be turned off in System Settings → Privacy & Security. The assistant only reads or changes your own notes (outside its folder) after you approve each time.
- Web search goes through DuckDuckGo.
- Google sign-in tokens and API keys live in the macOS Keychain, never in files or logs.

## Troubleshooting

- **It doesn't hear me** (the level meter stays flat): System Settings → Privacy & Security → Microphone, and make sure Personal Agent is on. After rebuilding, run `./install.sh` again; it resets the permission so macOS asks afresh.
- **Reminders or Notes don't work**: the first use asks for permission. If you said no, turn it on in System Settings → Privacy & Security → **Reminders**, or → **Automation** → Personal Agent → **Notes**. After rebuilding, run `./install.sh` again; it resets these too.
- **New features don't show up after `git pull`**: run `./install.sh` again. It rebuilds the app and restarts the backend.
- **"Gemini is busy"**: Flash is often overloaded on the free tier. The app falls back to Flash-Lite automatically; if both are busy, try again in a minute.
- **Logs**: `~/Library/Logs/PersonalAgent/agentd.log`.

## Develop

```bash
uv sync
uv run python -m scripts.download_models    # speech models; add "llm tts" or "all" for the rest
xcodegen generate && open PersonalAgent.xcodeproj   # Build & Run "My Mac"
uv run pytest                                # backend tests
uv run ruff check backend && uv run ruff format --check backend
```

Builds are ad-hoc signed by default. If you rebuild often, sign with your own certificate so the microphone permission survives rebuilds: copy `Config/Local.xcconfig.example` to `Config/Local.xcconfig` and fill in your team ID.

The app starts the backend itself (`uv run uvicorn backend.api.server:app`, bound to 127.0.0.1:8765), and replaces a backend left running from an earlier session (for example after stopping the app in Xcode). Backend code changes take effect when you quit and reopen the app. Tools that need native macOS APIs (Reminders via EventKit) ask the app over the WebSocket with `ctx.mac(...)`; see `MacApp/System/MacBridge.swift`. Conversations and memories live in SQLite (`backend/db.py`). Settings such as model choices live in `backend/config.py` and can be overridden in a `.env.local` file. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains how it all fits together, including how to add a tool.

## License

[MIT](LICENSE). The models are downloaded from Hugging Face under their own licenses: Parakeet (CC-BY-4.0), Kokoro and Qwen (Apache-2.0).
