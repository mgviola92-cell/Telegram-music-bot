"""Small, rights-aware Audius catalog fallback.

Audius offers a documented public search/stream API for tracks that artists
allow third-party apps to stream. Do not treat matches as YouTube originals.
This module never requests private networks or arbitrary user-provided URLs.
"""
import json
import logging
import re
import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger("telegram_music")
API_BASE = "https://api.audius.co/v1"
MAX_TRACK_SECONDS = 600
MAX_STREAM_BYTES = 24 * 1024 * 1024
MAX_RESULTS = 5
ALLOWED_STREAM_SUFFIXES = (".audius.co", ".audius.org", ".audiusindex.org")


def safe_audius_url(value: str) -> bool:
    """Limit redirects to Audius-operated HTTPS hosts, not arbitrary LANs."""
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        secure_port = url.port in (None, 443)
    except ValueError:
        return False
    return (
        url.scheme == "https"
        and secure_port
        and bool(host)
        and not url.username
        and not url.password
        and (
            host in {"api.audius.co", "audius.co", "audius.org", "audiusindex.org"}
            or any(host.endswith(suffix) for suffix in ALLOWED_STREAM_SUFFIXES)
        )
    )


class AudiusOnlyRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_audius_url(newurl):
            raise ValueError("Audius redirected outside its known audio hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def find_audius_tracks(query: str, *, opener=None) -> list[dict]:
    """Return public, accessible tracks from the Audius REST search endpoint."""
    if not query.strip() or len(query) > 120:
        return []
    opener = opener or build_opener(AudiusOnlyRedirect())
    url = API_BASE + "/tracks/search?" + urlencode({
        "query": query, "limit": MAX_RESULTS, "app_name": "mudyar-music"
    })
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "MudyarMusicBot/1.0"})
    with opener.open(request, timeout=12) as response:
        if int(response.status) != 200:
            raise ValueError("Audius search returned HTTP error")
        raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError("Audius search result exceeded safe limit")
        payload = json.loads(raw)
    return payload.get("data", []) if isinstance(payload, dict) and isinstance(payload.get("data"), list) else []


def fetch_audius_audio(
    query: str, cancel_signal: threading.Event | None = None, *, match=None, opener=None
) -> tuple[str, str, float | None] | None:
    """Fetch a playable public Audius stream to an isolated temporary file.

    Audius API stream permission/gated releases are respected. If a track is
    absent or a match is dubious, simply let the next provider handle it.
    """
    if cancel_signal is not None and cancel_signal.is_set():
        raise InterruptedError("Cancelled before Audius search")
    opener = opener or build_opener(AudiusOnlyRedirect())
    entries = find_audius_tracks(query, opener=opener)
    for track in entries[:MAX_RESULTS]:
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Cancelled Audius search")
        if not isinstance(track, dict):
            continue
        if (track.get("is_stream_gated") or track.get("is_scheduled_release")
                or track.get("is_unlisted") or track.get("is_delete")
                or track.get("is_streamable") is False):
            continue
        identifier = str(track.get("id") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identifier):
            continue
        seconds = track.get("duration")
        try:
            duration = float(seconds) if seconds is not None else None
            if duration is not None and not (0 < duration <= MAX_TRACK_SECONDS):
                continue
        except (ValueError, TypeError):
            continue
        title = str(track.get("title") or "")[:95]
        user = track.get("user") if isinstance(track.get("user"), dict) else {}
        artist = str(user.get("name") or "")
        if not title or (match is not None and not match(title, query, artist)):
            continue
        url = API_BASE + "/tracks/" + identifier + "/stream?" + urlencode({
            "app_name": "mudyar-music"
        })
        directory = Path(tempfile.mkdtemp(prefix="mudyar_media_"))
        target = directory / "audius.mp3"
        try:
            request = Request(
                url, headers={"Accept": "audio/mpeg", "User-Agent": "MudyarMusicBot/1.0"}
            )
            with opener.open(request, timeout=18) as response:
                if not safe_audius_url(response.url if isinstance(response.url, str) else response.geturl()):
                    raise ValueError("Audius audio redirected outside approved hosts")
                if int(response.status) != 200:
                    raise ValueError("Audius stream not available")
                mime = response.headers.get("Content-Type", "").lower()
                if (mime and not mime.startswith(("audio/", "application/octet-stream"))):
                    raise ValueError("Audius returned non-audio response")
                total = 0
                with target.open("wb") as out:
                    while True:
                        if cancel_signal is not None and cancel_signal.is_set():
                            raise InterruptedError("Cancelled Audius stream")
                        chunk = response.read(128 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_STREAM_BYTES:
                            raise ValueError("Audius stream exceeds size cap")
                        out.write(chunk)
            if total < 1024:
                raise ValueError("Audius returned empty audio")
            LOG.info("Resolved publicly streamable Audius recording")
            return str(target), f"{title} — {artist[:50]} · Audius", duration
        except InterruptedError:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        except Exception as exc:
            LOG.info("Audius track unavailable (%s)", type(exc).__name__)
            shutil.rmtree(directory, ignore_errors=True)
            continue
    return None
