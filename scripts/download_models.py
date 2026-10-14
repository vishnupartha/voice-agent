"""Download local model weights into ~/Library/Application Support/PersonalAgent/models (spec §87).

Re-running resumes interrupted downloads and skips complete ones. Hugging Face
verifies file hashes as it downloads.

    uv run python scripts/download_models.py [stt|llm|tts|kokoro ...]
"""

from __future__ import annotations

import sys

from huggingface_hub import snapshot_download

from backend.config import get_settings


def main(which: list[str]) -> None:
    settings = get_settings()
    repos = {
        "stt": settings.stt_repo,
        "llm": settings.llm_repo,
        "tts": settings.tts_repo,
        "kokoro": settings.kokoro_repo,
    }
    for kind in which or list(repos):
        repo = repos[kind]
        dest = settings.model_dir / repo.split("/")[-1]
        print(f"[{kind}] {repo} -> {dest}")
        snapshot_download(repo, local_dir=dest)
    # Silero VAD ships inside the silero-vad package; nothing to download.
    print("done")


if __name__ == "__main__":
    main(sys.argv[1:])
