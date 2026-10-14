"""Opt-in direct HTTPS audio stream resolver for publicly playable SoundCloud tracks.

This does not fetch a whole audio file into RAM. PyTgCalls/FFmpeg reads
small network chunks from the authorized media CDN. No access controls,
DRM, sign-in checks, or private-network URL restrictions are bypassed.
Fallback to existing, already-working local media if direct playback fails.
"""
import threading
import time

from player_visuals import (
    allowed_audio_cdn, save_artwork, save_stream_headers,
)


def resolve_soundcloud_https(
    webpage: str, requested_title: str, *,
    cancel_signal: threading.Event | None = None, check=None,
) -> tuple[str, str, float | None] | None:
    from urllib.parse import urlsplit
    from yt_dlp import YoutubeDL

    if cancel_signal is not None and cancel_signal.is_set():
        raise InterruptedError("Stream lookup cancelled")
    parsed = urlsplit(webpage)
    if parsed.scheme != "https" or parsed.hostname not in {"soundcloud.com", "www.soundcloud.com"}:
        return None
    started = time.monotonic()
    with YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 8,
        "retries": 0,
        "fragment_retries": 0,
        "format": "bestaudio/best",
    }) as ydl:
        info = ydl.extract_info(webpage, download=False)
    if cancel_signal is not None and cancel_signal.is_set():
        raise InterruptedError("Stream lookup cancelled")
    if not isinstance(info, dict) or info.get("is_drm"):
        return None
    if str(info.get("availability", "")).lower() in {"private", "subscriber_only", "needs_auth", "premium_only"}:
        return None
    if str(info.get("access", "playable")).lower() not in {"playable", "full", ""}:
        return None
    duration = info.get("duration")
    try:
        duration = float(duration) if duration is not None else None
        if duration is not None and not (1 <= duration <= 600):
            return None
    except (TypeError, ValueError):
        return None
    title = str(info.get("title") or "")[:85]
    artist = str(info.get("uploader") or "")[:75]
    if not title or (check is not None and not check(title, requested_title, artist)):
        return None

    # HLS playlists and HTTPS media are passed to PyTgCalls directly.
    # Only public SoundCloud media CDN destinations are allowed.
    choices = []
    for entry in info.get("requested_formats") or []:
        if isinstance(entry, dict):
            choices.append(entry)
    choices.append(info)
    for entry in choices:
        url = entry.get("url")
        if not allowed_audio_cdn(url):
            continue
        protocol = str(entry.get("protocol") or info.get("protocol") or "").lower()
        if protocol and protocol not in {"https", "http", "m3u8", "m3u8_native"}:
            continue
        headers = entry.get("http_headers") or info.get("http_headers") or {}
        if isinstance(headers, dict):
            save_stream_headers(url, headers)
        save_artwork(requested_title, info.get("thumbnail"))
        from logging import getLogger
        getLogger("telegram_music").info(
            "SoundCloud remote audio resolved in %.2fs (public HTTPS source)",
            time.monotonic() - started,
        )
        return url, f"{title} · SoundCloud (live)", duration
    return None
