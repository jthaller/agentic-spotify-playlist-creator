"""Spotipy wrapper providing typed access to the Spotify Web API."""

from __future__ import annotations

import re
from collections import Counter

import requests as _requests

from loguru import logger

import spotipy
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import MemoryCacheHandler

from .config import settings, SPOTIFY_SCOPES
from .models import (
    Artist,
    AudioFeatures,
    Playlist,
    Track,
    UserListeningContext,
    UserProfile,
)


def make_auth_manager(cache_handler=None) -> SpotifyOAuth:
    """Build a SpotifyOAuth manager. Token kept in memory only (no shared disk cache)."""
    if cache_handler is None:
        cache_handler = MemoryCacheHandler()
    return SpotifyOAuth(
        client_id=settings.spotify_client_id,
        client_secret=settings.spotify_client_secret,
        redirect_uri=settings.spotify_redirect_uri,
        scope=SPOTIFY_SCOPES,
        cache_handler=cache_handler,
        open_browser=False,
        show_dialog=False,
    )


class SpotifyClient:
    """High-level wrapper around spotipy.Spotify returning typed Pydantic models."""

    def __init__(self, sp: spotipy.Spotify) -> None:
        self._sp = sp

    # ------------------------------------------------------------------ #
    # User
    # ------------------------------------------------------------------ #

    def get_current_user(self) -> UserProfile:
        data = self._sp.current_user()
        return UserProfile(
            id=data["id"],
            display_name=data.get("display_name") or "",
            email=data.get("email") or "",
            country=data.get("country") or "",
            product=data.get("product") or "",
            image_url=(data.get("images") or [{}])[0].get("url") if data.get("images") else None,
            followers=data.get("followers", {}).get("total", 0),
        )

    # ------------------------------------------------------------------ #
    # Top items
    # ------------------------------------------------------------------ #

    def get_top_tracks(
        self,
        time_range: str = "medium_term",
        limit: int = 20,
    ) -> list[Track]:
        data = self._sp.current_user_top_tracks(time_range=time_range, limit=limit)
        return [self._parse_track(item) for item in (data.get("items") or [])]

    def get_top_artists(
        self,
        time_range: str = "medium_term",
        limit: int = 20,
    ) -> list[Artist]:
        data = self._sp.current_user_top_artists(time_range=time_range, limit=limit)
        return [self._parse_artist(item) for item in (data.get("items") or [])]

    def get_recently_played(self, limit: int = 20) -> list[Track]:
        data = self._sp.current_user_recently_played(limit=limit)
        return [self._parse_track(item["track"]) for item in (data.get("items") or [])]

    # ------------------------------------------------------------------ #
    # Search & Discovery
    # ------------------------------------------------------------------ #

    def search_tracks(self, query: str, limit: int = 10) -> list[Track]:
        data = self._sp.search(q=query, type="track", limit=limit, market="from_token")
        items = data.get("tracks", {}).get("items") or []
        return [self._parse_track(item) for item in items]

    def get_recommendations(
        self,
        seed_tracks: list[str] | None = None,
        seed_artists: list[str] | None = None,
        seed_genres: list[str] | None = None,
        limit: int = 20,
        target_energy: float | None = None,
        target_valence: float | None = None,
        target_danceability: float | None = None,
        target_tempo: float | None = None,
        min_popularity: int | None = None,
    ) -> list[Track]:
        kwargs: dict = {"limit": limit}
        if seed_tracks:
            kwargs["seed_tracks"] = seed_tracks[:5]
        if seed_artists:
            kwargs["seed_artists"] = seed_artists[:5]
        if seed_genres:
            kwargs["seed_genres"] = seed_genres[:5]
        if target_energy is not None:
            kwargs["target_energy"] = target_energy
        if target_valence is not None:
            kwargs["target_valence"] = target_valence
        if target_danceability is not None:
            kwargs["target_danceability"] = target_danceability
        if target_tempo is not None:
            kwargs["target_tempo"] = target_tempo
        if min_popularity is not None:
            kwargs["min_popularity"] = min_popularity

        data = self._sp.recommendations(**kwargs)
        return [self._parse_track(item) for item in (data.get("tracks") or [])]

    def get_audio_features(self, track_ids: list[str]) -> list[AudioFeatures]:
        results: list[AudioFeatures] = []
        for i in range(0, len(track_ids), 100):
            chunk = track_ids[i : i + 100]
            data = self._sp.audio_features(chunk) or []
            for item in data:
                if item:
                    results.append(
                        AudioFeatures(
                            id=item["id"],
                            danceability=item.get("danceability", 0.0),
                            energy=item.get("energy", 0.0),
                            valence=item.get("valence", 0.0),
                            tempo=item.get("tempo", 0.0),
                            acousticness=item.get("acousticness", 0.0),
                            instrumentalness=item.get("instrumentalness", 0.0),
                            speechiness=item.get("speechiness", 0.0),
                            loudness=item.get("loudness", 0.0),
                            mode=item.get("mode", 0),
                            key=item.get("key", 0),
                        )
                    )
        return results

    def get_artist_top_tracks(
        self,
        artist_id: str,
        limit: int = 10,
    ) -> list[Track]:
        # Bypass spotipy's artist_top_tracks wrapper which still sends deprecated
        # `country` param — Spotify API now requires `market` (changed Nov 2024)
        trid = self._sp._get_id("artist", artist_id)
        data = self._sp._get(f"artists/{trid}/top-tracks", market="from_token")
        tracks = data.get("tracks") or []
        return [self._parse_track(t) for t in tracks[:limit]]

    def get_related_artists(self, artist_id: str) -> list[Artist]:
        data = self._sp.artist_related_artists(artist_id)
        return [self._parse_artist(a) for a in (data.get("artists") or [])]

    # ------------------------------------------------------------------ #
    # Playlist creation
    # ------------------------------------------------------------------ #

    def create_playlist(
        self,
        user_id: str,
        name: str,
        description: str,
        track_ids: list[str],
        public: bool = False,
    ) -> Playlist:
        # Use POST /v1/me/playlists (modern endpoint) instead of the legacy
        # /v1/users/{user_id}/playlists to avoid user_id mismatch 403s.
        pl = self._sp._post(
            "me/playlists",
            payload={"name": name, "public": public, "description": description},
        )

        # Add tracks via /items (not /tracks — the /tracks endpoint is
        # forbidden for newer Spotify apps; spotipy hasn't been updated yet).
        headers = {**self._sp._get_auth_headers(), "Content-Type": "application/json"}
        # Normalize IDs — the model sometimes passes full URIs instead of bare IDs
        bare_ids = [
            tid[len("spotify:track:"):] if tid.startswith("spotify:track:") else tid.strip()
            for tid in track_ids
        ]

        # Drop hallucinated IDs before hitting the API.
        # Spotify track IDs are exactly 22 base62 characters (0-9, a-z, A-Z).
        _VALID_ID = re.compile(r'^[0-9A-Za-z]{22}$')
        valid_ids, bad_ids = [], []
        for tid in bare_ids:
            (valid_ids if _VALID_ID.match(tid) else bad_ids).append(tid)
        if bad_ids:
            logger.warning("Dropping {} invalid track ID(s) — likely hallucinated by the model: {}",
                           len(bad_ids), bad_ids)

        uris = [f"spotify:track:{tid}" for tid in valid_ids]
        for i in range(0, len(uris), 100):
            resp = _requests.post(
                f"https://api.spotify.com/v1/playlists/{pl['id']}/items",
                json={"uris": uris[i : i + 100]},
                headers=headers,
            )
            if not resp.ok:
                logger.error("Add items failed {} | uris={} | response={}",
                             resp.status_code, uris[i : i + 100], resp.text)
                resp.raise_for_status()

        # Fetch full details
        playlist_data = self._sp.playlist(pl["id"])
        return self._parse_playlist(playlist_data, valid_ids)

    # ------------------------------------------------------------------ #
    # Listening context
    # ------------------------------------------------------------------ #

    def build_listening_context(self) -> UserListeningContext:
        top_tracks_short = self.get_top_tracks(time_range="short_term", limit=20)
        top_tracks_long = self.get_top_tracks(time_range="long_term", limit=20)
        top_artists_short = self.get_top_artists(time_range="short_term", limit=20)
        top_artists_long = self.get_top_artists(time_range="long_term", limit=20)
        recently_played = self.get_recently_played(limit=20)
        favorite_genres = self._infer_favorite_genres(top_artists_short, top_artists_long)

        return UserListeningContext(
            top_tracks_short=top_tracks_short,
            top_tracks_long=top_tracks_long,
            top_artists_short=top_artists_short,
            top_artists_long=top_artists_long,
            recently_played=recently_played,
            favorite_genres=favorite_genres,
        )

    def _infer_favorite_genres(
        self,
        top_artists_short: list[Artist],
        top_artists_long: list[Artist],
    ) -> list[str]:
        counter: Counter = Counter()
        for artist in top_artists_short + top_artists_long:
            for genre in artist.genres:
                counter[genre] += 1
        return [genre for genre, _ in counter.most_common(10)]

    # ------------------------------------------------------------------ #
    # Private parsers
    # ------------------------------------------------------------------ #

    def _parse_track(self, data: dict) -> Track:
        artists = [self._parse_artist(a) for a in (data.get("artists") or [])]
        album = data.get("album") or {}
        images = album.get("images") or []
        album_image_url = images[0].get("url") if images else None
        ext_urls = data.get("external_urls") or {}
        return Track(
            id=data["id"],
            name=data.get("name", ""),
            artists=artists,
            album_name=album.get("name", ""),
            album_image_url=album_image_url,
            duration_ms=data.get("duration_ms", 0),
            popularity=data.get("popularity", 0),
            explicit=data.get("explicit", False),
            preview_url=data.get("preview_url"),
            spotify_url=ext_urls.get("spotify"),
        )

    def _parse_artist(self, data: dict) -> Artist:
        images = data.get("images") or []
        image_url = images[0].get("url") if images else None
        ext_urls = data.get("external_urls") or {}
        return Artist(
            id=data["id"],
            name=data.get("name", ""),
            genres=data.get("genres") or [],
            popularity=data.get("popularity", 0),
            image_url=image_url,
            spotify_url=ext_urls.get("spotify"),
        )

    def _parse_playlist(self, data: dict, track_ids: list[str]) -> Playlist:
        ext_urls = data.get("external_urls") or {}
        images = data.get("images") or []
        image_url = images[0].get("url") if images else None
        owner = data.get("owner") or {}
        # Spotify API changed: field was renamed from "tracks" to "items".
        # Both are paging objects; we need the inner "items" list from whichever is present.
        track_items = (
            (data.get("items") or {}).get("items")
            or (data.get("tracks") or {}).get("items")
            or []
        )
        tracks = []
        for item in track_items:
            track_data = item.get("item") or item.get("track")
            if isinstance(track_data, dict) and track_data.get("id"):
                tracks.append(self._parse_track(track_data))
        return Playlist(
            id=data["id"],
            name=data.get("name", ""),
            description=data.get("description", ""),
            tracks=tracks,
            spotify_url=ext_urls.get("spotify"),
            image_url=image_url,
            owner=owner.get("display_name") or owner.get("id", ""),
            public=data.get("public", False),
        )
