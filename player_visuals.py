"""Lightweight player UI and safe, ephemeral SoundCloud metadata.

No artwork HTTP fetches in the playback path. Telegram may fetch artwork
directly from an approved SoundCloud image host. No complete copyrighted
lyrics are reproduced: the Lyrics button points to a search page.
"""
import re
import threading
from urllib.parse import quote, urlsplit

PULSES = ("▁▃▆▄▂", "▂▅▇▃▁", "▃▆▂▇▄", "▁▄▆▃▇")
_LOCK = threading.RLock()
_IMAGES = {}
_STREAM_HEADERS = {}
_MAX_META = 48


def artwork_is_safe(url: str | None) -> bool:
    if not isinstance(url, str) or len(url) > 1200:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme == "https"
            and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and (host == "sndcdn.com" or host.endswith(".sndcdn.com"))
        )
    except ValueError:
        return False


def allowed_audio_cdn(url: str | None) -> bool:
    """Accept only HTTPS SoundCloud-operated public transcoding hosts."""
    if not isinstance(url, str) or len(url) > 16384:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        return (
            parsed.scheme == "https" and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
            and (host == "sndcdn.com" or host.endswith(".sndcdn.com")
                 or host == "playback.media-streaming.soundcloud.cloud")
        )
    except ValueError:
        return False


def save_artwork(query: str, thumbnail: str | None) -> None:
    if not artwork_is_safe(thumbnail):
        return
    with _LOCK:
        if len(_IMAGES) >= _MAX_META:
            _IMAGES.pop(next(iter(_IMAGES)))
        _IMAGES[query.strip().casefold()] = thumbnail


def artwork_for(query: str) -> str | None:
    with _LOCK:
        return _IMAGES.get(query.strip().casefold())


def save_stream_headers(url: str, headers: dict) -> None:
    if not allowed_audio_cdn(url):
        return
    safe = {}
    for key, value in headers.items():
        if key.lower() in {"user-agent", "accept", "referer", "origin"} and isinstance(value, str):
            if len(value) <= 300 and "\r" not in value and "\n" not in value:
                safe[key] = value
    with _LOCK:
        if len(_STREAM_HEADERS) >= _MAX_META:
            _STREAM_HEADERS.pop(next(iter(_STREAM_HEADERS)))
        _STREAM_HEADERS[url] = safe


def stream_headers_for(url: str) -> dict:
    with _LOCK:
        return dict(_STREAM_HEADERS.get(url, {}))


def format_clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = max(0, int(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def player_caption(
    title: str, elapsed: float, duration: float | None, *, paused: bool = False, requester: str = "—", requester_id=None, html_mode=False, actor="", actor_id=None
) -> str:
    """Static duration; Telegram cards cannot expose a seekable audio timeline."""
    from card_layout import render
    from music_ui import clean_track_title
    result=render('playing',html_mode=html_mode,requester_id=requester_id, title=clean_track_title(title), duration=format_clock(duration),
                  requester=requester, status='Paused' if paused else 'Started Streaming')
    if html_mode and actor:
        from card_layout import mention
        result+='\n\n<blockquote><b>'+('Stream paused by' if paused else 'Stream resumed by')+'</b> '+mention(actor,actor_id)+'</blockquote>'
    return result


def lyrics_search_url(title: str) -> str:
    # A lyrics-site search link is not permission to repost its song text.
    clean = re.sub(r"[\x00-\x1f\x7f]", " ", title or "")[:120]
    return "https://genius.com/search?q=" + quote(clean, safe="")
