"""Orchestration layer that wires the agent and Spotify client together."""

from __future__ import annotations

from typing import Callable

import spotipy

from .gemini_agent import PlaylistAgent
from .models import AgentResult, Playlist, PlaylistRequest, UserListeningContext, UserProfile
from .spotify_client import SpotifyClient


class PlaylistPlanner:
    """Orchestrates playlist creation: fetches user context, runs the agent, creates the playlist."""

    def __init__(self, sp: spotipy.Spotify) -> None:
        self._spotify_client = SpotifyClient(sp)
        self._agent = PlaylistAgent(self._spotify_client)

    def get_user_profile(self) -> UserProfile:
        return self._spotify_client.get_current_user()

    def get_listening_context(self) -> UserListeningContext:
        return self._spotify_client.build_listening_context()

    def create_playlist(
        self,
        request: PlaylistRequest,
        user_profile: UserProfile,
        listening_context: UserListeningContext,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[AgentResult, Playlist]:
        # Run the Gemini agent loop
        agent_result = self._agent.run(
            request=request,
            user_profile=user_profile,
            listening_context=listening_context,
            progress_callback=progress_callback,
        )

        # Deduplicate while preserving order
        seen: dict[str, None] = {}
        unique_ids = list(dict.fromkeys(agent_result.track_ids))

        if progress_callback:
            progress_callback(f"Creating playlist with {len(unique_ids)} tracks...")

        playlist = self._spotify_client.create_playlist(
            user_id=user_profile.id,  # kept for signature compat, not used by endpoint
            name=agent_result.playlist_name,
            description=agent_result.playlist_description,
            track_ids=unique_ids,
            public=False,
        )

        return agent_result, playlist
