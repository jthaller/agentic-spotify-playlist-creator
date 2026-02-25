"""Pydantic v2 domain models for the Spotify Playlist Creator."""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, Field


class Artist(BaseModel):
    id: str
    name: str
    genres: list[str] = Field(default_factory=list)
    popularity: int = 0
    image_url: str | None = None
    spotify_url: str | None = None


class AudioFeatures(BaseModel):
    id: str
    danceability: float = 0.0
    energy: float = 0.0
    valence: float = 0.0
    tempo: float = 0.0
    acousticness: float = 0.0
    instrumentalness: float = 0.0
    speechiness: float = 0.0
    loudness: float = 0.0
    mode: int = 0
    key: int = 0


class Track(BaseModel):
    id: str
    name: str
    artists: list[Artist] = Field(default_factory=list)
    album_name: str = ""
    album_image_url: str | None = None
    duration_ms: int = 0
    popularity: int = 0
    explicit: bool = False
    preview_url: str | None = None
    spotify_url: str | None = None
    audio_features: AudioFeatures | None = None

    @property
    def artist_names(self) -> str:
        return ", ".join(a.name for a in self.artists)

    @property
    def duration_str(self) -> str:
        total_seconds = self.duration_ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"


class Playlist(BaseModel):
    id: str
    name: str
    description: str = ""
    tracks: list[Track] = Field(default_factory=list)
    spotify_url: str | None = None
    image_url: str | None = None
    owner: str = ""
    public: bool = False


class UserProfile(BaseModel):
    id: str
    display_name: str = ""
    email: str = ""
    country: str = ""
    product: str = ""
    image_url: str | None = None
    followers: int = 0


class UserListeningContext(BaseModel):
    top_tracks_short: list[Track] = Field(default_factory=list)
    top_tracks_long: list[Track] = Field(default_factory=list)
    top_artists_short: list[Artist] = Field(default_factory=list)
    top_artists_long: list[Artist] = Field(default_factory=list)
    recently_played: list[Track] = Field(default_factory=list)
    favorite_genres: list[str] = Field(default_factory=list)


class PlaylistRequest(BaseModel):
    user_input: str
    target_length: int = 20
    include_explicit: bool = True


class ToolCall(BaseModel):
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: str
    iteration: int


class AgentResult(BaseModel):
    track_ids: list[str]
    playlist_name: str
    playlist_description: str
    reasoning_summary: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    iterations_used: int = 0
