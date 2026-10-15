"""Isolated Telegram Voice Chat Music Bot — MVP.

Telegram audio replies and allowlisted direct HTTPS audio streams only.
Secrets must be configured as Render environment variables.
"""
import asyncio
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from music_ui import player_controls, queue_display, queue_remove, queue_shuffle
from audius_source import fetch_audius_audio
from song_cache import get_cached_song, save_cached_song
from idle_media import configured_radio_url, ensure_idle_audio
from youtube_health import youtube_guard
from archive_source import get_archive_audio
from prefetch import PreparedAudio
from direct_stream import (resolve_soundcloud_https,
                           cached_search, remember_search, forget_search)
from player_visuals import (player_caption, artwork_for, artwork_is_safe, stream_headers_for, allowed_audio_cdn, save_artwork)
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import AudioQuality, ChatUpdate, GroupCallConfig, MediaStream, StreamEnded, VideoQuality

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram_music")

MAX_QUEUE = 10
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 20 * 1024 * 1024
MAX_YOUTUBE_BYTES = 40 * 1024 * 1024
MAX_MEDIA_SECONDS = 600


@dataclass
class Track:
    title: str
    source: str
    telegram_file: bool = False
    video: bool = False
    youtube: bool = False
    duration_seconds: float | None = None


@dataclass
class Player:
    queue: deque = field(default_factory=deque)
    current: Track | None = None
    temp_file: str | None = None
    started_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loading_task: asyncio.Task | None = None
    cancel_download: threading.Event | None = None
    paused: bool = False
    loop_current: bool = False
    panel_id: int | None = None
    recovery_attempts: int = 0
    # Only one voice connection per group. Idle is NOT a queued song.
    idle_mode: str | None = None  # "radio" or "silence"
    idle_started_at: float = 0.0
    radio_retry_after: float = 0.0
    radio_enabled: bool = True
    keep_idle: bool = True
    idle_notice_sent: bool = False
    idle_early_ends: int = 0
    prepared: PreparedAudio | None = None
    skip_pending: bool = False
    # UI timestamp excludes actual paused time.
    paused_since: float = 0.0
    paused_seconds: float = 0.0
    panel_is_photo: bool = False
    progress_task: asyncio.Task | None = None
    announce_task: asyncio.Task | None = None
    stream_enabled: bool = True


def voice_slot_count(players: dict[int, Player]) -> int:
    """Count active VC streams including idle ones, not group memberships."""
    return sum(
        bool(p.current is not None or p.idle_mode is not None or
             (p.loading_task is not None and not p.loading_task.done()))
        for p in players.values()
    )


def oldest_idle_chat(players: dict[int, Player], exclude: int) -> int | None:
    """An idle VC makes room for a requested song before rejecting a group."""
    idle = [
        (p.idle_started_at, chat_id)
        for chat_id, p in players.items()
        if chat_id != exclude and p.idle_mode is not None
        and p.current is None
        and (p.loading_task is None or p.loading_task.done())
    ]
    return min(idle)[1] if idle else None


def ended_early(elapsed: float, duration_seconds: float | None) -> bool:
    """An end event before expected duration is probably a broken stream.

    Don't restart normal completed tracks; allow one limited attempt for
    formats whose duration is unknown (including live/replied media).
    """
    if duration_seconds and duration_seconds > 0:
        return elapsed + 12 < duration_seconds
    return elapsed < 25


