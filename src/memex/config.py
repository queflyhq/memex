from __future__ import annotations

from pathlib import Path

from platformdirs import user_data_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DATA_DIR = Path(user_data_dir("memex", "Quefly"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default=_DEFAULT_DATA_DIR)
    listen: str = "127.0.0.1:7777"
    auth_token: str | None = None
    log_level: str = "INFO"

    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embed_dim: int = 384

    daemon_url: str | None = None
    pack_registry: str = "https://github.com/queflyhq/memex-skills"
    auto_bootstrap: bool = False
    bootstrap_root: Path = Field(default_factory=Path.cwd)

    @property
    def graph_path(self) -> Path:
        return self.data_dir / "graph.kuzu"

    @property
    def vectors_path(self) -> Path:
        return self.data_dir / "vectors.db"

    @property
    def episodic_path(self) -> Path:
        return self.data_dir / "episodic.db"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings


def reset_settings_for_testing(settings: Settings | None = None) -> None:
    """Test helper: replace the cached settings instance."""
    global _settings
    _settings = settings
