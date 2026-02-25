"""Centralised logging configuration using Loguru.

Architecture
────────────
Three sinks are created on setup_logging():

  stdout          Coloured, human-readable.  Good for local dev.
  logs/app.log    Rolling plain-text file.  Rotate by size; keep N backups
                  compressed with gzip.  Use this for debugging.
  logs/access.log Apache Combined Log Format (CLF).  GoAccess can parse this
                  natively with --log-format=COMBINED.  Each "request" is an
                  application event (tool call, playlist create, OAuth, …).

Usage
─────
Call setup_logging() once at process start (app.py does this).  All other
modules just do:

    from loguru import logger
    logger.info("something happened")

To emit a GoAccess-visible event call log_event():

    from spotify_playlist_creator.logging_setup import log_event
    log_event("TOOL", "/tools/search_tracks", status=200, bytes_sent=len(payload),
              user=user_id, agent="gemini-2.5-pro")

Stdlib → Loguru bridge
──────────────────────
The InterceptHandler installs itself as the root stdlib handler so that any
third-party library that uses logging.getLogger() (spotipy, httpx, google-
genai) is captured and routed through Loguru instead of printing to stderr
independently.

Log rotation
────────────
Loguru's built-in rotation/retention/compression is used (simpler than
wiring stdlib RotatingFileHandler as a Loguru sink).  If you need to pass an
existing RotatingFileHandler to Loguru, use:

    handler = RotatingFileHandler("app.log", maxBytes=10_000_000, backupCount=5)
    logger.add(handler.stream, format="{message}")   # raw message only

But the native approach below is strongly preferred.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from loguru import logger

# ─────────────────────────────────────────────────────────────────────────────
# Log directory (relative to cwd — the project root when running via uv)
# ─────────────────────────────────────────────────────────────────────────────

LOG_DIR = Path("logs")

# ─────────────────────────────────────────────────────────────────────────────
# Format strings
# ─────────────────────────────────────────────────────────────────────────────

# Coloured output for stdout.
_CONSOLE_FMT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>"
)

# Plain text for the rolling app log.  Same fields as console, no ANSI codes.
_FILE_FMT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{name}:{function}:{line} | {message}"
)

# Apache Combined Log Format for GoAccess.
# Field mapping — application concept → CLF field:
#   host   → module name or Spotify user ID
#   user   → Spotify user ID (or "-")
#   method → application verb: TOOL, AGENT, OAUTH, PLAYLIST, …
#   path   → resource path:  /tools/search_tracks, /playlist/create, …
#   status → 200 success | 400 bad input | 500 error | 503 retry
#   bytes  → response payload size (0 when not applicable)
#   agent  → model or component name (e.g. "gemini-2.5-pro")
#
# GoAccess command to parse this file:
#   goaccess logs/access.log --log-format=COMBINED -o report.html
_CLF_FMT = (
    '{extra[clf_host]} - {extra[clf_user]} [{time:DD/MMM/YYYY:HH:mm:ss +0000}] '
    '"{extra[clf_method]} {extra[clf_path]} HTTP/1.1" '
    '{extra[clf_status]} {extra[clf_bytes]} '
    '"-" "{extra[clf_agent]}"'
)

# ─────────────────────────────────────────────────────────────────────────────
# Filters — keep CLF records out of the human-readable sinks and vice-versa
# ─────────────────────────────────────────────────────────────────────────────

def _is_clf(record: dict) -> bool:
    return "clf_host" in record["extra"]

def _not_clf(record: dict) -> bool:
    return "clf_host" not in record["extra"]

# ─────────────────────────────────────────────────────────────────────────────
# Stdlib → Loguru bridge
# ─────────────────────────────────────────────────────────────────────────────

class _InterceptHandler(logging.Handler):
    """Re-routes all stdlib logging records into Loguru.

    Install once with:
        logging.root.handlers = [_InterceptHandler()]
        logging.root.setLevel(logging.DEBUG)

    After that, logging.getLogger("spotipy").warning("x") ends up in Loguru
    with the correct source location and level.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Walk up the call stack to find the real call site, skipping
        # frames that belong to the stdlib logging machinery.
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back  # type: ignore[assignment]
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )

# ─────────────────────────────────────────────────────────────────────────────
# One-time setup guard (Streamlit reruns the entire script on each interaction)
# ─────────────────────────────────────────────────────────────────────────────

_configured = False

# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(
    *,
    log_level: str = "DEBUG",
    log_dir: Path = LOG_DIR,
    max_size: str = "10 MB",
    retention: int = 7,
    console: bool = True,
) -> None:
    """Configure Loguru sinks and bridge stdlib logging into Loguru.

    Call once at process start (before any other imports that log).  Safe to
    call multiple times — subsequent calls are no-ops.

    Args:
        log_level:  Minimum level for stdout and app.log ("DEBUG", "INFO", …).
        log_dir:    Directory for log files; created if it doesn't exist.
        max_size:   Rotation threshold for app.log ("10 MB", "50 MB", …).
        retention:  Number of compressed backup files to keep per sink.
        console:    Emit to stdout (set False in CI/production if capturing).
    """
    global _configured
    if _configured:
        return
    _configured = True

    log_dir.mkdir(parents=True, exist_ok=True)

    # Remove Loguru's default stderr handler before adding our own sinks.
    logger.remove()

    # ── Sink 1: coloured stdout ──────────────────────────────────────────────
    if console:
        logger.add(
            sys.stdout,
            level=log_level,
            format=_CONSOLE_FMT,
            filter=_not_clf,
            colorize=True,
            # enqueue=True would be thread-safe but adds latency; False is fine
            # for Streamlit (single-threaded reruns + ThreadPoolExecutor workers
            # where we only log after futures resolve).
            enqueue=False,
        )

    # ── Sink 2: rolling app.log ──────────────────────────────────────────────
    # Rotates when the file reaches `max_size`.  Old files are gzip-compressed
    # and only `retention` copies are kept.  This replaces RotatingFileHandler.
    logger.add(
        log_dir / "app.log",
        level=log_level,
        format=_FILE_FMT,
        filter=_not_clf,
        rotation=max_size,       # e.g. "10 MB"
        retention=retention,     # keep last N rotated files
        compression="gz",        # compress rotated files automatically
        enqueue=False,
    )

    # ── Sink 3: access.log (Apache CLF for GoAccess) ─────────────────────────
    # Rotates daily at midnight so GoAccess date-based reports are clean.
    logger.add(
        log_dir / "access.log",
        level="INFO",
        format=_CLF_FMT,
        filter=_is_clf,
        rotation="00:00",        # rotate daily at midnight
        retention=retention,
        compression="gz",
        enqueue=False,
    )

    # ── Silence chatty third-party libraries ─────────────────────────────────
    for name in ("httpx", "httpcore", "urllib3", "google", "spotipy", "googleapiclient"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # ── Bridge: stdlib logging → Loguru ──────────────────────────────────────
    # Any library that calls logging.getLogger("foo").info("bar") will now
    # flow through Loguru instead of printing to stderr directly.
    logging.root.handlers = [_InterceptHandler()]
    logging.root.setLevel(logging.DEBUG)

    logger.info("Logging configured | log_dir={} level={} rotation={} retention={}",
                log_dir, log_level, max_size, retention)


def log_event(
    method: str,
    path: str,
    *,
    status: int = 200,
    bytes_sent: int = 0,
    host: str = "127.0.0.1",
    user: str = "-",
    agent: str = "spotify-playlist-creator",
    message: str = "",
) -> None:
    """Emit one Apache Combined Log Format record to logs/access.log.

    GoAccess will parse this file natively with --log-format=COMBINED and
    produce dashboards showing:
      • Request frequency over time  (tool calls per minute/hour)
      • Top "requests"               (most-used tools)
      • Status code breakdown        (success vs error rate)
      • Top "hosts"                  (active users or modules)

    Application → CLF field mapping
    ────────────────────────────────
    method      Application verb.  Use one of:
                  TOOL      — Spotify tool call (search_tracks, etc.)
                  AGENT     — agent iteration
                  OAUTH     — OAuth flow step
                  PLAYLIST  — playlist create/update
                  SESSION   — user login/logout

    path        Resource being accessed.  Examples:
                  /tools/search_tracks
                  /tools/get_user_top_items
                  /playlist/create
                  /oauth/callback

    status      HTTP-style code:
                  200  success
                  400  bad/missing input
                  500  unhandled exception
                  503  upstream service retry (Gemini, Spotify API)

    bytes_sent  Payload size in bytes (use len(json_str) for tool responses).
    host        Spotify user ID or module name.
    user        Spotify user ID (same as host if available, else "-").
    agent       Model or component name (e.g. "gemini-2.5-pro").
    message     Optional free-text note logged to app.log alongside the CLF record.

    Example
    ────────
    log_event(
        "TOOL", "/tools/search_tracks",
        status=200, bytes_sent=len(result_json),
        user=user_id, agent=settings.gemini_model,
    )
    """
    logger.bind(
        clf_host=host,
        clf_user=user,
        clf_method=method,
        clf_path=path,
        clf_status=status,
        clf_bytes=bytes_sent,
        clf_agent=agent,
    ).info(message or f"{method} {path} → {status}")
