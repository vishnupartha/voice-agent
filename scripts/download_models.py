"""Download local model weights into ~/Library/Application Support/PersonalAgent/models.

Re-running resumes interrupted downloads and skips complete ones. Hugging Face
verifies file hashes as it downloads.

    uv run python -m scripts.download_models            # speech in and out (~2.7 GB)
    uv run python -m scripts.download_models llm        # + local brain, Qwen3.5 9B (~8 GB)
    uv run python -m scripts.download_models tts        # + expressive Qwen3-TTS voice (~2.5 GB)
    uv run python -m scripts.download_models all        # everything
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
    # Speech recognition and the Kokoro voice are all Gemini mode needs.
    default = ["stt", "kokoro"]
    which = list(repos) if which == ["all"] else which or default
    for kind in which:
        if kind not in repos:
            sys.exit(f"unknown model {kind!r}; choose from {', '.join(repos)} or all")
        repo = repos[kind]
        dest = settings.model_dir / repo.split("/")[-1]
        print(f"[{kind}] {repo} -> {dest}")
        snapshot_download(repo, local_dir=dest)
    # Silero VAD ships inside the silero-vad package; nothing to download.
    print("done")


if __name__ == "__main__":
    main(sys.argv[1:])
