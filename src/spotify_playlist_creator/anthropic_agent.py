"""Claude agentic loop with Spotify tool definitions."""

from __future__ import annotations

import json
from typing import Callable

import anthropic

from .config import settings
from .models import AgentResult, PlaylistRequest, ToolCall, UserListeningContext, UserProfile
from .spotify_client import SpotifyClient


# ------------------------------------------------------------------ #
# Tool definitions
# ------------------------------------------------------------------ #

TOOLS: list[dict] = [
    {
        "name": "search_tracks",
        "description": (
            "Search the Spotify catalog for tracks matching a query. "
            "Use specific queries like 'artist:Radiohead genre:alternative' for better results. "
            "Returns track IDs, names, artists, and popularity scores."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (supports field filters: artist:, track:, genre:, year:)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (1–50)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_recommendations",
        "description": (
            "Get track recommendations from Spotify based on seed tracks, artists, or genres. "
            "This is the most powerful discovery tool — use it frequently. "
            "Total number of seeds (tracks + artists + genres) must be between 1 and 5."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seed_tracks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Spotify track IDs to use as seeds (max 5 total seeds)",
                },
                "seed_artists": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Spotify artist IDs to use as seeds (max 5 total seeds)",
                },
                "seed_genres": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "List of genre strings to use as seeds. "
                        "Must be valid Spotify genres (e.g. 'pop', 'rock', 'electronic', 'jazz'). "
                        "Max 5 total seeds."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of recommendations to return (1–100)",
                    "default": 20,
                },
                "target_energy": {
                    "type": "number",
                    "description": "Target energy level 0.0–1.0 (0=calm, 1=intense)",
                },
                "target_valence": {
                    "type": "number",
                    "description": "Target valence/mood 0.0–1.0 (0=sad/dark, 1=happy/upbeat)",
                },
                "target_danceability": {
                    "type": "number",
                    "description": "Target danceability 0.0–1.0",
                },
                "target_tempo": {
                    "type": "number",
                    "description": "Target tempo in BPM",
                },
                "min_popularity": {
                    "type": "integer",
                    "description": "Minimum track popularity 0–100",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_audio_features",
        "description": (
            "Get detailed audio features (energy, valence, danceability, tempo, etc.) "
            "for a list of tracks. Use this to evaluate candidate tracks before selecting them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Spotify track IDs (max 100)",
                },
            },
            "required": ["track_ids"],
        },
    },
    {
        "name": "get_user_top_items",
        "description": (
            "Get the user's top tracks or artists for a given time range. "
            "Use this to personalize recommendations based on listening history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_type": {
                    "type": "string",
                    "enum": ["tracks", "artists"],
                    "description": "Whether to fetch top tracks or top artists",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["short_term", "medium_term", "long_term"],
                    "description": (
                        "Time range: short_term (~4 weeks), medium_term (~6 months), "
                        "long_term (several years)"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of items to return (1–50)",
                    "default": 20,
                },
            },
            "required": ["item_type", "time_range"],
        },
    },
    {
        "name": "get_artist_top_tracks",
        "description": "Get the top tracks for a specific artist by their Spotify artist ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "artist_id": {
                    "type": "string",
                    "description": "Spotify artist ID",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of top tracks to return (1–10)",
                    "default": 10,
                },
            },
            "required": ["artist_id"],
        },
    },
    {
        "name": "finalize_playlist",
        "description": (
            "Call this when you have selected the final tracks for the playlist. "
            "This ends the agent loop and creates the playlist. "
            "Only include track IDs you have actually received from tool responses — "
            "never fabricate or guess track IDs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "track_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered list of Spotify track IDs for the playlist",
                },
                "playlist_name": {
                    "type": "string",
                    "description": "A creative, descriptive name for the playlist",
                },
                "playlist_description": {
                    "type": "string",
                    "description": "A brief description of the playlist (shown in Spotify)",
                },
                "reasoning_summary": {
                    "type": "string",
                    "description": (
                        "A concise explanation of how you built the playlist: "
                        "what cues you picked up on, what discovery strategies you used, "
                        "and why these tracks work together."
                    ),
                },
            },
            "required": ["track_ids", "playlist_name", "playlist_description", "reasoning_summary"],
        },
    },
]


