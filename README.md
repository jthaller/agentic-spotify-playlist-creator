# Agentic Spotify Playlist Creator

A local Streamlit web app that lets you describe a playlist in natural language and have a Gemini AI agent iteratively search Spotify, analyze audio features, and curate the perfect tracklist — then create it directly in your Spotify account.

## How it works

```
You (Streamlit UI)
  │  Natural language request
  ▼
PlaylistPlanner (orchestration)
  │  User profile + listening history + request
  ▼
PlaylistAgent (Gemini agentic loop)
  │  Calls Spotify tools iteratively
  ▼
SpotifyClient (Spotify Web API)
  │
  ▼
Playlist created → URL returned → Displayed in UI
```

Gemini uses 6 tools in a loop:
- **search_tracks** — full-text catalog search
- **get_recommendations** — seed-based audio discovery (most powerful)
- **get_audio_features** — energy, valence, danceability, tempo per track
- **get_user_top_items** — your listening history for personalization
- **get_artist_top_tracks** — deep-cut discovery by artist
- **finalize_playlist** — signals Gemini is done; playlist is created

---

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — fast Python package manager

---

## Setup

### 1. Spotify Developer App

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Click **Create app**
3. Fill in app name and description
4. Under **Redirect URIs**, add: `http://localhost:8501`
5. Save — then copy your **Client ID** and **Client Secret**

### 2. Google Gemini API Key

Get your API key from [Google AI Studio](https://aistudio.google.com/api-keys).

### 3. Install dependencies

```bash
uv sync
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```env
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8501
ANTHROPIC_API_KEY=your_anthropic_api_key
```

Optional settings:
```env
GEMINI_MODEL=Gemini-opus-4-6        # default
AGENT_MAX_ITERATIONS=10             # default
```

### 5. Run the app

```bash
uv run streamlit run src/spotify_playlist_creator/app.py
```

The app opens at [http://localhost:8501](http://localhost:8501).

---

## First login

1. Click **Connect with Spotify**
2. You'll be redirected to Spotify's authorization page
3. Approve the permissions
4. You're redirected back to `http://localhost:8501` — the app loads automatically

Your token is cached to `.spotify_cache` so you won't need to log in again on restart.

---

## Usage

1. Type a playlist description in the text box, e.g.:
   - *"Mellow electronic for deep focus, no vocals, 30 minutes"*
   - *"Upbeat 90s hip-hop for a workout"*
   - *"Jazz-influenced lo-fi for a rainy afternoon, similar to artists I've been listening to recently"*
2. Adjust the track count (5–50) and explicit content preference
3. Click **Create Playlist**
4. Watch Gemini make tool calls in real time
5. Click **Open in Spotify** — your playlist is live!

---

## Logging

Logs are written to the `logs/` directory (created automatically on first run, gitignored).

| File | Format | Contains |
|------|--------|----------|
| `logs/app.log` | Plain text | All application events — debug, info, warnings, errors |
| `logs/access.log` | Apache Combined Log Format | Key events only: tool calls, playlist creation, OAuth, errors |

`app.log` rotates at **10 MB** and keeps 7 compressed backups. `access.log` rotates **daily at midnight**.

**Tail logs while the app is running:**
```bash
tail -f logs/app.log
```

**View only warnings and errors:**
```bash
grep -E 'WARNING|ERROR' logs/app.log
```

**GoAccess — interactive dashboard in the terminal:**
```bash
goaccess logs/access.log --log-format=COMBINED
```

**GoAccess — export an HTML report:**
```bash
goaccess logs/access.log --log-format=COMBINED -o report.html && open report.html
```

The GoAccess "Requests" panel shows tool call frequency, "Status Codes" shows success vs error rate, and "Hosts" shows active users (once a Spotify user ID is available after login).

---

## Project structure

```
agentic-spotify-playlist-creator/
├── pyproject.toml
├── .env.example
└── src/
    └── spotify_playlist_creator/
        ├── config.py           # Environment variable loading (pydantic-settings)
        ├── models.py           # Pydantic v2 domain models
        ├── spotify_client.py   # Spotipy wrapper + OAuth factory
        ├── gemini_agent.py     # Tool schemas + Gemini agentic loop
        ├── playlist_planner.py # Orchestration layer
        ├── logging_setup.py    # Loguru configuration, log rotation, GoAccess sink
        └── app.py              # Streamlit UI + OAuth state machine
```

---

## Security notes

- `.env` and `.spotify_cache` are gitignored — never commit your credentials
- The Spotify OAuth token is stored locally on disk and in session state only
- The app requests these Spotify scopes: `user-read-private`, `user-top-read`, `user-read-recently-played`, `playlist-modify-public`, `playlist-modify-private`
