"""Low-resource VC idle media and optional authorized radio source.

A finite 15-minute silent WAV repeats while a group has no queued tracks;
this keeps the existing Telegram call alive without inventing a radio
station or broadcasting somebody else's music without permission.
"""
import os
import tempfile
import threading
import wave
from pathlib import Path
from urllib.parse import urlsplit

_IDLE_PATH = Path(tempfile.gettempdir()) / "mudyar_idle_silence_15min.wav"
_IDLE_LOCK = threading.Lock()
IDLE_SECONDS = 15 * 60


def configured_radio_url() -> str | None:
    """Only use explicitly allowed HTTPS sources permitted for rebroadcast.

    The station audio URL comes from the bot owner's private Render settings.
    Listing a host is not the same as obtaining station permission.
    """
    raw = os.getenv("RADIO_STREAM_URL", "").strip()
    allowed = {
        host.strip().lower().strip(".")
        for host in os.getenv("RADIO_STREAM_HOSTS", "").split(",")
        if host.strip()
    }
    if not raw or not allowed:
        return None
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().strip(".")
        return raw if (
            parsed.scheme == "https"
            and host in allowed
            and parsed.port in (None, 443)
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        ) else None
    except ValueError:
        return None


def ensure_idle_audio() -> str:
    """Produce a reusable, silent mono PCM WAV without loading it into RAM."""
    with _IDLE_LOCK:
        if _IDLE_PATH.is_file() and _IDLE_PATH.stat().st_size > IDLE_SECONDS * 8000:
            return str(_IDLE_PATH)
        temporary = _IDLE_PATH.with_suffix(".tmp")
        try:
            with wave.open(str(temporary), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                fragment = bytes(16000)
                for _ in range(IDLE_SECONDS):
                    audio.writeframesraw(fragment)
            temporary.replace(_IDLE_PATH)
        finally:
            temporary.unlink(missing_ok=True)
        return str(_IDLE_PATH)
