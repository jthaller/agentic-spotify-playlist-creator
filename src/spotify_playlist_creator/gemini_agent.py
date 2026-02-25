"""Gemini agentic loop with Spotify tool definitions."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from loguru import logger

from spotify_playlist_creator.logging_setup import log_event

from google import genai
from google.genai import types

from spotify_playlist_creator.config import settings
from spotify_playlist_creator.models import AgentResult, PlaylistRequest, ToolCall, UserListeningContext, UserProfile
from spotify_playlist_creator.spotify_client import SpotifyClient


# ------------------------------------------------------------------ #
# Tool definitions (JSON Schema format)
# ------------------------------------------------------------------ #

_FUNCTION_DECLARATIONS = [
    types.FunctionDeclaration(
        name="search_tracks",
        description=(
            "Search the Spotify catalog for tracks. This is your PRIMARY discovery tool. "
            "Use varied, creative queries to find tracks matching the mood, genre, or activity. "
            "Try queries like 'genre:k-pop upbeat dance 2024', 'artist:IU', 'mellow lo-fi study', "
            "'energetic workout hip-hop'. Make multiple calls with different queries to diversify results. "
            "Returns track IDs, names, artists, and popularity scores."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (supports field filters: artist:, track:, genre:, year:)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (1-50)",
                },
            },
            "required": ["query"],
        },
    ),
    types.FunctionDeclaration(
        name="get_user_top_items",
        description=(
            "Get the user's top tracks or artists for a given time range. "
            "Use this to personalize recommendations based on listening history."
        ),
        parameters={
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
                    "description": "Number of items to return (1-50)",
                },
            },
            "required": ["item_type", "time_range"],
        },
    ),
    types.FunctionDeclaration(
        name="finalize_playlist",
        description=(
            "Call this when you have selected the final tracks for the playlist. "
            "This ends the agent loop and creates the playlist. "
            "Only include track IDs you have actually received from tool responses — "
            "never fabricate or guess track IDs."
        ),
        parameters={
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
    ),
]

GEMINI_TOOLS = [types.Tool(function_declarations=_FUNCTION_DECLARATIONS)]


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
1. **Analyze the request** for mood, genre, activity, and any explicit artist preferences.
2. **Use search_tracks as your primary discovery tool.** Make 3-5 varied searches with different queries to build a diverse candidate pool. Good query patterns:
   - Genre + mood: `"genre:k-pop upbeat dance"`
   - Artist name: `"artist:IU"` or `"artist:Radiohead"`
   - Activity/mood keywords: `"chill lo-fi study beats 2024"`
   - Specific subgenre: `"indie pop dreamy female vocalist"`
3. **Use get_user_top_items** to see the user's top artists/tracks for personalization — then search for those artists by name using search_tracks.
4. **Never fabricate track IDs** — only use IDs copied exactly from tool responses. Made-up IDs will cause API errors.
5. **Order tracks thoughtfully** — consider energy arc, genre flow, and tempo transitions.
6. **Call finalize_playlist as soon as you have enough tracks** — do not keep searching once you have sufficient candidates.

## Constraints
- Respect explicit content preferences stated in the user's request.
- Aim for variety — avoid repeating artists too many times unless specifically requested.
"""


