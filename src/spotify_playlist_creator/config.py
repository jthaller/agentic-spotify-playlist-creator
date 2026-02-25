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
    spotify_redirect_uri: str = "http://127.0.0.1:8501"
    spotify_cache_path: str = ".spotify_cache"

    gemini_api_key: str
    gemini_model: str = "gemini-2.5-pro"
    agent_max_iterations: int = 10

    allowed_emails: list[str] = []
    # Empty list = open access (default). Set to your Spotify account email(s) to lock down.
    # In .env:  ALLOWED_EMAILS=you@example.com,friend@example.com


settings = Settings()
