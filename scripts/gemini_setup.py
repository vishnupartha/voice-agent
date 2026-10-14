"""Store your Gemini API key in the macOS Keychain (one-time).

Copy the key from https://aistudio.google.com (Get API key), then:

    pbpaste | uv run python -m scripts.gemini_setup

or run it without the pipe and paste the key at the prompt (it isn't shown).
"""

from __future__ import annotations

import getpass
import sys

import keyring

from backend.integrations.google_auth import KEYCHAIN_SERVICE
from backend.voice.gemini import GEMINI_KEY


def main() -> None:
    key = sys.stdin.read() if not sys.stdin.isatty() else getpass.getpass("Gemini API key: ")
    key = key.strip()
    if not key.startswith("AIza") or len(key) < 30:
        sys.exit("That doesn't look like a Gemini API key (they start with “AIza”).")
    keyring.set_password(KEYCHAIN_SERVICE, GEMINI_KEY, key)
    print("Saved to Keychain. Restart Personal Agent to use Gemini.")


if __name__ == "__main__":
    main()
