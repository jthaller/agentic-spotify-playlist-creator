"""Streamlit entrypoint — OAuth state machine + playlist creation UI."""

from __future__ import annotations

import os

import spotipy
import streamlit as st
from loguru import logger

from spotify_playlist_creator.logging_setup import setup_logging

# Must run before any other import that logs (spotipy, google-genai, etc.)
setup_logging()

from spotify_playlist_creator.config import settings
from spotify_playlist_creator.logging_setup import log_event
from spotify_playlist_creator.models import AgentResult, Playlist, PlaylistRequest, UserListeningContext, UserProfile
from spotify_playlist_creator.playlist_planner import PlaylistPlanner
from spotify_playlist_creator.spotify_client import make_auth_manager


# ------------------------------------------------------------------ #
# Page config (must be first Streamlit call)
# ------------------------------------------------------------------ #

st.set_page_config(
    page_title="Spotify Playlist Creator",
    page_icon="🎵",
    layout="centered",
)


# ------------------------------------------------------------------ #
# OAuth helpers
# ------------------------------------------------------------------ #

def _get_auth_manager() -> spotipy.oauth2.SpotifyOAuth:
    """Return a cached auth manager (one per session via session_state)."""
    if "auth_manager" not in st.session_state:
        st.session_state.auth_manager = make_auth_manager()
    return st.session_state.auth_manager


def _handle_oauth_callback() -> bool:
    """Exchange the ?code= query param for a token. Returns True if handled."""
    code = st.query_params.get("code")
    if not code:
        return False

    auth_manager = _get_auth_manager()
    token_info = auth_manager.get_access_token(code=code, as_dict=True)
    st.session_state.token_info = token_info
    # Clear the code from the URL immediately to prevent double-processing
    st.query_params.clear()
    log_event("OAUTH", "/oauth/callback", status=200)
    logger.info("OAuth callback complete — token acquired")
    return True


def _try_get_cached_token() -> dict | None:
    """Return cached token from session_state or disk, if valid."""
    auth_manager = _get_auth_manager()

    # Already in session_state
    if "token_info" in st.session_state:
        token_info = st.session_state.token_info
        if auth_manager.is_token_expired(token_info):
            token_info = auth_manager.refresh_access_token(token_info["refresh_token"])
            st.session_state.token_info = token_info
        return token_info

    # Try disk cache
    token_info = auth_manager.get_cached_token()
    if token_info:
        st.session_state.token_info = token_info
        return token_info

    return None


def _render_auth_page() -> None:
    st.title("🎵 Spotify Playlist Creator")
    st.markdown(
        "Describe any playlist in natural language and let Gemini build it for you — "
        "powered by the Spotify API."
    )
    st.divider()

    auth_manager = _get_auth_manager()
    auth_url = auth_manager.get_authorize_url()
    st.link_button("Connect with Spotify", auth_url, type="primary", use_container_width=True)
    st.caption("You'll be redirected to Spotify to authorize this app, then back here.")


def _initialize_spotify(token_info: dict) -> spotipy.Spotify:
    """Return a cached spotipy instance, creating it once per session."""
    if "sp" not in st.session_state:
        access_token = (token_info or {}).get("access_token")
        if not access_token:
            st.error("Spotify authentication failed — no access token. Please log in again.")
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.stop()
        st.session_state.sp = spotipy.Spotify(auth=access_token)
    return st.session_state.sp


# ------------------------------------------------------------------ #
# Session data loading
# ------------------------------------------------------------------ #

def _load_user_data(sp: spotipy.Spotify) -> tuple[UserProfile, UserListeningContext]:
    """Fetch user profile and listening context, cached in session_state."""
    if "user_profile" not in st.session_state:
        with st.spinner("Loading your Spotify profile..."):
            planner = PlaylistPlanner(sp)
            st.session_state.user_profile = planner.get_user_profile()

    if "listening_context" not in st.session_state:
        with st.spinner("Analyzing your listening history..."):
            planner = PlaylistPlanner(sp)
            st.session_state.listening_context = planner.get_listening_context()

    return st.session_state.user_profile, st.session_state.listening_context


# ------------------------------------------------------------------ #
# UI rendering
# ------------------------------------------------------------------ #

def _render_header(user_profile: UserProfile) -> None:
    col1, col2, col3 = st.columns([1, 5, 2])

    with col1:
        if user_profile.image_url:
            st.image(user_profile.image_url, width=48)
        else:
            st.markdown("👤")

    with col2:
        name = user_profile.display_name or user_profile.id
        st.markdown(f"**{name}**")
        st.caption(f"{user_profile.product.title() if user_profile.product else 'Spotify'} account")

    with col3:
        if st.button("Logout", use_container_width=True):
            _logout()


def _logout() -> None:
    """Clear all session state and delete the cache file."""
    log_event("SESSION", "/session/logout", status=200)
    logger.info("User logged out — clearing session state")
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    cache_path = settings.spotify_cache_path
    if os.path.exists(cache_path):
        os.remove(cache_path)
    st.rerun()



