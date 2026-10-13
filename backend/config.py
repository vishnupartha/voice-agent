from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "PersonalAgent"
LOG_DIR = Path.home() / "Library" / "Logs" / "PersonalAgent"


class Settings(BaseSettings):
    """Non-secret settings. Secrets live in the macOS Keychain, never here."""

    model_config = SettingsConfigDict(env_file=".env.local", extra="ignore")

    personal_agent_host: str = "127.0.0.1"
    personal_agent_port: int = 8765

    data_dir: Path = APP_SUPPORT
    model_dir: Path = APP_SUPPORT / "models"
    database_path: Path = APP_SUPPORT / "database.sqlite"
    log_dir: Path = LOG_DIR

    # Hugging Face repos; weights live in model_dir/<repo basename>.
    stt_repo: str = "mlx-community/parakeet-tdt-0.6b-v3"
    llm_repo: str = "mlx-community/Qwen3.5-9B-4bit"
    tts_repo: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-6bit"
    tts_voice: str = "ryan"

    llm_max_tokens: int = 1024
    # Low temperature: at 0.7 the model skipped tool calls ~30% of the time.
    llm_temperature: float = 0.2
    llm_top_p: float = 0.8

    background_monitoring: bool = True

    def model_path(self, repo: str) -> Path:
        return self.model_dir / repo.split("/")[-1]

    @property
    def stt_model_path(self) -> Path:
        return self.model_path(self.stt_repo)

    @property
    def llm_model_path(self) -> Path:
        return self.model_path(self.llm_repo)

    @property
    def tts_model_path(self) -> Path:
        return self.model_path(self.tts_repo)


@lru_cache
def get_settings() -> Settings:
    return Settings()
