"""Default direct HTTPS audio stream resolver for publicly playable SoundCloud tracks.

This does not fetch a whole audio file into RAM. PyTgCalls/FFmpeg reads
small network chunks from the authorized media CDN. No access controls,
DRM, sign-in checks, or private-network URL restrictions are bypassed.
Fallback to existing, already-working local media if direct playback fails.
"""
import threading
import time
from logging import getLogger

from player_visuals import (
    allowed_audio_cdn, save_artwork, save_stream_headers,
)

LOG = getLogger("telegram_music")

# Keep codec detection, but bound initial probing for known audio-only CDN streams.
DIRECT_AUDIO_FFMPEG = "-analyzeduration 500000 -probesize 131072"

# Cache track pages, never signed media URLs, so playback resolves fresh tokens.
_SEARCH_LOCK = threading.RLock()
_SEARCHES = {}
_SEARCH_TTL = 600
_SEARCH_MAX = 48


def cached_search(query: str, max_tracks: int) -> list[str] | None:
    key = query.strip().casefold()
    with _SEARCH_LOCK:
        item = _SEARCHES.get(key)
        if item is None:
            return None
        urls, expiry = item
        if time.monotonic() >= expiry:
            _SEARCHES.pop(key, None)
            return None
        LOG.info("SoundCloud track search cache hit; resolving a fresh media URL")
        return list(urls[:max_tracks])


def remember_search(query: str, urls: list[str]) -> None:
    if not urls:
        return
    with _SEARCH_LOCK:
        key = query.strip().casefold()
        _SEARCHES.pop(key, None)
        if len(_SEARCHES) >= _SEARCH_MAX:
            _SEARCHES.pop(next(iter(_SEARCHES)))
        _SEARCHES[key] = (tuple(urls), time.monotonic() + _SEARCH_TTL)


def forget_search(query: str) -> None:
    with _SEARCH_LOCK:
        _SEARCHES.pop(query.strip().casefold(), None)



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
        LOG.info("Direct stream rejected: empty metadata or DRM")
        return None
    if str(info.get("availability", "")).lower() in {"private", "subscriber_only", "needs_auth", "premium_only"}:
        LOG.info("Direct stream rejected: restricted availability")
        return None
    if str(info.get("access", "playable")).lower() not in {"playable", "full", ""}:
        LOG.info("Direct stream rejected: track access is restricted")
        return None
    duration = info.get("duration")
    try:
        duration = float(duration) if duration is not None else None
        if duration is not None and not (1 <= duration <= 600):
            LOG.info("Direct stream rejected: duration outside allowed range")
            return None
    except (TypeError, ValueError):
        return None
    title = str(info.get("title") or "")[:85]
    artist = str(info.get("uploader") or "")[:75]
    if not title or (check is not None and not check(title, requested_title, artist)):
        LOG.info("Direct stream rejected: recording does not match request")
        return None

    # HLS playlists and HTTPS media are passed to PyTgCalls directly.
    # Only public SoundCloud media CDN destinations are allowed.
    choices = []
    for entry in info.get("requested_formats") or []:
        if isinstance(entry, dict):
            choices.append(entry)
    choices.append(info)
    # The extractor's preferred format may be a download host that cannot
    # be used by this resolver. Try other public SoundCloud CDN formats.
    choices.extend(entry for entry in reversed(info.get("formats") or [])
                   if isinstance(entry, dict))
    for entry in choices:
        if entry.get("has_drm") or entry.get("is_drm"):
            continue
        if "preview" in str(entry.get("format_id") or "").lower():
            continue
        url = entry.get("url")
        if not allowed_audio_cdn(url):
            # Never log signed URL query strings or credentials.
            try:
                parsed_media = urlsplit(url) if isinstance(url, str) else None
                LOG.info(
                    "Direct stream URL rejected (scheme=%s host=%s length=%s)",
                    parsed_media.scheme if parsed_media else "missing",
                    (parsed_media.hostname or "unknown")[:100] if parsed_media else "missing",
                    len(url) if isinstance(url, str) else 0,
                )
            except ValueError:
                LOG.info("Direct stream URL rejected: malformed URL")
            continue
        protocol = str(entry.get("protocol") or info.get("protocol") or "").lower()
        if protocol and protocol not in {"https", "http", "m3u8", "m3u8_native"}:
            continue
        headers = entry.get("http_headers") or info.get("http_headers") or {}
        if isinstance(headers, dict):
            save_stream_headers(url, headers)
        save_artwork(requested_title, info.get("thumbnail"))
        LOG.info(
            "SoundCloud remote audio resolved in %.2fs (public HTTPS source)",
            time.monotonic() - started,
        )
        return url, f"{title} · SoundCloud (live)", duration
    LOG.info("Direct stream rejected: no approved HTTPS audio URL (protocol=%s)",
             str(info.get("protocol") or "unknown")[:40])
    return None
