"""Configuration via pydantic-settings — loads from .env file."""

from pydantic import field_validator
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

    gemini_api_key: str
    gemini_model: str = "gemini-2.5-pro"
    agent_max_iterations: int = 10

    allowed_emails: list[str] = []
    # Empty list = open access (default). Set to your Spotify account email(s) to lock down.
    # In .env:  ALLOWED_EMAILS=you@example.com,friend@example.com

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def parse_allowed_emails(cls, v: object) -> object:
        if isinstance(v, str):
            return [e.strip() for e in v.split(",") if e.strip()]
        return v


settings = Settings()
