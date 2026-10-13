# Personal Agent

A private voice assistant for your Mac. Speech recognition, the language model and the voice all run locally on Apple Silicon. Gmail and Google Calendar are optional connections.

## Use it

- **Option+Space** from any app opens the voice panel. Speak, pause, and it answers aloud. Talk over it to interrupt. Press Option+Space again to close the mic.
- The **◉ menu bar icon** has Talk to Assistant, Connect Google, Open Dashboard and Quit.
- The dashboard window shows the conversation, what the agent did (Gmail searched, calendar checked…), and any email draft it's working on. You can type there too.
- Sending email and adding, changing or deleting calendar events always ask first. Click Send / Cancel, or just say "send it" / "cancel".

Things to try: "What's important in my email?", "What does she want?", "Draft a reply saying Thursday after two works", "Make it shorter", "Send it", "What's my day look like tomorrow?", "When am I free Friday afternoon?", "Move my 3 PM to 4".

## Connect Google (one time, about 5 minutes)

Google only lets apps read your mail through an OAuth client that belongs to a Google Cloud project, so you create a free one of your own:

1. Go to <https://console.cloud.google.com/>, then create a project (e.g. "Personal Agent").
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **Google Auth Platform → Branding**: fill in an app name and your email (User type: External).
4. **Audience → Test users**: add your own Gmail address.
5. **Clients → Create client → Desktop app**, then **Download JSON**.
6. Store it in your Keychain:
   ```bash
   uv run python -m scripts.google_setup ~/Downloads/client_secret_*.json
   ```
   After that you can delete the downloaded file.
7. Menu bar ◉ → **Connect Google…** opens your browser. Sign in, click through "Google hasn't verified this app" (it's your own app), and allow access.

Note: while the app's publishing status is *Testing*, Google expires the sign-in after 7 days, and you'll need to choose Connect Google… again. To avoid that, set **Audience → Publish app** (it stays private to you).

Tokens are stored in the macOS Keychain and never reach the model or the logs.

## Develop

```bash
uv sync
uv run python -m scripts.download_models     # ~9 GB into ~/Library/Application Support/PersonalAgent/models
xcodegen generate && open PersonalAgent.xcodeproj   # Build & Run "My Mac"
uv run pytest                                 # backend tests
uv run python -m scripts.smoke_voice          # real-model latency check
```

The app starts the backend itself (`uv run uvicorn backend.api.server:app`, bound to 127.0.0.1:8765). Logs are in `~/Library/Logs/PersonalAgent/`. Build progress and the stage plan are in [docs/ROADMAP.md](docs/ROADMAP.md); manual setup steps and the work queue are in [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md).
