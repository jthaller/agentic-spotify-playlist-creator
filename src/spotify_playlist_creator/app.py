"""Streamlit entrypoint — OAuth state machine + playlist creation UI."""

from __future__ import annotations

import json
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
    page_icon=":material/queue_music:",
    layout="centered",
)

# SF Pro on macOS/iOS; graceful fallback chain on other platforms.
st.markdown(
    """
    <style>
    html, body, [class*="css"] {
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
                     "SF Pro Display", "Segoe UI", Roboto, Helvetica, Arial,
                     sans-serif;
        -webkit-font-smoothing: antialiased;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------ #
# Design constants
# ------------------------------------------------------------------ #

# Official Spotify icon SVG (three sound-wave arcs in a green circle).
# Rendered via st.html() — no external network requests needed.
_SPOTIFY_ICON_SVG = """
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#1DB954"
     width="{size}px" height="{size}px" aria-label="Spotify">
  <path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521
    17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122
    -.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42
    .18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58
    -11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15
    10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16
    9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28
    -1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z"/>
</svg>
"""

# Shared track-list CSS injected once per playlist render.
_TRACK_LIST_CSS = """
<style>
.track-row {
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 10px 8px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    border-radius: 6px;
    transition: background 0.15s ease;
}
.track-row:hover { background: rgba(255,255,255,0.05); }
.track-row:last-child { border-bottom: none; }
.track-num {
    min-width: 24px;
    text-align: right;
    font-size: 13px;
    color: #1DB954;
    font-weight: 600;
    flex-shrink: 0;
}
.track-art {
    width: 64px;
    height: 64px;
    border-radius: 8px;
    object-fit: cover;
    flex-shrink: 0;
}
.track-art-placeholder {
    width: 64px;
    height: 64px;
    border-radius: 8px;
    background: #282828;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
}
.track-info { flex: 1; min-width: 0; }
.track-name {
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    display: flex;
    align-items: center;
    gap: 6px;
}
.track-name a { color: inherit; text-decoration: none; }
.track-name a:hover { color: #1DB954; }
.track-sub {
    font-size: 12px;
    color: #aaa;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 3px;
}
.track-duration {
    font-size: 13px;
    color: #aaa;
    flex-shrink: 0;
}
.explicit-badge {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.03em;
    border: 1px solid #777;
    color: #777;
    border-radius: 3px;
    padding: 1px 4px;
    flex-shrink: 0;
    line-height: 1.4;
}
</style>
"""


# ------------------------------------------------------------------ #
# OAuth helpers  (NO CHANGES from original)
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
# Session data loading  (NO CHANGES from original)
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

def _render_auth_page() -> None:
    # Narrow centre column so content doesn't stretch full width
    _, col, _ = st.columns([1, 2, 1])
    with col:
        # Spotify logo — large, centred
        st.html(
            f'<div style="text-align:center;padding:32px 0 16px">'
            f'{_SPOTIFY_ICON_SVG.format(size=72)}'
            f'</div>'
        )
        st.html('<h1 style="text-align:center;margin:0 0 8px;font-size:1.8rem">Spotify Playlist Creator</h1>')
        st.html(
            '<p style="text-align:center;color:#aaa;margin:0 0 28px;font-size:0.95rem">'
            'Describe any playlist in plain English.<br>Gemini builds it, Spotify plays it.'
            '</p>'
        )
        st.divider()

        auth_manager = _get_auth_manager()
        auth_url = auth_manager.get_authorize_url()
        st.link_button(
            "Connect with Spotify",
            auth_url,
            type="primary",
            use_container_width=True,
        )
        st.html(
            '<p style="text-align:center;color:#777;font-size:0.8rem;margin-top:10px">'
            "You'll be redirected to Spotify to authorize, then back here."
            '</p>'
        )


def _render_header(user_profile: UserProfile) -> None:
    col1, col2, col3 = st.columns([1, 5, 2])

    with col1:
        if user_profile.image_url:
            # Circular avatar via inline HTML
            st.html(
                f'<img src="{user_profile.image_url}" '
                f'style="width:44px;height:44px;border-radius:50%;object-fit:cover;'
                f'border:2px solid #1DB954;margin-top:4px" />'
            )
        else:
            st.html(
                '<div style="width:44px;height:44px;border-radius:50%;background:#282828;'
                'display:flex;align-items:center;justify-content:center;'
                'border:2px solid #333;margin-top:4px;font-size:20px">👤</div>'
            )

    with col2:
        name = user_profile.display_name or user_profile.id
        product = user_profile.product or ""
        if product.lower() == "premium":
            badge_color, badge_bg = "#1DB954", "rgba(29,185,84,0.15)"
            badge_label = "Premium"
        else:
            badge_color, badge_bg = "#aaa", "rgba(255,255,255,0.08)"
            badge_label = product.title() if product else "Spotify"

        st.html(
            f'<div style="padding-top:4px">'
            f'<span style="font-weight:700;font-size:0.95rem">{name}</span>&nbsp;'
            f'<span style="font-size:0.72rem;background:{badge_bg};color:{badge_color};'
            f'border:1px solid {badge_color};border-radius:20px;padding:2px 8px;'
            f'vertical-align:middle">{badge_label}</span>'
            f'</div>'
        )

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
    # ── Playlist header ──────────────────────────────────────────────
    with st.container(border=True):
        # Spotify logo + playlist name on same line
        logo_html = _SPOTIFY_ICON_SVG.format(size=22)
        st.html(
            f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
            f'{logo_html}'
            f'<span style="font-size:1.3rem;font-weight:700">{playlist.name}</span>'
            f'</div>'
        )
        if playlist.description:
            st.markdown(f"*{playlist.description}*")

        btn_col, _ = st.columns([2, 3])
        with btn_col:
            if playlist.spotify_url:
                st.link_button("Open in Spotify", playlist.spotify_url, type="primary", use_container_width=True)

    # ── Stats row ────────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3)
    m1.metric("Tracks", len(playlist.tracks))
    m2.metric("Agent Iterations", agent_result.iterations_used)
    m3.metric("Tool Calls", len(agent_result.tool_calls))

    # ── How Gemini built this ────────────────────────────────────────
    with st.expander("How Gemini built this playlist", expanded=False):
        st.markdown("**Reasoning**")
        st.markdown(agent_result.reasoning_summary)

        if agent_result.tool_calls:
            st.markdown("**Tool Call Log**")
            for tc in agent_result.tool_calls:
                with st.expander(f"[Iter {tc.iteration}] `{tc.tool_name}`", expanded=False):
                    st.markdown("**Input**")
                    st.json(tc.tool_input)
                    st.markdown("**Output**")
                    try:
                        st.json(json.loads(tc.tool_output))
                    except Exception:
                        st.code(tc.tool_output)

    # ── Track list ───────────────────────────────────────────────────
    tracks = playlist.tracks
    st.markdown("### Tracks")
    if tracks:
        st.markdown(_TRACK_LIST_CSS, unsafe_allow_html=True)

        for i, track in enumerate(tracks, 1):
            art = (
                f'<img class="track-art" src="{track.album_image_url}" />'
                if track.album_image_url
                else '<div class="track-art-placeholder">♪</div>'
            )
            track_label = (
                f'<a href="{track.spotify_url}" target="_blank">{track.name}</a>'
                if track.spotify_url
                else track.name
            )
            explicit_badge = '<span class="explicit-badge">E</span>' if track.explicit else ""
            st.markdown(
                f'<div class="track-row">'
                f'  <span class="track-num">{i}</span>'
                f'  {art}'
                f'  <div class="track-info">'
                f'    <div class="track-name">{track_label}{explicit_badge}</div>'
                f'    <div class="track-sub">{track.artist_names}&nbsp;·&nbsp;{track.album_name}</div>'
                f'  </div>'
                f'  <span class="track-duration">{track.duration_str}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        # Total duration footer
        total_ms = sum(t.duration_ms for t in tracks)
        total_min = total_ms // 60000
        hrs, mins = divmod(total_min, 60)
        duration_str = f"{hrs} hr {mins} min" if hrs else f"{mins} min"
        st.html(
            f'<div style="text-align:right;color:#777;font-size:0.8rem;margin-top:8px">'
            f'{len(tracks)} tracks · {duration_str}'
            f'</div>'
        )


def _render_main(
    sp: spotipy.Spotify,
    user_profile: UserProfile,
    listening_context: UserListeningContext,
) -> None:
    # Page header — Spotify logo + title inline
    logo_html = _SPOTIFY_ICON_SVG.format(size=28)
    st.html(
        f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">'
        f'{logo_html}'
        f'<span style="font-size:1.6rem;font-weight:700">Spotify Playlist Creator</span>'
        f'</div>'
    )

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

    # ── Input form ───────────────────────────────────────────────────
    with st.container(border=True):
        user_input = st.text_area(
            "Describe your playlist",
            placeholder=(
                "e.g. 'Mellow electronic for deep focus, no vocals, around 30 minutes' "
                "or 'Upbeat 90s hip-hop to get pumped before a workout'"
            ),
            height=120,
        )

        col1, col2 = st.columns(2)
        with col1:
            target_length = st.slider("Number of tracks", min_value=5, max_value=50, value=20, step=1)
        with col2:
            include_explicit = st.checkbox("Include explicit content", value=True)

        if st.button("Create Playlist", type="primary", disabled=not user_input.strip(), use_container_width=True):
            request = PlaylistRequest(
                user_input=user_input.strip(),
                target_length=target_length,
                include_explicit=include_explicit,
            )

            planner = PlaylistPlanner(sp)

            try:
                with st.status("Gemini is building your playlist...", expanded=True) as status:
                    def progress_callback(msg: str) -> None:
                        status.write(msg)

                    agent_result, playlist = planner.create_playlist(
                        request=request,
                        user_profile=user_profile,
                        listening_context=listening_context,
                        progress_callback=progress_callback,
                    )
                    status.update(label="Playlist ready", state="complete", expanded=False)

                st.session_state["created_playlist"] = playlist
                st.session_state["agent_result"] = agent_result
                st.rerun()

            except Exception as exc:
                st.error(f"Something went wrong: {exc}")


# ------------------------------------------------------------------ #
# Main entrypoint  (NO CHANGES from original)
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
