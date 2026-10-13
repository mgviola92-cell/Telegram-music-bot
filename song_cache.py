"""Small bounded local media cache for repeated, already playable tracks.

Render Free filesystem is ephemeral; these files are convenience only and are
never a persistent backup. Each playback receives its own disposable copy.
"""
import hashlib
import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path


_LOG = logging.getLogger("telegram_music")
_CACHE_DIR = Path(tempfile.mkdtemp(prefix="mudyar_audio_cache_"))
_LOCK = threading.RLock()
_ITEMS: dict[str, tuple[Path, str, float | None, float]] = {}
_TTL_SECONDS = 15 * 60
_MAX_ITEMS = 2
_MAX_TOTAL_BYTES = 36 * 1024 * 1024
_MAX_ONE_FILE_BYTES = 24 * 1024 * 1024


def _key(query: str, video: bool) -> str:
    return hashlib.sha256((("video" if video else "audio") + ":" + query.strip().casefold()).encode()).hexdigest()


def _prune() -> None:
    now = time.monotonic()
    for key, (path, _, _, expiry) in list(_ITEMS.items()):
        if now >= expiry or not path.is_file() or path.stat().st_size == 0:
            _ITEMS.pop(key, None)
            path.unlink(missing_ok=True)


def get_cached_song(query: str, video: bool) -> tuple[str, str, float | None] | None:
    with _LOCK:
        _prune()
        item = _ITEMS.get(_key(query, video))
        if item is None:
            return None
        path, title, duration, _ = item
        directory = Path(tempfile.mkdtemp(prefix="mudyar_media_"))
        destination = directory / ("cached" + path.suffix)
        try:
            shutil.copyfile(path, destination)
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            return None
        _LOG.info("Media cache hit for a previous playback")
        return str(destination), title, duration


def save_cached_song(query: str, video: bool, source: str, title: str, duration: float | None) -> bool:
    path = Path(source)
    if not path.is_file():
        return False
    size = path.stat().st_size
    if size <= 0 or size > _MAX_ONE_FILE_BYTES:
        return False
    key = _key(query, video)
    with _LOCK:
        _prune()
        # Remove the expired/replaced file without deleting files in use.
        old = _ITEMS.pop(key, None)
        if old is not None:
            old[0].unlink(missing_ok=True)
        for existing_key, existing in list(_ITEMS.items()):
            if len(_ITEMS) < _MAX_ITEMS and sum(
                t[0].stat().st_size for t in _ITEMS.values() if t[0].is_file()
            ) + size <= _MAX_TOTAL_BYTES:
                break
            existing[0].unlink(missing_ok=True)
            _ITEMS.pop(existing_key, None)
        cached = _CACHE_DIR / (key + path.suffix.lower())
        try:
            shutil.copyfile(path, cached)
            _ITEMS[key] = (cached, title, duration, time.monotonic() + _TTL_SECONDS)
            return True
        except OSError:
            cached.unlink(missing_ok=True)
            return False