def build_user_message(request: PlaylistRequest) -> str:
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
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def _generate(self, contents, config, max_retries: int = 3):
        """Call generate_content with exponential backoff on 503 UNAVAILABLE."""
        for attempt in range(max_retries):
            try:
                return self._client.models.generate_content(
                    model=settings.gemini_model,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if attempt < max_retries - 1 and "503" in str(exc):
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning("Gemini 503, retrying in {}s (attempt {}/{})", wait, attempt + 1, max_retries)
                    log_event("AGENT", "/agent/generate", status=503,
                              agent=settings.gemini_model,
                              message=f"503 retry attempt {attempt + 1}/{max_retries}")
                    time.sleep(wait)
                else:
                    raise

    def run(
        self,
        request: PlaylistRequest,
        user_profile: UserProfile,
        listening_context: UserListeningContext,
        progress_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        system = build_system_prompt(user_profile, listening_context)
        contents: list[types.Content] = [
            types.Content(
                role="user",
                parts=[types.Part(text=build_user_message(request))],
            )
        ]
        tool_calls_log: list[ToolCall] = []
        iteration = 0
        max_iterations = settings.agent_max_iterations

        while iteration < max_iterations:
            iteration += 1

            if progress_callback:
                progress_callback(f"Thinking... (iteration {iteration}/{max_iterations})")

            # On the last 2 iterations, force finalize_playlist
            forcing_finalize = iteration >= max_iterations - 1 and len(tool_calls_log) > 0
            if forcing_finalize:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part(text=(
                            "You must now call finalize_playlist with the tracks you have gathered. "
                            "No more search or recommendation calls are allowed."
                        ))],
                    )
                )
                tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="ANY",
                        allowed_function_names=["finalize_playlist"],
                    )
                )
            else:
                tool_config = types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(mode="AUTO")
                )

            response = self._generate(
                contents,
                types.GenerateContentConfig(
                    system_instruction=system,
                    tools=GEMINI_TOOLS,
                    tool_config=tool_config,
                ),
            )

            candidate = response.candidates[0]
            contents.append(candidate.content)

            # Collect all function calls from this response
            function_call_parts = [
                p for p in candidate.content.parts if p.function_call is not None
            ]

            # No tool calls — force a finalize on next iteration if we have tracks
            if not function_call_parts:
                if len(tool_calls_log) > 0:
                    # Give the model one forced finalize attempt
                    contents.append(
                        types.Content(
                            role="user",
                            parts=[types.Part(text=(
                                "You stopped without finalizing. "
                                "Call finalize_playlist now with the tracks you have gathered."
                            ))],
                        )
                    )
                    response = self._generate(
                        contents,
                        types.GenerateContentConfig(
                            system_instruction=system,
                            tools=GEMINI_TOOLS,
                            tool_config=types.ToolConfig(
                                function_calling_config=types.FunctionCallingConfig(
                                    mode="ANY",
                                    allowed_function_names=["finalize_playlist"],
                                )
                            ),
                        ),
                    )
                    for part in response.candidates[0].content.parts:
                        if part.function_call and part.function_call.name == "finalize_playlist":
                            args = dict(part.function_call.args)
                            return AgentResult(
                                track_ids=args.get("track_ids", []),
                                playlist_name=args.get("playlist_name", "My Playlist"),
                                playlist_description=args.get("playlist_description", ""),
                                reasoning_summary=args.get("reasoning_summary", ""),
                                tool_calls=tool_calls_log,
                                iterations_used=iteration,
                            )
                break

            # Check for finalize_playlist first
            for part in function_call_parts:
                fc = part.function_call
                if fc.name == "finalize_playlist":
                    args = dict(fc.args)
                    track_ids = args.get("track_ids", [])
                    logger.info(
                        "Agent finalized playlist '{}' | {} tracks | {} iterations | {} tool calls",
                        args.get("playlist_name", "My Playlist"),
                        len(track_ids),
                        iteration,
                        len(tool_calls_log),
                    )
                    log_event(
                        "PLAYLIST", "/playlist/finalize",
                        status=200,
                        agent=settings.gemini_model,
                        message=f"Playlist '{args.get('playlist_name')}' | {len(track_ids)} tracks | {iteration} iterations",
                    )
                    return AgentResult(
                        track_ids=track_ids,
                        playlist_name=args.get("playlist_name", "My Playlist"),
                        playlist_description=args.get("playlist_description", ""),
                        reasoning_summary=args.get("reasoning_summary", ""),
                        tool_calls=tool_calls_log,
                        iterations_used=iteration,
                    )

            # Dispatch all tool calls in parallel
            calls = [(part.function_call.name, dict(part.function_call.args))
                     for part in function_call_parts]

            if progress_callback:
                names = ", ".join(name for name, _ in calls)
                progress_callback(f"Calling tools: {names}")

            results: dict[int, str] = {}
            with ThreadPoolExecutor(max_workers=len(calls)) as pool:
                futures = {
                    pool.submit(self._dispatch_tool, name, inputs): i
                    for i, (name, inputs) in enumerate(calls)
                }
                for future in as_completed(futures):
                    results[futures[future]] = future.result()

            response_parts: list[types.Part] = []
            for i, (tool_name, tool_input) in enumerate(calls):
                result_str = results[i]
                tool_calls_log.append(
                    ToolCall(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        tool_output=result_str,
                        iteration=iteration,
                    )
                )
                try:
                    result_data = json.loads(result_str)
                except Exception:
                    result_data = {"result": result_str}

                response_parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=tool_name,
                            response={"result": result_data},
                        )
                    )
                )

            contents.append(types.Content(role="user", parts=response_parts))

        raise RuntimeError(
            f"Agent did not finalize the playlist within {max_iterations} iterations."
        )

    def _dispatch_tool(self, name: str, inputs: dict) -> str:
        t0 = time.perf_counter()
        try:
            match name:
                case "search_tracks":
                    tracks = self._spotify.search_tracks(
                        query=inputs["query"],
                        limit=int(inputs.get("limit", 10)),
                    )
                    result = json.dumps(
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

                case "get_user_top_items":
                    item_type = inputs["item_type"]
                    time_range = inputs["time_range"]
                    limit = int(inputs.get("limit", 20))
                    if item_type == "tracks":
                        items = self._spotify.get_top_tracks(time_range=time_range, limit=limit)
                        result = json.dumps(
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
                        items_a = self._spotify.get_top_artists(time_range=time_range, limit=limit)
                        result = json.dumps(
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

                case _:
                    result = json.dumps({"error": f"Unknown tool: {name}"})

            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.debug("Tool {} ok | {}ms | {} bytes", name, elapsed_ms, len(result))
            log_event(
                "TOOL", f"/tools/{name}",
                status=200,
                bytes_sent=len(result),
                agent=settings.gemini_model,
                message=f"Tool {name} completed in {elapsed_ms}ms",
            )
            return result

        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            logger.error("Tool {} failed | {}ms | input={} | error={}", name, elapsed_ms, inputs, exc, exception=True)
            log_event(
                "TOOL", f"/tools/{name}",
                status=500,
                agent=settings.gemini_model,
                message=f"Tool {name} failed: {exc}",
            )
            return json.dumps({"error": str(exc)})