# ------------------------------------------------------------------ #
# Prompt builders
# ------------------------------------------------------------------ #

def build_system_prompt(
    user_profile: UserProfile,
    listening_context: UserListeningContext,
) -> str:
    short_artists = ", ".join(a.name for a in listening_context.top_artists_short[:8])
    long_artists = ", ".join(a.name for a in listening_context.top_artists_long[:8])
    short_tracks = ", ".join(
        f"{t.name} by {t.artist_names}" for t in listening_context.top_tracks_short[:8]
    )
    long_tracks = ", ".join(
        f"{t.name} by {t.artist_names}" for t in listening_context.top_tracks_long[:8]
    )
    recent_tracks = ", ".join(
        f"{t.name} by {t.artist_names}" for t in listening_context.recently_played[:8]
    )
    genres = ", ".join(listening_context.favorite_genres[:10]) or "not available"

    return f"""You are a music curator AI helping {user_profile.display_name or "the user"} build a Spotify playlist.

## User's Listening Profile
- **Recent favorites (last ~4 weeks):** {short_artists or "not available"}
- **All-time favorites:** {long_artists or "not available"}
- **Recently played tracks:** {recent_tracks or "not available"}
- **Top tracks (recent):** {short_tracks or "not available"}
- **Top tracks (all-time):** {long_tracks or "not available"}
- **Favorite genres:** {genres}

## Your Approach
1. **Analyze the request** for mood, energy level, genre, tempo, activity, and any explicit artist/track preferences.
2. **Build a 2× candidate pool** before curating — gather at least twice as many tracks as needed, then select the best.
3. **Use `get_recommendations` as your primary discovery tool** — it's the most powerful. Tune `target_energy`, `target_valence`, `target_danceability`, and `target_tempo` based on the request.
4. **Personalize** — reference the user's listening history when appropriate, but also introduce new discoveries.
5. **Never fabricate track IDs** — only use IDs that appeared in tool responses.
6. **Order tracks thoughtfully** — consider energy arc (warm-up → peak → cool-down), genre flow, and tempo transitions.
7. **Call `finalize_playlist`** when you have curated the final selection.

## Constraints
- Total recommendation seeds (tracks + artists + genres combined) must be 1–5.
- Respect explicit content preferences stated in the user's request.
- Aim for variety — avoid repeating artists too many times unless specifically requested.
"""


def build_user_message(request: "PlaylistRequest") -> str:
    explicit_note = "" if request.include_explicit else " (no explicit content)"
    return (
        f"Please create a playlist with {request.target_length} tracks{explicit_note}.\n\n"
        f"Request: {request.user_input}"
    )


# ------------------------------------------------------------------ #
# Agent
# ------------------------------------------------------------------ #