def _render_playlist(playlist: Playlist, agent_result: AgentResult) -> None:
    st.success(f"Playlist **{playlist.name}** created!")

    col1, col2 = st.columns([3, 1])
    with col1:
        if playlist.spotify_url:
            st.link_button(
                "Open in Spotify",
                playlist.spotify_url,
                type="primary",
            )

    st.markdown(f"*{playlist.description}*" if playlist.description else "")

    # How Gemini built this
    with st.expander("How Gemini built this playlist", expanded=False):
        st.markdown("### Reasoning")
        st.markdown(agent_result.reasoning_summary)

        st.markdown(f"**Iterations used:** {agent_result.iterations_used}")
        st.markdown(f"**Tool calls:** {len(agent_result.tool_calls)}")

        if agent_result.tool_calls:
            st.markdown("### Tool Call Log")
            for tc in agent_result.tool_calls:
                with st.expander(
                    f"[Iter {tc.iteration}] {tc.tool_name}",
                    expanded=False,
                ):
                    st.json(tc.tool_input)
                    try:
                        import json
                        output_data = json.loads(tc.tool_output)
                        st.json(output_data)
                    except Exception:
                        st.code(tc.tool_output)

    tracks = playlist.tracks
    st.markdown("### Tracks")
    if tracks:
        st.markdown("""
        <style>
        .track-row {
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 10px 4px;
            border-bottom: 1px solid rgba(128,128,128,0.15);
        }
        .track-row:last-child { border-bottom: none; }
        .track-num {
            min-width: 24px;
            text-align: right;
            font-size: 13px;
            color: #888;
            flex-shrink: 0;
        }
        .track-art {
            width: 52px;
            height: 52px;
            border-radius: 4px;
            object-fit: cover;
            flex-shrink: 0;
        }
        .track-art-placeholder {
            width: 52px;
            height: 52px;
            border-radius: 4px;
            background: #333;
            flex-shrink: 0;
        }
        .track-info { flex: 1; min-width: 0; }
        .track-name {
            font-size: 14px;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .track-name a { color: inherit; text-decoration: none; }
        .track-name a:hover { text-decoration: underline; }
        .track-sub {
            font-size: 12px;
            color: #888;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            margin-top: 3px;
        }
        .track-duration {
            font-size: 13px;
            color: #888;
            flex-shrink: 0;
        }
        </style>
        """, unsafe_allow_html=True)

        for i, track in enumerate(tracks, 1):
            art = (
                f'<img class="track-art" src="{track.album_image_url}" />'
                if track.album_image_url
                else '<div class="track-art-placeholder"></div>'
            )
            name = (
                f'<a href="{track.spotify_url}" target="_blank">{track.name}</a>'
                if track.spotify_url
                else track.name
            )
            st.markdown(f"""
            <div class="track-row">
                <span class="track-num">{i}</span>
                {art}
                <div class="track-info">
                    <div class="track-name">{name}</div>
                    <div class="track-sub">{track.artist_names}&nbsp;·&nbsp;{track.album_name}</div>
                </div>
                <span class="track-duration">{track.duration_str}</span>
            </div>
            """, unsafe_allow_html=True)


def _render_main(
    sp: spotipy.Spotify,
    user_profile: UserProfile,
    listening_context: UserListeningContext,
) -> None:
    st.title("🎵 Spotify Playlist Creator")

    _render_header(user_profile)
    st.divider()

    # Show previously created playlist if it exists
    if "created_playlist" in st.session_state and "agent_result" in st.session_state:
        _render_playlist(st.session_state.created_playlist, st.session_state.agent_result)
        st.divider()
        if st.button("Create Another Playlist"):
            del st.session_state["created_playlist"]
            del st.session_state["agent_result"]
            if "playlist_tracks" in st.session_state:
                del st.session_state["playlist_tracks"]
            st.rerun()
        return

    # Input form
    st.markdown("### Describe your playlist")
    user_input = st.text_area(
        "What kind of playlist do you want?",
        placeholder=(
            "e.g. 'Mellow electronic for deep focus, no vocals, around 30 minutes' "
            "or 'Upbeat 90s hip-hop to get pumped before a workout'"
        ),
        height=120,
        label_visibility="collapsed",
    )

    col1, col2 = st.columns(2)
    with col1:
        target_length = st.slider("Number of tracks", min_value=5, max_value=50, value=20, step=1)
    with col2:
        include_explicit = st.checkbox("Include explicit content", value=True)

    if st.button("Create Playlist", type="primary", disabled=not user_input.strip()):
        request = PlaylistRequest(
            user_input=user_input.strip(),
            target_length=target_length,
            include_explicit=include_explicit,
        )

        planner = PlaylistPlanner(sp)
        status_placeholder = st.empty()
        progress_messages: list[str] = []

        def progress_callback(msg: str) -> None:
            progress_messages.append(msg)
            status_placeholder.info(msg)

        try:
            with st.spinner("Gemini is building your playlist..."):
                agent_result, playlist = planner.create_playlist(
                    request=request,
                    user_profile=user_profile,
                    listening_context=listening_context,
                    progress_callback=progress_callback,
                )

            status_placeholder.empty()

            st.session_state["created_playlist"] = playlist
            st.session_state["agent_result"] = agent_result
            st.rerun()

        except Exception as exc:
            status_placeholder.empty()
            st.error(f"Something went wrong: {exc}")


# ------------------------------------------------------------------ #
# Main entrypoint
# ------------------------------------------------------------------ #

def main() -> None:
    # State 2: OAuth callback
    callback_handled = _handle_oauth_callback()

    # State 1/2 → check for valid token
    token_info = _try_get_cached_token()

    if token_info is None:
        # State 1: No token — show login page
        _render_auth_page()
        st.stop()

    # State 3: Valid token — show app
    sp = _initialize_spotify(token_info)
    user_profile, listening_context = _load_user_data(sp)
    _render_main(sp, user_profile, listening_context)


main()
