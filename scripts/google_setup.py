"""Store your Google OAuth client in the macOS Keychain (one-time).

    uv run python -m scripts.google_setup ~/Downloads/client_secret_XXXX.json

The file comes from Google Cloud Console → APIs & Services → Credentials →
Create credentials → OAuth client ID → Desktop app → Download JSON. After this
you can delete the downloaded file; then choose "Connect Google…" in the app.
"""

from __future__ import annotations

import sys
from pathlib import Path

from backend.integrations.google_auth import GoogleAuth


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    GoogleAuth.save_client_config(Path(sys.argv[1]).expanduser().read_text())
    print("Saved to Keychain. Now choose “Connect Google…” from the Personal Agent menu bar icon.")


if __name__ == "__main__":
    main()