class PlaylistAgent:
    def __init__(self, spotify_client: SpotifyClient) -> None:
        self._spotify = spotify_client
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def run(
        self,
        request: PlaylistRequest,
        user_profile: UserProfile,
        listening_context: UserListeningContext,
        progress_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        system = build_system_prompt(user_profile, listening_context)
        messages: list[dict] = [
            {"role": "user", "content": build_user_message(request)}
        ]
        tool_calls_log: list[ToolCall] = []
        iteration = 0
        max_iterations = settings.agent_max_iterations

        while iteration < max_iterations:
            iteration += 1

            if progress_callback:
                progress_callback(f"Thinking... (iteration {iteration}/{max_iterations})")

            response = self._client.messages.create(
                model=settings.claude_model,
                max_tokens=4096,
                system=system,
                tools=TOOLS,  # type: ignore[arg-type]
                messages=messages,
            )

            # Append assistant message
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "tool_use":
                tool_results = []

                for block in response.content:
                    if block.type != "tool_use":
                        continue

                    tool_name = block.name
                    tool_input = block.input

                    if progress_callback:
                        progress_callback(f"Calling tool: {tool_name}")

                    # Finalize terminates the loop immediately
                    if tool_name == "finalize_playlist":
                        return AgentResult(
                            track_ids=tool_input.get("track_ids", []),
                            playlist_name=tool_input.get("playlist_name", "My Playlist"),
                            playlist_description=tool_input.get("playlist_description", ""),
                            reasoning_summary=tool_input.get("reasoning_summary", ""),
                            tool_calls=tool_calls_log,
                            iterations_used=iteration,
                        )

                    # Dispatch and collect result
                    result_str = self._dispatch_tool(tool_name, tool_input)

                    tool_calls_log.append(
                        ToolCall(
                            tool_name=tool_name,
                            tool_input=tool_input,
                            tool_output=result_str,
                            iteration=iteration,
                        )
                    )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        }
                    )

                # Feed all results back as a single user message
                messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(
            f"Agent did not finalize the playlist within {max_iterations} iterations."
        )

    def _dispatch_tool(self, name: str, inputs: dict) -> str:
        try:
            match name:
                case "search_tracks":
                    tracks = self._spotify.search_tracks(
                        query=inputs["query"],
                        limit=inputs.get("limit", 10),
                    )
                    return json.dumps(
                        [
                            {
                                "id": t.id,
                                "name": t.name,
                                "artists": t.artist_names,
                                "album": t.album_name,
                                "popularity": t.popularity,
                                "explicit": t.explicit,
                                "duration": t.duration_str,
                            }
                            for t in tracks
                        ]
                    )

                case "get_recommendations":
                    tracks = self._spotify.get_recommendations(
                        seed_tracks=inputs.get("seed_tracks"),
                        seed_artists=inputs.get("seed_artists"),
                        seed_genres=inputs.get("seed_genres"),
                        limit=inputs.get("limit", 20),
                        target_energy=inputs.get("target_energy"),
                        target_valence=inputs.get("target_valence"),
                        target_danceability=inputs.get("target_danceability"),
                        target_tempo=inputs.get("target_tempo"),
                        min_popularity=inputs.get("min_popularity"),
                    )
                    return json.dumps(
                        [
                            {
                                "id": t.id,
                                "name": t.name,
                                "artists": t.artist_names,
                                "album": t.album_name,
                                "popularity": t.popularity,
                                "explicit": t.explicit,
                                "duration": t.duration_str,
                            }
                            for t in tracks
                        ]
                    )

                case "get_audio_features":
                    features = self._spotify.get_audio_features(inputs["track_ids"])
                    return json.dumps(
                        [
                            {
                                "id": f.id,
                                "energy": f.energy,
                                "valence": f.valence,
                                "danceability": f.danceability,
                                "tempo": f.tempo,
                                "acousticness": f.acousticness,
                                "instrumentalness": f.instrumentalness,
                                "speechiness": f.speechiness,
                            }
                            for f in features
                        ]
                    )

                case "get_user_top_items":
                    item_type = inputs["item_type"]
                    time_range = inputs["time_range"]
                    limit = inputs.get("limit", 20)
                    if item_type == "tracks":
                        items = self._spotify.get_top_tracks(time_range=time_range, limit=limit)
                        return json.dumps(
                            [
                                {
                                    "id": t.id,
                                    "name": t.name,
                                    "artists": t.artist_names,
                                    "popularity": t.popularity,
                                }
                                for t in items
                            ]
                        )
                    else:
                        items_a = self._spotify.get_top_artists(
                            time_range=time_range, limit=limit
                        )
                        return json.dumps(
                            [
                                {
                                    "id": a.id,
                                    "name": a.name,
                                    "genres": a.genres,
                                    "popularity": a.popularity,
                                }
                                for a in items_a
                            ]
                        )

                case "get_artist_top_tracks":
                    tracks = self._spotify.get_artist_top_tracks(
                        artist_id=inputs["artist_id"],
                        limit=inputs.get("limit", 10),
                    )
                    return json.dumps(
                        [
                            {
                                "id": t.id,
                                "name": t.name,
                                "artists": t.artist_names,
                                "popularity": t.popularity,
                                "explicit": t.explicit,
                                "duration": t.duration_str,
                            }
                            for t in tracks
                        ]
                    )

                case _:
                    return json.dumps({"error": f"Unknown tool: {name}"})

        except Exception as exc:
            return json.dumps({"error": str(exc)})