async def play_with_reconnect(calls: PyTgCalls, chat_id: int, stream: MediaStream) -> None:
    """Recover once from a stale per-chat voice connection.

    Do not create missing group calls; a group admin must start the VC.
    """
    try:
        await asyncio.wait_for(
            calls.play(chat_id, stream, GroupCallConfig(auto_start=False)),
            timeout=40,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        reason = type(exc).__name__
        if reason in {"NoActiveGroupCall", "NoAudioSourceFound"}:
            raise
        log.warning("VC first join/play failed in chat %s (%s); trying reconnection", chat_id, reason)
        try:
            await asyncio.wait_for(calls.leave_call(chat_id), timeout=8)
        except Exception as leave_exc:
            log.info("VC reconnection cleanup in %s: %s", chat_id, type(leave_exc).__name__)
        await asyncio.sleep(0.8)
        await asyncio.wait_for(
            calls.play(chat_id, stream, GroupCallConfig(auto_start=False)),
            timeout=40,
        )


class AudioFramesStalled(RuntimeError):
    """The decoder was accepted but its outgoing media clock never advanced."""


async def wait_for_audio_frames(calls: PyTgCalls, chat_id: int, *,
                                timeout: float = 8, interval: float = 1) -> None:
    """Check native outgoing frame progress, not UI elapsed wall-clock time."""
    async def poll():
        initial = await calls.time(chat_id)
        previous = initial
        while True:
            await asyncio.sleep(interval)
            current = await calls.time(chat_id)
            if current > previous:
                log.info("VC outgoing media clock advanced chat %s: %s -> %s",
                         chat_id, initial, current)
                return
            previous = current
    try:
        await asyncio.wait_for(poll(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("VC decoder accepted but outgoing media stalled chat %s", chat_id)
        raise AudioFramesStalled("No outgoing audio frame progress after playback started") from None


async def ensure_voice_audio(calls: PyTgCalls, chat_id: int, assistant_id: int) -> bool:
    """Restore local capture and report server-side admin mute without bypassing it."""
    own = None
    try:
        participants = await asyncio.wait_for(calls.get_participants(chat_id), timeout=4)
        own = next((p for p in participants or [] if p.user_id == assistant_id), None)
        if own is not None:
            log.info("VC audio state chat %s: muted=%s admin_muted=%s volume=%s",
                     chat_id, own.muted, own.muted_by_admin, own.volume)
            if own.muted_by_admin:
                return True
    except Exception as exc:
        log.info("VC participant audio check unavailable (%s)", type(exc).__name__)
    try:
        await asyncio.wait_for(calls.unmute(chat_id), timeout=4)
        if own is not None and own.volume == 0:
            await asyncio.wait_for(calls.change_volume_call(chat_id, 100), timeout=4)
            log.info("Restored zero VC volume chat %s", chat_id)
        log.info("VC microphone capture enabled chat %s", chat_id)
    except Exception as exc:
        log.warning("VC microphone enable unavailable chat %s (%s)", chat_id, type(exc).__name__)
    return False


def cancel_active_loading(player: Player) -> bool:
    """Let /stop interrupt the loader before waiting on its chat lock."""
    if player.cancel_download is not None:
        player.cancel_download.set()
    task = player.loading_task
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
        return True
    return False


def clean_temp(path: str | None) -> None:
    if path and not path.startswith(("https://", "http://")):
        try:
            p = Path(path)
            temp_root = Path(tempfile.gettempdir())
            if p.parent.parent == temp_root and p.parent.name.startswith("mudyar_media_"):
                shutil.rmtree(p.parent, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            log.warning("Temporary audio file cleanup failed")


def allowed_url(value: str, allowed_hosts: set[str]) -> bool:
    """Do not let the media decoder request arbitrary private-network URLs."""
    try:
        url = urlsplit(value)
        return (
            url.scheme == "https"
            and bool(url.hostname)
            and url.hostname.lower() in allowed_hosts
            and not url.username
            and not url.password
        )
    except ValueError:
        return False


def discard_prepared(player: Player) -> None:
    previous = player.prepared
    player.prepared = None
    if previous is not None:
        previous.discard()


def prepare_next_song(player: Player) -> None:
    """Prepare only the immediate next audio while current VC audio plays."""
    upcoming = player.queue[0] if player.queue else None
    if (player.prepared is not None and player.prepared.track is upcoming
            and player.prepared.prefer_stream == player.stream_enabled):
        return
    discard_prepared(player)
    if (upcoming is not None and player.current is not None
            and upcoming.youtube and not upcoming.video
            and os.getenv("MUSIC_PREFETCH_ENABLED", "1") != "0"):
        player.prepared = PreparedAudio(
            upcoming, download_song, clean_temp, prefer_stream=player.stream_enabled,
        )
        log.info("Prefetch began for next queued audio")


def is_youtube_url(value: str) -> bool:
    """Only video URLs from known YouTube domains may reach yt-dlp."""
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and (parsed.hostname or "").lower() in {
                "youtube.com", "www.youtube.com", "music.youtube.com",
                "m.youtube.com", "youtu.be",
            }
        )
    except ValueError:
        return False


async def download_bot_api_file(
    file_id: str,
    target: str,
    size_limit: int,
    token: str,
    api_root: str = "https://api.telegram.org",
) -> str:
    """Fetch media over Bot API HTTPS instead of the failing MTProto media DC.

    Bot API file downloads have a 20 MB limit. URLs containing the token
    must never appear in group replies or application exception logs.
    """
    limit = min(size_limit, 20 * 1024 * 1024)
    timeout = ClientTimeout(total=35, connect=8, sock_read=15)
    try:
        async with ClientSession(timeout=timeout) as http:
            async with http.post(
                f"{api_root}/bot{token}/getFile",
                data={"file_id": file_id},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ValueError("Telegram getFile returned non-200")
                payload = await response.json()
                if not payload.get("ok"):
                    raise ValueError("Telegram getFile rejected file_id")
                result = payload.get("result") or {}
                file_path = result.get("file_path")
                if (not isinstance(file_path, str) or not file_path or
                        file_path.startswith("/") or
                        ".." in file_path.split("/") or "://" in file_path):
                    raise ValueError("Telegram returned invalid file path")
                if int(result.get("file_size") or 0) > limit:
                    raise ValueError("Telegram Bot API 20 MB download limit")
            file_url = f"{api_root}/file/bot{token}/" + quote(file_path, safe="/")
            async with http.get(file_url, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError("Telegram file download HTTP failure")
                size = 0
                with open(target, "wb") as media:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise ValueError("Telegram file exceeds 20 MB limit")
                        media.write(chunk)
                if size == 0:
                    raise ValueError("Telegram returned an empty file")
        return target
    except Exception as exc:
        Path(target).unlink(missing_ok=True)
        # Do not leak the Bot token through aiohttp's URL-containing errors.
        raise RuntimeError(
            f"Telegram HTTP media download failed ({type(exc).__name__})"
        ) from None


def suitable_recording(title: str, query: str, artist: str = "") -> bool:
    """Avoid playing covers/remixes instead of the requested original.

    This heuristic cannot verify recording ownership or a music license.
    It only rejects obviously altered or unrelated uploads.
    """
    import re

    terms = set(re.findall(r"[a-z0-9]+", query.casefold()))
    title_words = set(re.findall(r"[a-z0-9]+", title.casefold()))
    artist_words = set(re.findall(r"[a-z0-9]+", artist.casefold()))
    variant_terms = {
        "cover", "reverb", "slowed", "remix", "nightcore", "karaoke",
        "instrumental", "acoustic", "sped", "lofi", "mashup", "bootleg",
        "unofficial", "piano", "8d", "chopped",
    }
    variants = variant_terms & title_words
    if variants - terms:
        return False
    if "live" in title_words and "live" not in terms:
        return False
    if "bass boosted" in title.casefold() and "bass" not in terms:
        return False
    # With an artist and title supplied, require most query keywords.
    # For short ambiguous queries such as "baby", no identity guarantee.
    if len(terms) >= 2:
        matched = terms & (title_words | artist_words)
        return len(matched) >= max(2, len(terms) - 1)
    return True


def soundcloud_results(query: str, max_tracks: int = 6, cancel_signal: threading.Event | None = None) -> list[str]:
    """Rank several publicly playable SoundCloud matches, skipping variants.

    A track not labelled remix/cover is not necessarily the original; show
    the real source/title and avoid claiming otherwise.
    """
    from yt_dlp import YoutubeDL
    import re

    if cancel_signal is not None and cancel_signal.is_set():
        return []
    cached = cached_search(query, max_tracks)
    if cached is not None:
        return cached

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 0,
    }
    search_started = time.monotonic()
    try:
        if cancel_signal is not None and cancel_signal.is_set():
            return []
        with YoutubeDL(opts) as searcher:
            result = searcher.extract_info("scsearch10:" + query, download=False)
            entries = list((result or {}).get("entries") or [])
        if cancel_signal is not None and cancel_signal.is_set():
            return []
    except Exception as exc:
        log.warning("SoundCloud search unavailable after %.2fs (%s)",
                    time.monotonic() - search_started, type(exc).__name__)
        return []
    log.info("SoundCloud search completed in %.2fs (raw matches=%s)",
             time.monotonic() - search_started, len(entries))

    words = set(re.findall(r"[a-z0-9]+", query.casefold()))
    ranked = sorted(
        enumerate(entries),
        key=lambda pair: (
            -len(words & set(re.findall(
                r"[a-z0-9]+",
                (str((pair[1] or {}).get("title") or "") + " " +
                 str((pair[1] or {}).get("uploader") or "")).casefold(),
            ))),
            pair[0],
        ),
    )
    urls: list[str] = []
    for _, entry in ranked:
        if not isinstance(entry, dict) or entry.get("is_drm"):
            continue
        title = str(entry.get("title") or "")
        artist = str(entry.get("uploader") or "")
        if not suitable_recording(title, query, artist):
            continue
        link = entry.get("webpage_url") or entry.get("url")
        if not isinstance(link, str):
            continue
        try:
            hostname = (urlsplit(link).hostname or "").lower()
        except ValueError:
            continue
        if hostname != "soundcloud.com" and not hostname.endswith(".soundcloud.com"):
            continue
        if link not in urls:
            urls.append(link)
        if len(urls) >= max_tracks:
            break
    remember_search(query, urls)
    return urls

def audius_source_enabled() -> bool:
    """Opt in when the owner has configured official Audius API access."""
    setting = os.getenv("AUDIUS_ENABLED", "auto").lower()
    if setting == "0":
        return False
    if setting == "1":
        return True
    return bool(os.getenv("AUDIUS_API_KEY"))


def youtube_audio_enabled() -> bool:
    """YouTube audiovisual extraction is off by default on this server.

    Verification has repeatedly failed on Render's IP; do not waste time
    testing it on each user request or attempt alternate account clients.
    """
    return os.getenv("YOUTUBE_ENABLED", "0") == "1"


def source_candidates(
    query: str, video: bool, cancel_signal: threading.Event | None = None
):
    """Resolve song requests through usable, independently licensed sources.

    Direct YouTube URLs aren't silently replaced with unrelated recordings.
    YouTube extraction is opt-in and never circumvents verification checks.
    """
    youtube_active = youtube_audio_enabled()
    if is_youtube_url(query):
        if youtube_active:
            yield ("YouTube", query)
        return
    if video:
        if youtube_active:
            yield ("YouTube", "ytsearch1:" + query)
        return
    youtube = ("YouTube", "ytsearch1:" + query + " official")
    priority = os.getenv("MUSIC_SOURCE_PRIORITY", "soundcloud").lower()
    if youtube_active and priority == "youtube":
        yield youtube
    if cancel_signal is None or not cancel_signal.is_set():
        for url in soundcloud_results(query, max_tracks=3, cancel_signal=cancel_signal):
            yield ("SoundCloud", url)
    if os.getenv("ARCHIVE_ENABLED", "1") == "1":
        yield ("Internet Archive", query)
    if audius_source_enabled():
        yield ("Audius", query)
    if youtube_active and priority != "youtube":
        yield youtube


class StreamOnlyPlaybackError(RuntimeError):
    pass


def stream_only_enabled() -> bool:
    return os.getenv("MUSIC_STREAM_ONLY", "1") != "0"


def resolve_stream_only(query: str, cancel_signal: threading.Event | None = None):
    """Return approved HTTPS audio only; never create or fetch a local media file."""
    for target in soundcloud_results(query, max_tracks=3, cancel_signal=cancel_signal):
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Stream lookup cancelled")
        try:
            direct = resolve_soundcloud_https(target, query, cancel_signal=cancel_signal,
                                             check=suitable_recording)
            if direct is not None and allowed_audio_cdn(direct[0]):
                log.info("Stream-only audio resolved; disk download and file cache bypassed")
                return direct
        except InterruptedError:
            raise
        except Exception as exc:
            log.info("Stream-only candidate unavailable (%s)", type(exc).__name__)
    if cancel_signal is not None and cancel_signal.is_set():
        raise InterruptedError("Stream lookup cancelled")
    raise StreamOnlyPlaybackError("No playable direct HTTPS audio URL; downloading is disabled")


def download_song(
    query: str, video: bool, cancel_signal: threading.Event | None = None,
    prefer_stream: bool = False,
) -> tuple[str, str, float | None]:
    """Download one playable source, trying SoundCloud if YouTube is blocked."""
    if not video and stream_only_enabled():
        return resolve_stream_only(query, cancel_signal)
    from yt_dlp import YoutubeDL

    if os.getenv("MUSIC_CACHE_ENABLED", "1") != "0":
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Download cancelled by stop")
        cached = get_cached_song(query, video)
        if cached is not None:
            log.info("Audio source: cached local media (direct requested=%s)", prefer_stream)
            return cached

    def abort_if_stopped(_: dict | None = None) -> None:
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Download cancelled by stop")

    log.info("Audio resolution started (direct requested=%s, video=%s)", prefer_stream, video)
    failures: list[str] = []
    resolution_started = time.monotonic()
    provider_started = time.monotonic()
    for provider, target in source_candidates(query, video, cancel_signal=cancel_signal):
        abort_if_stopped()
        log.info("Music provider %s selected after %.2fs",
                 provider, time.monotonic() - resolution_started)
        provider_started = time.monotonic()
        if provider == "YouTube":
            wait = youtube_guard.seconds_remaining()
            if wait > 0:
                log.info("YouTube verification cooldown (%ds remaining); skipping challenge", int(wait))
                failures.append("YouTube verification cooldown")
                continue
        if provider == "Internet Archive":
            # Independently hosted public CC BY/CC0 recordings, not
            # copies of YouTube music or unlicensed commercial tracks.
            try:
                archived = get_archive_audio(
                    target, cancel_signal=cancel_signal, check=suitable_recording
                )
                abort_if_stopped()
                if archived is not None:
                    path, label, seconds = archived
                    if os.getenv("MUSIC_CACHE_ENABLED", "1") != "0":
                        save_cached_song(query, video, path, label, seconds)
                    return path, label, seconds
            except InterruptedError:
                raise
            except Exception as exc:
                log.warning("Archive source unavailable (%s)", type(exc).__name__)
            failures.append("Internet Archive")
            continue
        if provider == "Audius":
            # This is a documented third-party stream API for publicly
            # streamable recordings; it does not extract YouTube media.
            try:
                audius = fetch_audius_audio(
                    target, cancel_signal=cancel_signal, match=suitable_recording
                )
                abort_if_stopped()
                if audius is not None:
                    path, label, seconds = audius
                    if os.getenv("MUSIC_CACHE_ENABLED", "1") != "0":
                        save_cached_song(query, video, path, label, seconds)
                    return path, label, seconds
            except InterruptedError:
                raise
            except Exception as exc:
                log.warning("Audius source unavailable (%s)", type(exc).__name__)
            failures.append("Audius")
            continue
        if provider == "SoundCloud" and prefer_stream and not video:
            try:
                direct = resolve_soundcloud_https(
                    target, query, cancel_signal=cancel_signal,
                    check=suitable_recording,
                )
                abort_if_stopped()
                if direct is not None:
                    return direct
                log.info("Direct SoundCloud URL unavailable; normal download fallback")
            except InterruptedError:
                raise
            except Exception as exc:
                log.info("Direct SoundCloud stream unavailable (%s); normal download fallback",
                         type(exc).__name__)
        directory = Path(tempfile.mkdtemp(prefix="mudyar_media_"))
        opts = {
            "format": (
                "best[ext=mp4][height<=360]/best[height<=360]/best[ext=mp4]"
                if video else "bestaudio/best"
            ),
            "outtmpl": str(directory / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "playlist_items": "1",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [abort_if_stopped],
            "socket_timeout": 10,
            "retries": 0,
            "fragment_retries": 0,
            "max_filesize": MAX_YOUTUBE_BYTES,
        }
        try:
            with YoutubeDL(opts) as downloader:
                result = downloader.extract_info(target, download=True)
            abort_if_stopped()
            if not result:
                raise ValueError("No playable result")
            if result.get("_type") == "playlist":
                entries = [entry for entry in result.get("entries", []) if entry]
                if not entries:
                    raise ValueError("No playable result")
                result = entries[0]
            duration = result.get("duration")
            if duration is not None and float(duration) > MAX_MEDIA_SECONDS:
                raise ValueError("Track exceeds the 10-minute limit")
            valid_types = {".mp3", ".m4a", ".mp4", ".webm", ".opus", ".ogg", ".aac", ".flac", ".wav"}
            files = [
                path for path in directory.iterdir()
                if path.is_file() and path.suffix.lower() in valid_types
            ]
            if not files:
                raise ValueError("No downloaded playable media file")
            media_path = max(files, key=lambda path: path.stat().st_size)
            if media_path.stat().st_size > MAX_YOUTUBE_BYTES:
                raise ValueError("Track exceeds the 40 MB limit")
            title = str(result.get("title") or query)[:85]
            if not video and not is_youtube_url(query) and not suitable_recording(
                title, query, str(result.get("uploader") or "")
            ):
                raise ValueError("Non-matching or altered recording")
            log.info("Resolved music through %s in %.2fs (total=%.2fs; track title may differ)",
                     provider, time.monotonic() - provider_started,
                     time.monotonic() - resolution_started)
            if provider == "YouTube":
                youtube_guard.record_success()
            label = f"{title} · {provider}"
            if provider == "SoundCloud":
                save_artwork(query, result.get("thumbnail"))
            seconds = float(duration) if duration is not None else None
            if os.getenv("MUSIC_CACHE_ENABLED", "1") != "0":
                save_cached_song(query, video, str(media_path), label, seconds)
            return str(media_path), label, seconds
        except Exception as exc:
            shutil.rmtree(directory, ignore_errors=True)
            abort_if_stopped()
            failures.append(provider)
            if provider == "YouTube" and youtube_guard.record_error(exc):
                remaining = youtube_guard.seconds_remaining()
                log.warning(
                    "YouTube rejected automated access; official verification required "
                    "(cooldown=%ds). Not retrying verification with other clients.",
                    int(remaining),
                )
            else:
                log.warning(
                    "Audio source %s candidate unavailable (%s): %s",
                    provider, type(exc).__name__, str(exc)[:230],
                )
    forget_search(query)
    raise RuntimeError("No playable track from " + ", ".join(sorted(set(failures))))


async def main() -> None:
    names = ("API_ID", "API_HASH", "BOT_TOKEN", "SESSION_STRING")
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing Render environment variables: " + ", ".join(missing))

    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    allowed_hosts = {
        value.strip().lower()
        for value in os.getenv("ALLOWED_STREAM_HOSTS", "").split(",")
        if value.strip()
    }
    # Conservative Render Free cap: more than 2 is allowed, but not unlimited.
    max_groups = max(1, min(8, int(os.getenv("MAX_ACTIVE_GROUPS", "4"))))
    players: dict[int, Player] = defaultdict(Player)
    ready = False

    assistant = Client(
        "music_assistant",
        api_id=api_id,
        api_hash=api_hash,
        session_string=os.environ["SESSION_STRING"],
        in_memory=True,
    )
    bot = Client(
        "music_bot",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=os.environ["BOT_TOKEN"],
        in_memory=True,
    )
    calls = PyTgCalls(assistant, workers=min(max_groups, 4))

    async def is_chat_admin(chat_id: int, user_id: int | None) -> bool:
        if not user_id:
            return False
        try:
            member = await bot.get_chat_member(chat_id, user_id)
            return member.status in {
                ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
            }
        except Exception as exc:
            log.info("Cannot check admin status %s: %s", chat_id, type(exc).__name__)
            return False

    async def require_admin(msg: Message) -> bool:
        if msg.sender_chat and msg.sender_chat.id == msg.chat.id:
            return True  # Anonymous group administrator.
        if msg.from_user and await is_chat_admin(msg.chat.id, msg.from_user.id):
            return True
        await msg.reply_text("🛡 ဒီ Playback Control ကို Group Admin ပဲ သုံးနိုင်တယ်။")
        return False

    def elapsed_for(player: Player) -> float:
        if player.current is None or player.started_at <= 0:
            return 0.
        now = time.monotonic()
        withheld = player.paused_seconds
        if player.paused and player.paused_since > 0:
            withheld += now - player.paused_since
        return max(0., now - player.started_at - withheld)

    async def edit_panel(chat_id: int, player: Player) -> None:
        if player.panel_id is None or player.current is None:
            return
        song = player.current
        text = player_caption(
            song.title, elapsed_for(player), song.duration_seconds,
            paused=player.paused,
        )
        buttons = player_controls(
            chat_id, paused=player.paused,
            loop=player.loop_current, title=song.title,
        )
        try:
            if player.panel_is_photo:
                await bot.edit_message_caption(
                    chat_id, player.panel_id, caption=text,
                    reply_markup=buttons,
                )
            else:
                await bot.edit_message_text(
                    chat_id, player.panel_id, text,
                    reply_markup=buttons,
                )
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" not in str(exc).upper():
                log.info("Player UI update unavailable %s (%s)", chat_id, type(exc).__name__)

    async def update_player_panel(chat_id: int, player: Player) -> None:
        await edit_panel(chat_id, player)

    async def update_progress_until_changed(chat_id: int, song: Track) -> None:
        player = players[chat_id]
        try:
            while player.current is song:
                await asyncio.sleep(25)
                if player.current is not song:
                    return
                if not player.paused:
                    await edit_panel(chat_id, player)
        except asyncio.CancelledError:
            return

    async def announce_now_playing(
        chat_id: int, player: Player, song: Track, progress: Message | None
    ) -> None:
        """Publishing UI never blocks playback locks or a /skip request."""
        admin_muted = await ensure_voice_audio(calls, chat_id, identity.id)
        if player.current is not song:
            return
        image = artwork_for(song.source) if not song.video else None
        title = player_caption(
            song.title, elapsed_for(player), song.duration_seconds,
            paused=player.paused,
        )
        if admin_muted:
            title += "\n\n🔇 VC Admin က Assistant ကို mute ထားတယ်။ Unmute / Allow to Speak ပေးပါ။"
        buttons = player_controls(
            chat_id, paused=player.paused, loop=player.loop_current,
            title=song.title,
        )
        previous = player.panel_id
        panel = None
        is_photo = False
        try:
            if image and artwork_is_safe(image):
                try:
                    panel = await asyncio.wait_for(
                        bot.send_photo(chat_id, photo=image, caption=title, reply_markup=buttons),
                        timeout=5,
                    )
                    is_photo = True
                except Exception as exc:
                    log.info("Artwork unavailable (%s); using lightweight text UI",
                             type(exc).__name__)
            if panel is None:
                if progress is not None:
                    try:
                        panel = await progress.edit_text(title, reply_markup=buttons)
                    except Exception:
                        panel = None
                if panel is None:
                    panel = await bot.send_message(chat_id, title, reply_markup=buttons)
            if player.current is not song:
                try:
                    await bot.delete_messages(chat_id, panel.id)
                except Exception:
                    pass
                return
            player.panel_id = panel.id
            player.panel_is_photo = is_photo
            if previous is not None and previous != panel.id:
                try:
                    await bot.delete_messages(chat_id, previous)
                except Exception:
                    pass
            if is_photo and progress is not None:
                try:
                    await progress.delete()
                except Exception:
                    pass
            if player.progress_task is not None and not player.progress_task.done():
                player.progress_task.cancel()
            player.progress_task = asyncio.create_task(
                update_progress_until_changed(chat_id, song)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("Unable to post player card in %s (%s)", chat_id, type(exc).__name__)

    async def clear_player_panel(chat_id: int, player: Player) -> None:
        for task in (player.announce_task, player.progress_task):
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
        player.announce_task = None
        player.progress_task = None
        old_id = player.panel_id
        player.panel_id = None
        player.panel_is_photo = False
        if old_id is not None:
            try:
                await bot.delete_messages(chat_id, old_id)
            except Exception:
                pass

    async def ensure_assistant_in_group(chat: object) -> None:
        """Join automatically. Private groups need bot admin + invite rights."""
        chat_id = chat.id
        try:
            await assistant.get_chat_member(chat_id, "me")
            return
        except Exception:
            pass
        # Public groups can be joined by their public username.
        username = getattr(chat, "username", None)
        if username:
            try:
                await assistant.join_chat(username)
                await assistant.get_chat_member(chat_id, "me")
                return
            except Exception:
                log.info("Assistant public join failed for chat %s", chat_id)

        # Create a one-use link without revoking the group's main invite link.
        try:
            invite = await bot.create_chat_invite_link(
                chat_id, name="Mudyar Music Assistant", member_limit=1
            )
        except Exception as exc:
            log.warning("Assistant invite unavailable: %s", type(exc).__name__)
            raise RuntimeError(
                "Private Group မှာ Music Bot ကို Admin + Invite Users permission "
                "ပေးပါ။ ပြီးရင် /play ပြန်စမ်းပါ။"
            ) from exc
        try:
            await assistant.join_chat(invite.invite_link)
            await assistant.get_chat_member(chat_id, "me")
        except Exception as exc:
            log.warning("Assistant join failed: %s", type(exc).__name__)
            raise RuntimeError(
                "Assistant Auto Join မရသေးဘူး။ Group Join Restriction "
                "နဲ့ Ban Settings စစ်ပါ။"
            ) from exc
        finally:
            try:
                await bot.revoke_chat_invite_link(chat_id, invite.invite_link)
            except Exception:
                log.warning("Could not revoke temporary invite for chat %s", chat_id)

    async def start_idle(
        chat_id: int, player: Player, progress: Message | None = None
    ) -> bool:
        """Preserve a live VC after the queue ends without faking radio audio.

        Radio requires a verified, authorized station configured by its owner.
        Otherwise play a tiny silent loop to remain connected. Caller holds lock.
        """
        if not player.keep_idle:
            return False
        options: list[tuple[str, str]] = []
        radio = (
            configured_radio_url() if player.radio_enabled
            and time.monotonic() >= player.radio_retry_after else None
        )
        if radio:
            options.append(("radio", radio))
        try:
            silence = await asyncio.to_thread(ensure_idle_audio)
            options.append(("silence", silence))
        except OSError as exc:
            log.warning("Could not build silent idle media (%s)", type(exc).__name__)
        for mode, source in options:
            try:
                await play_with_reconnect(
                    calls, chat_id,
                    MediaStream(
                        source, AudioQuality.MEDIUM,
                        video_flags=MediaStream.Flags.IGNORE,
                        audio_flags=MediaStream.Flags.REQUIRED,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Could not enter %s idle in chat %s (%s)",
                            mode, chat_id, type(exc).__name__)
                if mode == "radio":
                    player.radio_retry_after = time.monotonic() + 10 * 60
                continue
            if not player.idle_mode:
                player.idle_started_at = time.monotonic()
            player.idle_mode = mode
            player.started_at = time.monotonic()
            player.current = None
            player.temp_file = None
            player.paused = False
            player.recovery_attempts = 0
            await clear_player_panel(chat_id, player)
            # Stay connected quietly: no new chat message or progress edit
            # when the queue empties, including silent-loop renewals.
            log.info("Empty queue: chat %s stayed in VC on %s idle (quiet)", chat_id, mode)
            return True
        return False

    async def evict_oldest_idle_for_song(chat_id: int) -> bool:
        """A song request takes priority over the oldest idle group VC."""
        if voice_slot_count(players) < max_groups:
            return True
        victim_id = oldest_idle_chat(players, chat_id)
        if victim_id is None:
            return False
        victim = players[victim_id]
        async with victim.lock:
            if victim.current is not None or victim.idle_mode is None:
                return False
            if victim.loading_task is not None and not victim.loading_task.done():
                return False
            victim.idle_mode = None
            victim.idle_notice_sent = False
            victim.idle_started_at = 0
            victim.started_at = 0
            try:
                await asyncio.wait_for(calls.leave_call(victim_id), timeout=10)
            except Exception as exc:
                log.warning("Idle eviction VC leave failed %s (%s)",
                            victim_id, type(exc).__name__)
                return False
            log.info("Freed oldest idle chat %s for a new song in %s",
                     victim_id, chat_id)
        try:
            await bot.send_message(
                victim_id,
                "🎧 အခြား Group က သီချင်းဖွင့်ဖို့ VC Slot လိုလာလို့ "
                "Idle ဖြစ်နေတဲ့ ဒီ VC ကနေ ခဏထွက်ပေးလိုက်တယ်။ "
                "/play နဲ့ ပြန်ဝင်နိုင်တယ်။",
            )
        except Exception:
            pass
        return voice_slot_count(players) < max_groups

    async def advance(chat_id: int, progress: Message | None = None, *, repeat: bool = False) -> None:
        """Caller holds this chat's lock. Move to next track or idle VC."""
        player = players[chat_id]
        task = asyncio.current_task()
        player.loading_task = task
        cancel_signal = threading.Event()
        player.cancel_download = cancel_signal
        try:
            if repeat and player.loop_current and player.current is not None:
                player.queue.appendleft(player.current)
                discard_prepared(player)
            clean_temp(player.temp_file)
            player.temp_file = None
            player.current = None
            player.paused = False
            player.recovery_attempts = 0

            while player.queue:
                # A failed timed-out worker must not poison the next track.
                cancel_signal = threading.Event()
                player.cancel_download = cancel_signal
                song: Track = player.queue.popleft()
                local_file = None
                try:
                    source = song.source
                    if song.youtube:
                        prepared = player.prepared
                        if prepared is not None and prepared.track is song:
                            player.prepared = None
                            log.info("Next queued track is using background prefetch")
                            try:
                                local_file, title, duration_seconds = await asyncio.wait_for(
                                    prepared.consume(), timeout=100
                                )
                            except Exception:
                                log.info("Prefetch unavailable; trying ordinary track resolution")
                                local_file, title, duration_seconds = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        download_song, source, song.video, cancel_signal,
                                        player.stream_enabled,
                                    ),
                                    timeout=100,
                                )
                        else:
                            discard_prepared(player)
                            local_file, title, duration_seconds = await asyncio.wait_for(
                                asyncio.to_thread(
                                    download_song, source, song.video, cancel_signal,
                                    player.stream_enabled,
                                ),
                                timeout=100,
                            )
                        song.duration_seconds = duration_seconds
                        if cancel_signal.is_set():
                            clean_temp(local_file)
                            return
                        source = local_file
                        song.title = title
                    elif song.telegram_file:
                        target = str(
                            Path(tempfile.gettempdir())
                            / ("music_" + uuid.uuid4().hex + (".mp4" if song.video else ".mp3"))
                        )
                        local_file = target
                        try:
                            downloaded = await download_bot_api_file(
                                source, target,
                                MAX_VIDEO_BYTES if song.video else MAX_AUDIO_BYTES,
                                os.environ["BOT_TOKEN"],
                            )
                            log.info("Telegram media downloaded via HTTPS Bot API in chat %s", chat_id)
                        except RuntimeError as exc:
                            log.warning(
                                "Bot API media fetch unavailable in chat %s (%s); "
                                "trying MTProto with short timeout",
                                chat_id, type(exc).__name__,
                            )
                            Path(target).unlink(missing_ok=True)
                            try:
                                downloaded = await asyncio.wait_for(
                                    bot.download_media(source, file_name=target), timeout=25
                                )
                            except Exception as mtproto_exc:
                                raise RuntimeError(
                                    "Telegram HTTPS and MTProto downloads both unavailable "
                                    f"({type(mtproto_exc).__name__})"
                                ) from None
                        if not downloaded:
                            raise RuntimeError("Telegram file download returned no media")
                        local_file = downloaded
                        source = local_file

                    if cancel_signal.is_set():
                        raise asyncio.CancelledError()
                    log.info("VC play requested chat %s (direct=%s)",
                             chat_id, allowed_audio_cdn(source))
                    try:
                        # Stream construction may probe a remote URL and fail.
                        # Include it in the same fallback as calls.play().
                        if song.video:
                            stream = MediaStream(
                                source, AudioQuality.MEDIUM, VideoQuality.SD_360p,
                            )
                        else:
                            options = {"video_flags": MediaStream.Flags.IGNORE,
                                       "audio_flags": MediaStream.Flags.REQUIRED}
                            if allowed_audio_cdn(source):
                                options["headers"] = stream_headers_for(source)
                            stream = MediaStream(source, AudioQuality.HIGH, **options)
                        await play_with_reconnect(calls, chat_id, stream)
                        if not song.video:
                            await wait_for_audio_frames(calls, chat_id)
                    except Exception as live_exc:
                        if song.youtube and not song.video and stream_only_enabled():
                            log.warning("Stream-only playback failed (%s); download fallback disabled",
                                        type(live_exc).__name__)
                            raise StreamOnlyPlaybackError(
                                "Direct stream produced no usable audio; download fallback disabled"
                            ) from live_exc
                        if not (allowed_audio_cdn(source) and song.youtube):
                            raise
                        log.warning(
                            "Remote audio not playable in VC (%s); reverting to tested local media",
                            type(live_exc).__name__,
                        )
                        local_file, title, duration_seconds = await asyncio.wait_for(
                            asyncio.to_thread(
                                download_song, song.source, song.video, cancel_signal, False
                            ), timeout=100,
                        )
                        song.duration_seconds = duration_seconds
                        song.title = title
                        source = local_file
                        stream = MediaStream(
                            source, AudioQuality.HIGH,
                            video_flags=MediaStream.Flags.IGNORE,
                            audio_flags=MediaStream.Flags.REQUIRED,
                        )
                        await play_with_reconnect(calls, chat_id, stream)
                        await wait_for_audio_frames(calls, chat_id)
                    log.info("VC play success chat %s", chat_id)
                    player.current = song
                    player.idle_mode = None
                    player.idle_started_at = 0
                    player.idle_notice_sent = False
                    player.idle_early_ends = 0
                    player.recovery_attempts = 0
                    player.temp_file = local_file
                    player.started_at = time.monotonic()
                    player.paused_seconds = 0.
                    player.paused_since = 0.
                    # The next requested song downloads while this one plays.
                    prepare_next_song(player)
                    if player.announce_task is not None and not player.announce_task.done():
                        player.announce_task.cancel()
                    player.announce_task = asyncio.create_task(
                        announce_now_playing(chat_id, player, song, progress)
                    )
                    return
                except asyncio.CancelledError:
                    cancel_signal.set()
                    clean_temp(local_file)
                    log.info("Playback loading canceled chat %s", chat_id)
                    raise
                except Exception as exc:
                    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                        cancel_signal.set()
                    clean_temp(local_file)
                    log.exception("Could not play track in chat %s", chat_id)
                    if isinstance(exc, StreamOnlyPlaybackError):
                        error_message = "📡 Direct streaming မအောင်မြင်ဘူး။ ဒီစမ်းသပ်မှုမှာ Download နဲ့ file cache ကို ပိတ်ထားတယ်။"
                    elif type(exc).__name__ == "NoActiveGroupCall":
                        error_message = ("🎙 Group Voice Chat ကို Admin က ဖွင့်ပေးပါ။ "
                                         "Assistant က /play နဲ့ အလိုအလျောက် ဝင်ပေးမယ်။")
                    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
                        song.telegram_file and "download" in str(exc).lower()
                    ):
                        error_message = ("⚠️ Telegram Media Download မအောင်မြင်ဘူး။ "
                                         "HTTPS Bot API နဲ့ MTProto နှစ်ခုစလုံးမရနိုင်တာပါ။ "
                                         "နောက်မှ ပြန်စမ်းပေးပါ။")
                    elif isinstance(exc, AudioFramesStalled):
                        error_message = "🔇 VC ချိတ်ထားပေမဲ့ အသံ data မထွက်လာဘူး။ ပြန်ချိတ်ဖို့ /stop ပြီး /play စမ်းပါ။"
                    elif type(exc).__name__ == "NoAudioSourceFound":
                        error_message = "🔇 သီချင်းရင်းမြစ်မှာ ဖွင့်နိုင်တဲ့ audio မတွေ့ဘူး။ အခြားသီချင်း သို့မဟုတ် audio file နဲ့ စမ်းပါ။"
                    elif song.youtube:
                        error_message = (
                            "⚠️ YouTube ရဲ့ Login/Verification လိုအပ်နေတဲ့ Audio ကို "
                            "Telegram VC ထဲ မဖွင့်နိုင်သေးဘူး။ "
                            "SoundCloud ကလည်း ဒီသီချင်းအတွက် အသုံးပြုနိုင်တဲ့ "
                            "Recording မပေးနိုင်ဘူး။ YouTube ကို တရားဝင် App မှာ "
                            "ဖွင့်နိုင်တယ်။ ခွင့်ပြုထားတဲ့ Audio File ရှိရင် "
                            "Reply ပြီး /play စမ်းနိုင်တယ်။"
                        )
                    else:
                        error_message = ("⚠️ VC သို့မဟုတ် Media Error တက်နေတယ်။ "
                                         "Group Voice Chat ဖွင့်ထားလား စစ်ပါ။")
                    try:
                        if progress:
                            await progress.edit_text(error_message)
                            progress = None
                        else:
                            await bot.send_message(chat_id, error_message)
                    except Exception:
                        log.warning("Could not announce track error in chat %s", chat_id)

            discard_prepared(player)
            # Keep the assistant connected between requests; explicit /stop
            # still leaves. The silent fallback is not claimed as a radio station.
            if await start_idle(chat_id, player, progress):
                return
            player.idle_mode = None
            player.idle_started_at = 0
            player.idle_notice_sent = False
            try:
                await calls.leave_call(chat_id)
            except Exception as exc:
                log.info("VC leave after idle failure in %s: %s", chat_id, type(exc).__name__)
            log.info("No idle stream available; VC left chat %s", chat_id)
            player.paused = False
            await clear_player_panel(chat_id, player)
        finally:
            if player.loading_task is task:
                player.loading_task = None
                player.cancel_download = None

    @bot.on_message(filters.command(["start", "help", "cmd"]))
    async def help_command(_: Client, msg: Message) -> None:
        identity = await bot.get_me()
        add_button = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "➕ Add မုဒြာ to Group",
                url=f"https://t.me/{identity.username}?startgroup=true",
            )
        ]])
        await msg.reply_text(
            "🎧 မုဒြာ Music Bot — VC Player\n\n"
            "Group VC ဖွင့်ပြီး /play Faded လို့ သီချင်းနာမည် ရိုက်နိုင်တယ်။\n"
            "Audio File ကို Reply လုပ်ပြီး /play လည်းရတယ်။\n"
            "Video: ကိုယ်ပိုင် MP4 ကို Reply လုပ်ပြီး /vplay သုံးပါ။\n"
            "Music: SoundCloud ကို အရင်ရှာပြီး open-license Archive Source နဲ့ ဖြည့်တင်းမယ်။\n"
            "YouTube VC Audio: Server Verification ကြောင့် လောလောဆယ် ပိတ်ထားတယ်။\n"
            "Private Group: Music Bot ကို Admin + Invite Users ပေးထားပါ။\n"
            "/play /vplay /queue /now /skip /pause /resume /stop\n"
            "/shuffle /remove 1 /clear /loop — Admin Controls\n"
            "/radio on|off — Authorized radio fallback\n"
            "/idle on|off — Queue ကုန်ရင် VC ထဲဆက်နေမလား\n"
            "/stream on|off — Experimental direct HTTPS audio\n"
            "/ping — bot online ဖြစ်မဖြစ် စစ်မယ်။\n\n"
            "Direct HTTPS audio link တွေအတွက် server-side host allowlist လိုတယ်။",
            reply_markup=add_button,
        )

    @bot.on_message(
        filters.command(["play", "vplay", "ping", "stop", "queue", "radio", "idle", "stream"]) &
        (filters.group | filters.private),
        group=-1,
    )
    async def log_music_command(_: Client, msg: Message) -> None:
        # This pre-handler runs independently of music downloading. It lets
        # Render logs distinguish Telegram not delivering the command from
        # a command that was received but failed to produce a reply.
        action = (msg.command or ["unknown"])[0].lower()
        log.info("Telegram command received /%s in chat %s", action, msg.chat.id)

    @bot.on_message(filters.command("ping"))
    async def ping_command(_: Client, msg: Message) -> None:
        try:
            await msg.reply_text("🏓 Pong — Music Bot online!")
            log.info("Ping replied in chat %s", msg.chat.id)
        except Exception:
            log.exception("Ping reply failed in chat %s", msg.chat.id)

    @bot.on_message(filters.command(["play", "vplay"]) & filters.group)
    async def play_command(_: Client, msg: Message) -> None:
        video_mode = msg.command[0].lower() == "vplay"
        replied = msg.reply_to_message
        media = None
        if replied:
            media = replied.video if video_mode else replied.audio
            if not media and replied.document:
                doc = replied.document
                allowed_mime = "video/mp4" if video_mode else "audio/"
                if (doc.mime_type or "").startswith(allowed_mime):
                    media = doc

        if media:
            byte_limit = MAX_VIDEO_BYTES if video_mode else MAX_AUDIO_BYTES
            if media.file_size and media.file_size > byte_limit:
                await msg.reply_text("⚠️ လောလောဆယ် Telegram Audio/Video 20 MB အောက်ပဲ လက်ခံတယ်။")
                return
            title = (
                getattr(media, "title", None)
                or getattr(media, "file_name", None)
                or ("Telegram video" if video_mode else "Telegram audio")
            )
            track = Track(
                str(title)[:100], media.file_id, telegram_file=True, video=video_mode,
                duration_seconds=float(media.duration) if getattr(media, "duration", None) else None,
            )
        elif len(msg.command) >= 2:
            query = msg.text.split(maxsplit=1)[1].strip()
            if not query or len(query) > 120:
                await msg.reply_text("⚠️ သီချင်းနာမည် 1–120 စာလုံးအတွင်း ထည့်ပေးပါ။")
                return
            if query.startswith(("https://", "http://")):
                if is_youtube_url(query):
                    if not youtube_audio_enabled():
                        await msg.reply_text(
                            "⚠️ YouTube က Render Server ရဲ့ Audio Request တွေကို "
                            "Login Verification တောင်းနေတဲ့အတွက် YouTube VC Playback ကို "
                            "လောလောဆယ် ပိတ်ထားတယ်။ YouTube App ထဲမှာ ဒီ Link ကို "
                            "ဖွင့်နိုင်တယ်။ ဒီမှာတော့ /play <song name> နဲ့ "
                            "အခြား Source ရှာနိုင်တယ်။"
                        )
                        return
                    track = Track("YouTube video" if video_mode else "YouTube audio",
                                  query, video=video_mode, youtube=True)
                elif allowed_url(query, allowed_hosts):
                    track = Track("Direct video" if video_mode else "Direct audio",
                                  query, video=video_mode)
                else:
                    await msg.reply_text("⚠️ YouTube Link သို့မဟုတ် /play <song name> ထည့်ပေးပါ။")
                    return
            else:
                if video_mode and not youtube_audio_enabled():
                    await msg.reply_text(
                        "⚠️ YouTube Video Source ကို လောလောဆယ် ပိတ်ထားတယ်။ "
                        "ကိုယ်ပိုင် MP4 ကို Reply ပြီး /vplay သုံးနိုင်တယ်။"
                    )
                    return
                track = Track(query, query, video=video_mode, youtube=True)
        else:
            await msg.reply_text(
                "🎬 /vplay <video name> သို့မဟုတ် MP4 ကို reply /vplay"
                if video_mode else "🎵 /play Faded လို့ သီချင်းနာမည် ထည့်ပါ။"
            )
            return

        player = players[msg.chat.id]
        log.info("Play mode chat %s: direct=%s", msg.chat.id, player.stream_enabled)
        try:
            progress = await msg.reply_text("⏳ Assistant အလိုအလျောက်ဝင်ပြီး VC ချိတ်နေတယ်...")
            log.info("Music request acknowledged in chat %s", msg.chat.id)
        except Exception:
            log.exception("Cannot acknowledge /play in chat %s", msg.chat.id)
            return
        try:
            await ensure_assistant_in_group(msg.chat)
        except RuntimeError as exc:
            await progress.edit_text("⚠️ " + str(exc))
            return
        except Exception:
            log.exception("Assistant auto-join failed in chat %s", msg.chat.id)
            await progress.edit_text("⚠️ Assistant Auto Join မရသေးဘူး။ Admin / Invite Settings စစ်ပါ။")
            return
        await progress.edit_text("🔎 သီချင်းရှာပြီး VC ဖွင့်ဖို့ ပြင်ဆင်နေတယ်...")
        # Do not make another /play wait for a long download to release
        # the chat lock; safely append while an existing loader owns it.
        if player.loading_task is not None and not player.loading_task.done():
            if len(player.queue) >= MAX_QUEUE:
                await progress.edit_text("⚠️ Queue ပြည့်နေပြီ (10 tracks)။")
                return
            player.queue.append(track)
            prepare_next_song(player)
            await progress.edit_text("✅ Queue ထဲထည့်ပြီးပြီ — " + track.title)
            return
        async with player.lock:
            # An external VC disconnect can leave player.current set even
            # though PyTgCalls has no active session. Without this check,
            # every subsequent /play is stuck in a queue that never plays.
            if player.current is not None:
                try:
                    active_calls = await asyncio.wait_for(calls.calls, timeout=8)
                    if msg.chat.id not in active_calls:
                        log.warning("Resetting stale player state for chat %s on /play", msg.chat.id)
                        player.current = None
                        player.paused = False
                        player.started_at = 0.0
                        player.recovery_attempts = 0
                        await clear_player_panel(msg.chat.id, player)
                        clean_temp(player.temp_file)
                        player.temp_file = None
                except Exception as exc:
                    log.warning("Could not verify VC state in %s (%s)", msg.chat.id, type(exc).__name__)
            if len(player.queue) >= MAX_QUEUE:
                await progress.edit_text("⚠️ Queue ပြည့်နေပြီ (10 tracks)။")
                return
            if video_mode and sum(
                p.current is not None and p.current.video for p in players.values()
            ) >= 1:
                await progress.edit_text("⚠️ Video Streaming တစ်ခုသာ စမ်းသပ်ခွင့်ပြုထားတယ်။")
                return
            if (player.current is None and player.idle_mode is None
                    and not await evict_oldest_idle_for_song(msg.chat.id)):
                await progress.edit_text(
                    f"⚠️ တစ်ပြိုင်နက် VC {max_groups} ခု ပြည့်နေပြီ။ "
                    "Idle Group ရှိလာမှ /play ပြန်စမ်းပါ။"
                )
                return

            player.queue.append(track)
            if player.current is not None:
                prepare_next_song(player)
            if player.current is None:
                try:
                    # Never cancel Pyrogram's long-lived HandlerTask; only
                    # the child playback operation is cancelable by /stop.
                    await asyncio.create_task(advance(msg.chat.id, progress))
                except asyncio.CancelledError:
                    try:
                        await progress.edit_text("⏹ /stop လုပ်ထားလို့ ရပ်လိုက်ပြီ။")
                    except Exception:
                        pass
                    return
            else:
                await progress.edit_text("✅ Queue ထဲထည့်ပြီးပြီ — " + track.title)

    @bot.on_message(filters.command(["queue", "now", "nowplaying"]) & filters.group)
    async def queue_command(_: Client, msg: Message) -> None:
        player = players[msg.chat.id]
        async with player.lock:
            message = queue_display(
                (player.current.title if player.current else
                 "📻 Radio (idle)" if player.idle_mode == "radio" else
                 "🔇 Quiet idle" if player.idle_mode == "silence" else None),
                [song.title for song in player.queue],
                loop=player.loop_current,
            )
        await msg.reply_text(message)

    @bot.on_message(filters.command("radio") & filters.group)
    async def radio_command(_: Client, msg: Message) -> None:
        """Manage the optional authorized radio fallback per group."""
        player = players[msg.chat.id]
        action = msg.command[1].lower() if len(msg.command) > 1 else "status"
        if action == "status":
            configured = configured_radio_url() is not None
            await msg.reply_text(
                "📻 Radio URL: " + ("configured" if configured else "မချိတ်ရသေးဘူး") +
                "\nRadio fallback: " + ("ON" if player.radio_enabled else "OFF") +
                "\nIdle stream: " + (player.idle_mode or "none") +
                "\nGroup Admin: /radio on, /radio off"
            )
            return
        if action not in {"on", "off"}:
            await msg.reply_text("📻 /radio on, /radio off, /radio status")
            return
        if not await require_admin(msg):
            return
        async with player.lock:
            player.radio_enabled = action == "on"
            if player.current is None and player.idle_mode is not None:
                await start_idle(msg.chat.id, player)
        if action == "on" and configured_radio_url() is None:
            await msg.reply_text(
                "📻 Radio ON သတ်မှတ်ထားတယ်။ ဒါပေမဲ့ ခွင့်ပြုထားတဲ့ "
                "Radio Stream URL မရှိသေးလို့ Queue အလွတ်မှာ Silent Idle နဲ့ စောင့်မယ်။"
            )
        else:
            await msg.reply_text(
                "📻 Radio auto-fallback: " + ("ON" if action == "on" else "OFF")
            )

    @bot.on_message(filters.command("idle") & filters.group)
    async def idle_command(_: Client, msg: Message) -> None:
        """Opt out of remaining in a voice call between song requests."""
        player = players[msg.chat.id]
        action = msg.command[1].lower() if len(msg.command) > 1 else "status"
        if action == "status":
            await msg.reply_text(
                "🎙 Empty queue stay in VC: " + ("ON" if player.keep_idle else "OFF") +
                "\nIdle stream: " + (player.idle_mode or "none") +
                "\nGroup Admin: /idle on, /idle off"
            )
            return
        if action not in {"on", "off"}:
            await msg.reply_text("🎙 /idle on, /idle off, /idle status")
            return
        if not await require_admin(msg):
            return
        async with player.lock:
            player.keep_idle = action == "on"
            if not player.keep_idle and player.idle_mode is not None:
                player.idle_mode = None
                player.idle_started_at = 0
                player.idle_notice_sent = False
                try:
                    await asyncio.wait_for(calls.leave_call(msg.chat.id), timeout=10)
                except Exception as exc:
                    log.info("Idle opt-out leave in %s: %s", msg.chat.id, type(exc).__name__)
        await msg.reply_text(
            "🎙 Queue အလွတ်မှာ VC ဆက်စောင့်မယ်: " +
            ("ON" if action == "on" else "OFF") +
            (". /play နဲ့ ပြန်စနိုင်တယ်။" if action == "on" else "")
        )

    @bot.on_message(filters.command("stream") & filters.group)
    async def stream_command(_: Client, msg: Message) -> None:
        """Direct CDN audio is enabled by default; local media remains the fallback."""
        if stream_only_enabled():
            await msg.reply_text("📡 Stream-only mode: ON\n/play က Direct HTTPS ပဲ သုံးမယ်။ Download fallback ပိတ်ထားတယ်။")
            return
        player = players[msg.chat.id]
        action = msg.command[1].lower() if len(msg.command) > 1 else "status"
        if action == "status":
            await msg.reply_text(
                "📡 Direct HTTPS Streaming: "
                + ("ON" if player.stream_enabled else "OFF")
                + "\n/stream on | /stream off — Group Admin"
            )
            return
        if action not in {"on", "off"}:
            await msg.reply_text("📡 /stream on | /stream off | /stream status")
            return
        if not await require_admin(msg):
            return
        player.stream_enabled = action == "on"
        prepare_next_song(player)
        log.info("Direct stream setting chat %s: %s", msg.chat.id, action.upper())
        await msg.reply_text(
            "📡 Direct HTTPS: " + ("ON" if player.stream_enabled else "OFF")
            + "\nနောက်ထပ် /play မှ စမ်းမယ်။ "
            "မရရင် ပုံမှန် Download Playback ကို ပြန်သုံးမယ်။"
        )

    @bot.on_message(filters.command("shuffle") & filters.group)
    async def shuffle_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        async with player.lock:
            if len(player.queue) < 2:
                await msg.reply_text("🔀 Shuffle အတွက် Queue ထဲ အနည်းဆုံး ၂ ပုဒ် လိုတယ်။")
                return
            count = queue_shuffle(player.queue)
            prepare_next_song(player)
        await msg.reply_text(f"🔀 Next Queue သီချင်း {count} ပုဒ် Shuffle လုပ်ပြီးပြီ။")

    @bot.on_message(filters.command("remove") & filters.group)
    async def remove_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        if len(msg.command) != 2 or not msg.command[1].isdigit():
            await msg.reply_text("📜 /queue ကြည့်ပြီး /remove 1 လို သုံးပါ။")
            return
        player = players[msg.chat.id]
        async with player.lock:
            try:
                track = queue_remove(player.queue, int(msg.command[1]))
                prepare_next_song(player)
            except IndexError:
                await msg.reply_text("⚠️ Queue မှာ အဲဒီ နံပါတ် မရှိဘူး။")
                return
        await msg.reply_text("🗑 Queue ကနေ ဖယ်ပြီးပြီ — " + track.title[:90])

    @bot.on_message(filters.command("clear") & filters.group)
    async def clear_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        async with player.lock:
            count = len(player.queue)
            player.queue.clear()
            discard_prepared(player)
        await msg.reply_text(f"🧹 Next Queue {count} ပုဒ် ရှင်းပြီးပြီ။ လက်ရှိသီချင်း ဆက်ဖွင့်မယ်။")

    @bot.on_message(filters.command("loop") & filters.group)
    async def loop_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        async with player.lock:
            player.loop_current = not player.loop_current
            await update_player_panel(msg.chat.id, player)
            is_on = player.loop_current
        await msg.reply_text("🔁 Repeat current: " + ("ON" if is_on else "OFF"))

    async def skip_in_background(chat_id: int, expected_track: Track) -> None:
        """Switch only the song that was active when /skip was received."""
        player = players[chat_id]
        start = time.monotonic()
        try:
            async with player.lock:
                # A natural song-end may have already changed the track.
                if player.current is expected_track:
                    await advance(chat_id)
                log.info("Skip transition completed in chat %s after %.2fs",
                         chat_id, time.monotonic() - start)
        except asyncio.CancelledError:
            log.info("Skip worker cancelled in chat %s", chat_id)
        except Exception:
            log.exception("Skip transition failed in chat %s", chat_id)
        finally:
            player.skip_pending = False

    def queue_skip(chat_id: int) -> bool:
        player = players[chat_id]
        if player.current is None or player.skip_pending:
            return False
        player.skip_pending = True
        asyncio.create_task(skip_in_background(chat_id, player.current))
        return True

    @bot.on_message(filters.command(["skip", "next"]) & filters.group)
    async def skip_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        if queue_skip(msg.chat.id):
            await msg.reply_text("⏭ Next သီချင်းကို ပြောင်းနေပြီ…")
        else:
            await msg.reply_text("⏳ Skip လုပ်နေပြီးသား (သို့) ဖွင့်ထားတဲ့ သီချင်းမရှိဘူး။")

    @bot.on_message(filters.command(["stop", "end"]) & filters.group)
    async def stop_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        # Preempt any ongoing search or VC join before acquiring the lock.
        was_loading = cancel_active_loading(player)
        discard_prepared(player)
        if was_loading:
            log.info("Stop interrupted loading chat %s", msg.chat.id)
        async with player.lock:
            player.queue.clear()
            player.current = None
            player.started_at = 0.0
            player.paused = False
            player.loop_current = False
            player.idle_mode = None
            player.idle_started_at = 0
            player.idle_notice_sent = False
            await clear_player_panel(msg.chat.id, player)
            clean_temp(player.temp_file)
            player.temp_file = None
            try:
                await asyncio.wait_for(calls.leave_call(msg.chat.id), timeout=12)
            except Exception as exc:
                log.info("VC leave status chat %s: %s", msg.chat.id, type(exc).__name__)
        log.info("Stop completed; queue cleared chat %s", msg.chat.id)
        await msg.reply_text("⏹ Stopped and left VC. /play နဲ့ ပြန်ဖွင့်နိုင်တယ်။")

    @bot.on_message(filters.command(["pause", "resume"]) & filters.group)
    async def pause_resume_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        async with player.lock:
            if player.current is None:
                await msg.reply_text("သီချင်းဖွင့်ထားတာ မရှိသေးဘူး။")
                return
            action = msg.command[0].lower()
            try:
                if action == "pause":
                    await calls.pause(msg.chat.id)
                    if not player.paused:
                        player.paused_since = time.monotonic()
                    player.paused = True
                else:
                    await calls.resume(msg.chat.id)
                    if player.paused and player.paused_since:
                        player.paused_seconds += time.monotonic() - player.paused_since
                    player.paused_since = 0.
                    player.paused = False
                await update_player_panel(msg.chat.id, player)
                await msg.reply_text("⏸ Paused." if action == "pause" else "▶️ Resumed.")
            except Exception:
                log.exception("Could not %s chat %s", action, msg.chat.id)
                await msg.reply_text("⚠️ VC connection ပြန်စစ်ပါ။")

    @bot.on_callback_query(filters.regex(r"^mud:(pause|resume|skip|stop|loop|queue):"))
    async def panel_callback(_: Client, cb: CallbackQuery) -> None:
        """One panel per chat; stale buttons cannot control future tracks."""
        try:
            prefix, action, raw_chat = (cb.data or "").split(":")
            chat_id = int(raw_chat)
        except (ValueError, TypeError):
            await cb.answer("Invalid player control", show_alert=True)
            return
        if not cb.message or cb.message.chat.id != chat_id:
            await cb.answer("This control is not for this group.", show_alert=True)
            return
        player = players[chat_id]
        if player.panel_id != cb.message.id or player.current is None:
            await cb.answer("ဒီ Player Card သက်တမ်းကုန်ပြီ။ /now သုံးပါ။", show_alert=True)
            return
        if action == "queue":
            titles = [item.title for item in player.queue]
            info = queue_display(
                player.current.title, titles[:2], loop=player.loop_current
            )[:195]
            await cb.answer(info, show_alert=True)
            return
        if not await is_chat_admin(chat_id, cb.from_user.id if cb.from_user else None):
            await cb.answer("🛡 Group Admin တွေကပဲ Control နှိပ်နိုင်တယ်။", show_alert=True)
            return
        # Answer promptly; music downloads or reconnection may last >30 seconds.
        # Calling cb.answer() after that makes Telegram return QUERY_ID_INVALID.
        try:
            await cb.answer("⏳ " + action.title())
        except Exception as exc:
            log.info("Player callback acknowledgement expired: %s", type(exc).__name__)
        if action == "stop":
            log.info("Stop requested via player button chat %s", chat_id)
            cancel_active_loading(player)
            discard_prepared(player)
            async with player.lock:
                player.queue.clear()
                player.current = None
                player.paused = False
                player.loop_current = False
                player.started_at = 0
                player.idle_mode = None
                player.idle_started_at = 0
                player.idle_notice_sent = False
                await clear_player_panel(chat_id, player)
                clean_temp(player.temp_file)
                player.temp_file = None
                try:
                    await asyncio.wait_for(calls.leave_call(chat_id), timeout=12)
                except Exception as exc:
                    log.info("Button stop leave status %s: %s", chat_id, type(exc).__name__)
            log.info("Button stop completed; queue cleared chat %s", chat_id)
            return
        if action == "skip":
            if not queue_skip(chat_id):
                log.info("Skip ignored in chat %s: already transitioning", chat_id)
            return
        async with player.lock:
            if player.current is None:
                return
            try:
                if action == "pause":
                    await calls.pause(chat_id)
                    if not player.paused:
                        player.paused_since = time.monotonic()
                    player.paused = True
                elif action == "resume":
                    await calls.resume(chat_id)
                    if player.paused and player.paused_since:
                        player.paused_seconds += time.monotonic() - player.paused_since
                    player.paused_since = 0.
                    player.paused = False
                elif action == "loop":
                    player.loop_current = not player.loop_current
                await update_player_panel(chat_id, player)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.warning("Panel %s failed in chat %s: %s", action, chat_id, type(exc).__name__)
                try:
                    await bot.send_message(chat_id, "⚠️ VC Control မအောင်မြင်ဘူး။ /play ပြန်စမ်းပါ။")
                except Exception:
                    pass
                return

    @calls.on_update(call_filters.chat_update(ChatUpdate.Status.LEFT_CALL))
    async def call_left(_: PyTgCalls, update: ChatUpdate) -> None:
        log.warning("Telegram VC state changed in chat %s (status=%s)", update.chat_id, update.status)
        player = players.get(update.chat_id)
        if player is None:
            return
        async with player.lock:
            if player.current is None and player.idle_mode is None:
                return  # This may be an intentional /stop.
            # Never resurrect an externally closed call; wait for a new /play.
            player.current = None
            player.idle_mode = None
            player.idle_started_at = 0
            player.idle_notice_sent = False
            player.idle_early_ends = 0
            player.paused = False
            player.started_at = 0.0
            player.recovery_attempts = 0
            await clear_player_panel(update.chat_id, player)
            clean_temp(player.temp_file)
            player.temp_file = None
        try:
            await bot.send_message(
                update.chat_id,
                "⚠️ Voice Chat connection ပိတ်/ပြတ်သွားတယ်။ "
                "Group VC ကို ပြန်ဖွင့်ထားပြီး /play သုံးနိုင်တယ်။",
            )
        except Exception:
            pass

    async def watch_voice_connections() -> None:
        """Repair a detached stream even if no StreamEnded update arrives.

        The registry check is local to the PyTgCalls binding and cheap.
        No background music download is attempted: reuse existing media.
        """
        while True:
            await asyncio.sleep(35)
            active = [
                cid for cid, player in players.items()
                if player.current is not None or player.idle_mode is not None
            ]
            if not active:
                continue
            try:
                call_registry = await asyncio.wait_for(calls.calls, timeout=10)
            except Exception as exc:
                log.warning("VC watchdog could not check active calls (%s)", type(exc).__name__)
                continue
            for chat_id in active:
                if chat_id in call_registry:
                    continue
                player = players[chat_id]
                async with player.lock:
                    song = player.current
                    if song is None and player.idle_mode is None:
                        continue
                    if song is None and player.idle_mode is not None:
                        log.warning("Idle VC disconnected chat %s; rechecking", chat_id)
                        if await start_idle(chat_id, player):
                            continue
                        player.idle_mode = None
                        player.idle_started_at = 0
                        player.idle_notice_sent = False
                        player.started_at = 0
                        continue
                    log.warning(
                        "VC watchdog detected disconnected stream chat %s (attempts=%s)",
                        chat_id, player.recovery_attempts,
                    )
                    if player.recovery_attempts < 1:
                        player.recovery_attempts += 1
                        source = player.temp_file or song.source
                        if not source or (player.temp_file and not Path(source).is_file()):
                            log.warning("VC recovery file absent in chat %s", chat_id)
                        else:
                            try:
                                if song.video:
                                    media = MediaStream(
                                        source, AudioQuality.MEDIUM, VideoQuality.SD_360p
                                    )
                                else:
                                    media = MediaStream(
                                        source, AudioQuality.HIGH,
                                        video_flags=MediaStream.Flags.IGNORE,
                                        audio_flags=MediaStream.Flags.REQUIRED,
                                    )
                                await play_with_reconnect(calls, chat_id, media)
                                player.started_at = time.monotonic()
                                log.info("VC watchdog repaired chat %s", chat_id)
                                continue
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                log.warning(
                                    "VC watchdog reconnect unsuccessful in %s (%s)",
                                    chat_id, type(exc).__name__,
                                )
                    player.current = None
                    player.idle_mode = None
                    player.idle_started_at = 0
                    player.idle_notice_sent = False
                    player.paused = False
                    player.started_at = 0.0
                    await clear_player_panel(chat_id, player)
                    clean_temp(player.temp_file)
                    player.temp_file = None
                try:
                    await bot.send_message(
                        chat_id,
                        "⚠️ VC connection ပြတ်သွားတယ်။ Group VC ဆက်ဖွင့်ထားရင် "
                        "/play နဲ့ ပြန်ဖွင့်နိုင်တယ်။",
                    )
                except Exception:
                    pass

    @calls.on_update(call_filters.stream_end())
    async def stream_end(_: PyTgCalls, update: StreamEnded) -> None:
        player = players.get(update.chat_id)
        if player is None or update.stream_type != StreamEnded.Type.AUDIO:
            return
        async with player.lock:
            song = player.current
            if song is None and player.idle_mode is None:
                log.info("Ignored VC end event in chat %s (not playing)", update.chat_id)
                return
            elapsed = time.monotonic() - player.started_at
            if elapsed < 2:
                log.info("Ignored delayed VC audio end in chat %s (%.1fs)", update.chat_id, elapsed)
                return
            if song is None:
                mode = player.idle_mode
                # An unplayable live stream must not create an endless
                # join/restart loop. Back off radio, then use quiet idle.
                if elapsed < 30:
                    player.idle_early_ends += 1
                else:
                    player.idle_early_ends = 0
                if mode == "radio" and elapsed < 30:
                    player.radio_retry_after = time.monotonic() + 10 * 60
                log.info("Idle %s stream ended chat %s after %.1fs (early=%s)",
                         mode, update.chat_id, elapsed, player.idle_early_ends)
                if player.idle_early_ends >= 3:
                    player.idle_mode = None
                    player.idle_started_at = 0
                    player.idle_notice_sent = False
                    player.idle_early_ends = 0
                    try:
                        await asyncio.wait_for(calls.leave_call(update.chat_id), timeout=10)
                    except Exception:
                        pass
                    try:
                        await bot.send_message(
                            update.chat_id,
                            "⚠️ Idle Audio ကို ထပ်ခါတလဲလဲ ဖွင့်မရလို့ VC ထွက်ထားတယ်။ "
                            "/play နဲ့ ပြန်စမ်းနိုင်တယ်။"
                        )
                    except Exception:
                        pass
                    return
                await asyncio.create_task(advance(update.chat_id))
                return
            log.warning(
                "VC audio end chat %s after %.1fs (duration=%s, recovery=%s, queue=%s)",
                update.chat_id, elapsed, song.duration_seconds,
                player.recovery_attempts, len(player.queue),
            )
            # A premature StreamEnded update may be a transient call failure,
            # not the end of the audio. Reuse the same cached local media once.
            if (not player.paused and player.recovery_attempts < 1
                    and ended_early(elapsed, song.duration_seconds)):
                player.recovery_attempts += 1
                source = player.temp_file or song.source
                try:
                    if song.video:
                        stream = MediaStream(
                            source, AudioQuality.MEDIUM, VideoQuality.SD_360p
                        )
                    else:
                        stream = MediaStream(
                            source, AudioQuality.HIGH,
                            video_flags=MediaStream.Flags.IGNORE,
                            audio_flags=MediaStream.Flags.REQUIRED,
                        )
                    await play_with_reconnect(calls, update.chat_id, stream)
                    player.started_at = time.monotonic()
                    log.info("Recovered prematurely ended VC media in chat %s", update.chat_id)
                    return
                except Exception as exc:
                    log.warning(
                        "VC premature-end recovery failed in chat %s (%s)",
                        update.chat_id, type(exc).__name__,
                    )
            try:
                await asyncio.create_task(advance(update.chat_id, repeat=True))
            except asyncio.CancelledError:
                return

    async def health(_: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok" if ready else "starting",
            "active_groups": sum(p.current is not None for p in players.values()),
        }, status=200 if ready else 503)

    server = web.Application()
    server.router.add_get("/", health)
    server.router.add_get("/health", health)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(
        runner, "0.0.0.0", int(os.getenv("PORT", "10000"))
    ).start()

    watchdog_task: asyncio.Task | None = None
    try:
        await calls.start()  # Starts the assistant MTProto client.
        identity = await assistant.get_me()
        if identity.is_bot:
            raise RuntimeError("SESSION_STRING is a bot session, not a user session.")
        log.info("Assistant session verified: user account")
        await bot.start()
        ready = True
        log.info("Music Bot started; maximum active voice chats: %s", max_groups)
        log.info("Named audio stream-only mode: %s", stream_only_enabled())
        watchdog_task = asyncio.create_task(watch_voice_connections())
        await asyncio.Event().wait()
    finally:
        ready = False
        if watchdog_task is not None:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
        for player in players.values():
            clean_temp(player.temp_file)
        await bot.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
