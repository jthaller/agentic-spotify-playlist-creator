"""Configuration via pydantic-settings — loads from .env file."""

from pydantic_settings import BaseSettings, SettingsConfigDict


SPOTIFY_SCOPES = (
    "user-read-private "
    "user-top-read "
    "user-read-recently-played "
    "playlist-modify-public "
    "playlist-modify-private"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str = "http://localhost:8501"
    spotify_cache_path: str = ".spotify_cache"

    anthropic_api_key: str
    claude_model: str = "claude-opus-4-6"
    agent_max_iterations: int = 10


settings = Settings()
