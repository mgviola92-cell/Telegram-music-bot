"""Isolated Telegram Voice Chat Music Bot — MVP.

Telegram audio replies and allowlisted direct HTTPS audio streams only.
Secrets must be configured as Render environment variables.
"""
import asyncio
import logging
import os
import shutil
import signal
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from music_ui import player_controls, queue_display, queue_remove, queue_shuffle, queue_track_title, queue_popup
from audius_source import fetch_audius_audio
from song_cache import get_cached_song, save_cached_song, maintain_cache
from idle_media import configured_radio_url, ensure_idle_audio
from radio_catalog import RadioCatalog
from welcome_audio import WelcomeAudio
from music_owner_store import MusicOwnerStore
from music_owner_ui import MusicOwnerUI
from private_songs import PrivateSongs
from lyrics_service import LyricsUI
from youtube_health import youtube_guard
from archive_source import get_archive_audio
from prefetch import PreparedAudio, AsyncPreparedAudio
from playback_prepare import warm_voice
from stream_probe import probe_audio_url
from network_audio import DirectAudioStream
from youtube_relay import (RelayAudioStream, resolve_request as resolve_youtube_relay,
                           source_id as relay_source_id, enabled as youtube_relay_enabled,
                           sender_startup_timeout)
from audio_options import AudioSettings, ProcessedAudioStream, EFFECTS, EFFECT_LABELS
from music_search import collect_choices, SearchSessions, youtube_title, related_query, choose_related, title_key, restricted_error, block_source, source_blocked
from music_stats import record_play, stats_text, stats_popup
from music_cleanup import housekeeping
from feature_probe import run_feature_probe
from bot_source_probe import run_bot_source_probe
from lybot_source import LyBotSource, BotSourceUnavailable, resolve_with_fallback, run_live_probe, run_candidate_probes, APPROVED_BOTS, search_text
from voice_transport import clear_stale_voice, ensure_native_sender, AudioTransportUnavailable
from source_race import ParallelSources
from effect_exports import EffectExports
from track_metadata import remember_metadata, lookup_metadata
from queue_receipts import acknowledge_queued, schedule_receipts
from song_recognition import SongRecognizer, recognition_media, send_recognized_audio, DeliveredAudioCache
from pyrogram.enums import ParseMode
from types import SimpleNamespace
from direct_stream import (resolve_soundcloud_https,
                           cached_search, remember_search, forget_search)
from player_visuals import (player_caption, artwork_for, artwork_is_safe, stream_headers_for, allowed_audio_cdn, save_artwork)
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import AudioQuality, ChatUpdate, GroupCallConfig, MediaStream, StreamEnded, VideoQuality, GroupCallParticipant, UpdatedGroupCallParticipant

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
    metadata_resolved: bool = False
    metadata_failed: bool = False
    queue_receipt: object | None = None
    queue_receipt_text: str | None = None
    selected: object | None = None
    autoplay: bool = False
    output_speed: float = 1.0
    original_duration_seconds: float | None = None
    queue_token: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    requester: str = "—"
    requester_id: int | None = None
    control_actor: str = ""
    control_actor_id: int | None = None
    relay_requested: bool = False


@dataclass
class Player:
    queue: deque = field(default_factory=deque)
    current: Track | None = None
    loading_track: Track | None = None
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
    radio_station: str | None = None
    radio_panel_id: int | None = None
    radio_revision: int = 0
    welcome_panel_id: int | None = None
    keep_idle: bool = True
    idle_notice_sent: bool = False
    idle_early_ends: int = 0
    prepared: PreparedAudio | None = None
    skip_pending: bool = False
    voice_warmer: object | None = None
    # UI timestamp excludes actual paused time.
    paused_since: float = 0.0
    paused_seconds: float = 0.0
    panel_is_photo: bool = False
    progress_task: asyncio.Task | None = None
    announce_task: asyncio.Task | None = None
    stream_enabled: bool = True
    resolver: object | None = None
    metadata_tasks: dict = field(default_factory=dict)
    queue_changed: object | None = None
    queue_refresh_task: asyncio.Task | None = None
    queue_revision: int = 0
    control_menu: str = "main"
    audio_settings: AudioSettings = field(default_factory=AudioSettings)
    playing_settings: AudioSettings = field(default_factory=AudioSettings)
    autoplay_enabled: bool = False
    autoplay_task: asyncio.Task | None = None
    autoplay_prepare: object | None = None
    recent_tracks: deque = field(default_factory=lambda: deque(maxlen=30))


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


def audio_stream(source: str, quality=AudioQuality.HIGH, *, settings=None):
    if relay_source_id(source):
        return RelayAudioStream(source, settings)
    if settings is not None and settings != AudioSettings():
        if allowed_audio_cdn(source):
            return DirectAudioStream(source, settings)
        return ProcessedAudioStream(source, settings)
    if allowed_audio_cdn(source):
        return DirectAudioStream(source)
    return MediaStream(source, quality, video_flags=MediaStream.Flags.IGNORE,
                       audio_flags=MediaStream.Flags.REQUIRED)


async def play_with_reconnect(calls: PyTgCalls, chat_id: int, stream: MediaStream) -> None:
    welcome = getattr(calls, "welcome_audio", None)
    if not isinstance(welcome, WelcomeAudio):
        return await _play_with_reconnect(calls, chat_id, stream)
    wrapped, pipe = welcome.prepare(chat_id, stream)
    try:
        await _play_with_reconnect(calls, chat_id, wrapped)
    except BaseException:
        if pipe:
            pipe.close()
        raise
    welcome.commit(chat_id, pipe)


async def _play_with_reconnect(calls: PyTgCalls, chat_id: int, stream: MediaStream) -> None:
    """Recover once from a stale per-chat voice connection.

    Do not create missing group calls; a group admin must start the VC.
    """
    try:
        started = time.monotonic()
        await asyncio.wait_for(
            calls.play(chat_id, stream, GroupCallConfig(auto_start=False)),
            timeout=40,
        )
        log.info("VC stream setup chat %s after %.2fs", chat_id, time.monotonic()-started)
        started = time.monotonic()
        await ensure_native_sender(calls, chat_id, startup_timeout=sender_startup_timeout(stream))
        log.info("VC sender check chat %s after %.2fs", chat_id, time.monotonic()-started)
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

        await ensure_native_sender(calls, chat_id, startup_timeout=sender_startup_timeout(stream))

class AudioFramesStalled(RuntimeError):
    """The decoder was accepted but its outgoing media clock never advanced."""


async def wait_for_audio_frames(calls: PyTgCalls, chat_id: int, *,
                                timeout: float = 8, interval: float = .25) -> None:
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
    player.radio_revision += 1
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
    if player.queue_changed is not None:
        player.queue_changed()
    upcoming = player.queue[0] if player.queue else None
    if (player.prepared is not None and player.prepared.track is upcoming
            and player.prepared.prefer_stream == player.stream_enabled):
        return
    discard_prepared(player)
    if upcoming is None and player.current is not None and player.autoplay_enabled and player.autoplay_prepare:
        player.autoplay_prepare()
    if (upcoming is not None and player.current is not None
            and upcoming.youtube and not upcoming.video
            and os.getenv("MUSIC_PREFETCH_ENABLED", "1") != "0"):
        if player.resolver is not None:
            player.prepared = AsyncPreparedAudio(upcoming, player.resolver, clean_temp,
                                                 prefer_stream=player.stream_enabled)
        else:
            player.prepared = PreparedAudio(
                upcoming, download_song, clean_temp, prefer_stream=player.stream_enabled,
            )
        player.prepared.on_metadata = player.queue_changed
        player.prepared.prime_stream(audio_stream, player.audio_settings)
        log.info("Prefetch began for next queued audio")


def drop_autoplay(player):
    task = player.autoplay_task
    player.autoplay_task = None
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    automatic = any(song.autoplay for song in player.queue)
    if automatic:
        player.queue = deque(song for song in player.queue if not song.autoplay)
        discard_prepared(player)


def cancel_queue_metadata(player):
    if player.queue_changed is not None:
        player.queue_changed()
    for task in list(player.metadata_tasks.values()):
        task.cancel()
    player.metadata_tasks.clear()


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
    match_terms = set(re.findall(r"[a-z0-9]+", search_text(query)))
    if len(match_terms) >= 2:
        actual = set(re.findall(r"[a-z0-9]+", search_text(title))) | set(
            re.findall(r"[a-z0-9]+", search_text(artist)))
        matched = match_terms & actual
        return len(matched) >= max(2, len(match_terms) - 1)
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
            if not urls:
                remember_metadata(query, title + (" – " + str(entry["artist"]) if entry.get("artist") else "") + " · SoundCloud", entry.get("duration"))
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
    if not video and allowed_url(query, {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}):
        yield ("SoundCloud", query)
        return
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
    return os.getenv("MUSIC_STREAM_ONLY", "0") != "0"


def resolve_stream_only(query: str, cancel_signal: threading.Event | None = None):
    """Return approved HTTPS audio only; never create or fetch a local media file."""
    selected_url = allowed_url(query, {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
    targets = [query] if selected_url else soundcloud_results(query, max_tracks=3, cancel_signal=cancel_signal)
    for target in targets:
        if source_blocked(target):
            continue
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Stream lookup cancelled")
        try:
            direct = resolve_soundcloud_https(target, query, cancel_signal=cancel_signal,
                                             check=None if selected_url else suitable_recording)
            if direct is not None and allowed_audio_cdn(direct[0]):
                log.info("Stream-only audio resolved; disk download and file cache bypassed")
                return direct
        except InterruptedError:
            raise
        except Exception as exc:
            if restricted_error(exc):
                block_source(target)
            log.info("Stream-only candidate unavailable (%s)", type(exc).__name__)
    if cancel_signal is not None and cancel_signal.is_set():
        raise InterruptedError("Stream lookup cancelled")
    raise StreamOnlyPlaybackError("No playable direct HTTPS audio URL; downloading is disabled")


async def run_stream_probe(query: str) -> None:
    cancel = threading.Event()
    try:
        log.info("FFmpeg-only diagnostic started; playback configuration unchanged")
        url, _, _ = await asyncio.wait_for(
            asyncio.to_thread(resolve_stream_only, query, cancel), timeout=45,
        )
        for variant in ("library", "combined_headers", "hls_no_eof"):
            result = await probe_audio_url(url, stream_headers_for(url), variant=variant)
            log.info("FFmpeg-only diagnostic result: %s", result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("FFmpeg-only diagnostic unavailable (%s)", type(exc).__name__)
    finally:
        cancel.set()


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
        if provider == "SoundCloud" and source_blocked(target):
            failures.append("SoundCloud restricted")
            continue
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
                    check=None if allowed_url(query, {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}) else suitable_recording,
                )
                abort_if_stopped()
                if direct is not None:
                    return direct
                log.info("Direct SoundCloud URL unavailable; normal download fallback")
            except InterruptedError:
                raise
            except Exception as exc:
                if restricted_error(exc):
                    block_source(target)
                    failures.append("SoundCloud restricted")
                    log.info("SoundCloud track is restricted; skipping file download")
                    continue
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
            if (not video and not is_youtube_url(query)
                    and not allowed_url(query, {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
                    and not suitable_recording(title, query, str(result.get("uploader") or ""))):
                raise ValueError("Non-matching or altered recording")
            log.info("Resolved music through %s in %.2fs (total=%.2fs; track title may differ)",
                     provider, time.monotonic() - provider_started,
                     time.monotonic() - resolution_started)
            if provider == "YouTube":
                youtube_guard.record_success()
            label = f"{title} · {provider}"
            from song_links import remember_link
            remember_link(label,result.get("webpage_url") or target)
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
            if provider == "SoundCloud" and restricted_error(exc):
                block_source(target)
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
    max_groups = max(1, min(8, int(os.getenv("MAX_ACTIVE_GROUPS", "6"))))
    players: dict[int, Player] = defaultdict(Player)
    ready = False
    fresh_voice_chats: set[int] = set()
    shutdown = asyncio.Event()

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
    radio_catalog = RadioCatalog()
    owner_settings = MusicOwnerStore(assistant)
    from card_layout import configure as configure_cards
    configure_cards(lambda: owner_settings.data.get("cards", {}))
    welcome = WelcomeAudio(owner_settings,bot=bot)
    owner_ui = MusicOwnerUI(bot,owner_settings,welcome,players)
    owner_ui.register()
    effect_exports = EffectExports(bot,owner_settings,players)
    calls.welcome_audio = welcome
    source_names = [name.strip() for name in os.getenv("MUSIC_BOT_SOURCES", "LyBot,Song_Vkm_bot,VoiceShazamBot").split(",")
                    if name.strip() in APPROVED_BOTS]
    lybot = (ParallelSources([LyBotSource(assistant, name) for name in dict.fromkeys(source_names)], clean_temp)
             if os.getenv("LYBOT_ENABLED", "1") != "0" else None)
    recognition_source = next((s for s in lybot.sources if s.username == "VoiceShazamBot"), None) if lybot else None
    recognizer = SongRecognizer(bot, recognition_source) if recognition_source else None
    recognition_results = {}
    delivered_audio = DeliveredAudioCache()
    search_sessions = SearchSessions()
    search_slots = asyncio.Semaphore(2)
    search_cooldowns = {}
    metadata_slots = asyncio.Semaphore(2)

    async def resolve_for_queue(song, prefer_stream):
        strict = stream_only_enabled()
        if song.relay_requested:
            try:
                relay = await resolve_youtube_relay(song.source)
                log.info('YouTube relay metadata ready; direct POST audio selected')
                return relay.source, relay.title, relay.duration
            except (asyncio.CancelledError, InterruptedError):
                raise
            except Exception as exc:
                log.info('YouTube relay lookup unavailable (%s); using existing music sources', type(exc).__name__)
                query = await youtube_title(song.source) if is_youtube_url(song.source) else song.source
                song = replace(song, source=query, title=query, relay_requested=False)
        if song.selected is not None:
            if strict:
                raise StreamOnlyPlaybackError("Selected Telegram audio requires a file transfer")
            try:
                return await song.selected.source.download(song.selected)
            except (asyncio.CancelledError, InterruptedError):
                raise
            except Exception as exc:
                log.info("Selected Telegram audio unavailable (%s); matching other sources", type(exc).__name__)
                # Match the selected title/artist, never fall back to a broad original query.
                requested = Track(queue_track_title(song), queue_track_title(song), youtube=True)
                primary = PreparedAudio(requested, download_song, clean_temp, prefer_stream=prefer_stream, publish_metadata=False)
                providers = [s for s in lybot.sources if s is not song.selected.source] if lybot else []
                return await ParallelSources(providers, clean_temp).race(primary, requested.source)
        primary = PreparedAudio(song, download_song, clean_temp, prefer_stream=prefer_stream, publish_metadata=False)
        if lybot is not None and not song.video and not song.source.startswith(("http://", "https://")) and not strict:
            return await lybot.race(primary, song.source)
        try:
            return await primary.consume()
        except (asyncio.CancelledError, InterruptedError):
            raise
        except Exception as exc:
            if (lybot is None or song.video or strict or not song.metadata_resolved
                    or not allowed_url(song.source, {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})):
                raise
            log.info("Selected SoundCloud audio unavailable (%s); matching selected title on bot sources", type(exc).__name__)
            return await lybot.resolve(queue_track_title(song))

    from shared_audio import SharedAudio
    shared_audio=SharedAudio(resolve_for_queue,clean_temp)
    resolve_for_queue=shared_audio.resolve

    async def resolve_private_song(query):
        if not isinstance(query,str):
            if stream_only_enabled():raise StreamOnlyPlaybackError("Private file requests disabled")
            return await resolve_for_queue(choice_track(query),False)
        if stream_only_enabled():
            raise StreamOnlyPlaybackError("Private file requests disabled in strict stream-only mode")
        if is_youtube_url(query):
            query=await youtube_title(query)
            if not query:raise ValueError("No public song title")
        elif query.startswith(("http://","https://")) and not allowed_url(query,{
                "soundcloud.com","www.soundcloud.com","m.soundcloud.com"}):
            raise ValueError("Unsupported song URL")
        return await resolve_for_queue(Track(query,query,youtube=True),False)

    async def search_private_songs(query):
        if is_youtube_url(query):query=await youtube_title(query)
        if query.startswith(("http://","https://")):
            if not allowed_url(query,{"soundcloud.com","www.soundcloud.com","m.soundcloud.com"}):
                raise ValueError("Unsupported song URL")
            from music_search import SearchChoice
            return [SearchChoice(query,query)]
        return await collect_choices(query,lybot.sources if lybot and not stream_only_enabled() else (),limit=10,timeout=10)

    def current_lyrics(chat,panel):
        player=players.get(chat)
        if not player or not player.current or player.panel_id!=panel:return None
        return queue_track_title(player.current),player.current.duration_seconds
    lyrics_ui=LyricsUI(bot,current_lyrics)
    lyrics_ui.register()
    private_songs=PrivateSongs(bot,resolve_private_song,players,clean_temp,search_private_songs,lyrics_ui.button)
    private_songs.register()

    async def describe_primary(query):
        known = lookup_metadata(query)
        if known:
            return known
        await asyncio.to_thread(soundcloud_results, query, 1)
        return lookup_metadata(query)

    def describe_queued(player, track):
        if (not track.youtube or track.video or track.metadata_resolved or lybot is None
                or (player.prepared is not None and player.prepared.track is track)):
            return
        async def update():
            try:
                async with metadata_slots:
                    if track.relay_requested:
                        try:
                            relay = await resolve_youtube_relay(track.source)
                            result = relay.title, relay.duration
                        except Exception:
                            result = None
                    else:
                        result = await lybot.describe(track.source, describe_primary)
                if result and any(item is track for item in player.queue) and not track.metadata_resolved:
                    track.title, track.duration_seconds = result
                    track.metadata_resolved = True
                    track.metadata_failed = False
                    if player.queue_changed is not None:
                        player.queue_changed()
                elif not result and any(item is track for item in player.queue):
                    track.metadata_failed = True
                    if player.queue_changed is not None:
                        player.queue_changed()
            finally:
                row = player.metadata_tasks.get(id(track))
                if row is asyncio.current_task():
                    player.metadata_tasks.pop(id(track), None)
        player.metadata_tasks[id(track)] = asyncio.create_task(update())

    def choice_track(choice, *, automatic=False):
        return Track(choice.title, choice.source, youtube=True, duration_seconds=choice.duration,
                     metadata_resolved=True, selected=choice.candidate, autoplay=automatic)

    def start_autoplay(player):
        if (not player.autoplay_enabled or player.current is None or player.current.video
                or player.queue or (player.autoplay_task and not player.autoplay_task.done())):
            return
        seed = player.current
        async def recommend():
            try:
                async with search_slots:
                    choices = await collect_choices(related_query(seed.title),
                        lybot.sources if lybot and not stream_only_enabled() else (), timeout=9)
                choice = choose_related(choices, player.recent_tracks)
                if (choice and player.autoplay_enabled and player.current is seed and not player.queue):
                    player.queue.append(choice_track(choice, automatic=True))
                    prepare_next_song(player)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("Autoplay recommendation unavailable (%s)", type(exc).__name__)
            finally:
                if player.autoplay_task is asyncio.current_task():
                    player.autoplay_task = None
        player.autoplay_task = asyncio.create_task(recommend())

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

    from telegram_photo_cache import TelegramPhotoCache, send_default_photo
    photo_cache = TelegramPhotoCache()

    async def edit_panel(chat_id: int, player: Player) -> None:
        if player.panel_id is None or player.current is None:
            return
        song = player.current
        text = player_caption(
            song.title, elapsed_for(player), song.duration_seconds,
            paused=player.paused, requester=song.requester, requester_id=song.requester_id,
            html_mode=True,actor=song.control_actor,actor_id=song.control_actor_id,
        )
        buttons = player_controls(
            chat_id, paused=player.paused,
            loop=player.loop_current, title=song.title,
            normalize=player.audio_settings.normalize, quality=player.audio_settings.quality,
            effect=player.audio_settings.effect, menu=player.control_menu, queue_items=list(player.queue),
        )
        try:
            if player.panel_is_photo:
                await bot.edit_message_caption(
                    chat_id, player.panel_id, caption=text, parse_mode=ParseMode.HTML,
                    reply_markup=buttons,
                )
            else:
                await bot.edit_message_text(
                    chat_id, player.panel_id, text, parse_mode=ParseMode.HTML,
                    reply_markup=buttons,
                )
        except Exception as exc:
            if "MESSAGE_NOT_MODIFIED" not in str(exc).upper():
                log.info("Player UI update unavailable %s (%s)", chat_id, type(exc).__name__)

    async def update_player_panel(chat_id: int, player: Player) -> None:
        if player.queue_changed is not None:
            player.queue_changed()
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
        from default_artwork import default_photo
        title = player_caption(
            song.title, elapsed_for(player), song.duration_seconds,
            paused=player.paused, requester=song.requester, requester_id=song.requester_id,
            html_mode=True,actor=song.control_actor,actor_id=song.control_actor_id,
        )
        if admin_muted:
            title += "\n\n🔇 VC Admin က Assistant ကို mute ထားတယ်။ Unmute / Allow to Speak ပေးပါ။"
        buttons = player_controls(
            chat_id, paused=player.paused, loop=player.loop_current,
            title=song.title, normalize=player.audio_settings.normalize,
            quality=player.audio_settings.quality, effect=player.audio_settings.effect,
            menu=player.control_menu, queue_items=list(player.queue),
        )
        previous = player.panel_id
        panel = None
        is_photo = False
        try:
            if image and artwork_is_safe(image):
                try:
                    panel = await asyncio.wait_for(
                        photo_cache.send(bot, chat_id, image, image, caption=title, reply_markup=buttons, parse_mode=ParseMode.HTML, reply_to_message_id=getattr(progress,"reply_to_message_id",None)),
                        timeout=5,
                    )
                    is_photo = True
                except Exception as exc:
                    log.info("Artwork unavailable (%s); using lightweight text UI",
                             type(exc).__name__)
            if panel is None and not song.video:
                try:
                    panel = await asyncio.wait_for(send_default_photo(photo_cache, bot, chat_id, owner_settings.data.get("default_photo"), caption=title, reply_markup=buttons, parse_mode=ParseMode.HTML, reply_to_message_id=getattr(progress,"reply_to_message_id",None)),timeout=5)
                    is_photo = True
                except Exception as exc:
                    log.info("Default artwork unavailable (%s)",type(exc).__name__)
            if panel is None:
                if progress is not None:
                    try:
                        panel = await progress.edit_text(title, reply_markup=buttons,parse_mode=ParseMode.HTML)
                    except Exception:
                        panel = None
                if panel is None:
                    panel = await bot.send_message(chat_id, title, reply_markup=buttons,parse_mode=ParseMode.HTML)
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
            player.progress_task = None

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("Unable to post player card in %s (%s)", chat_id, type(exc).__name__)

    async def clear_player_panel(chat_id: int, player: Player) -> None:
        if player.queue_changed is not None:
            player.queue_changed()
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
        chat_id: int, player: Player, progress: Message | None = None, *, quiet: bool = False
    ) -> bool:
        """Preserve a live VC after the queue ends without faking radio audio.

        Radio requires a verified, authorized station configured by its owner.
        Otherwise play a tiny silent loop to remain connected. Caller holds lock.
        """
        if not player.keep_idle:
            return False
        options: list[tuple[str, str]] = []
        radio = (
            (radio_catalog.stations[player.radio_station].url
             if player.radio_station in radio_catalog.stations else configured_radio_url()) if player.radio_enabled and not quiet
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
        warm_task = None
        try:
            if (player.current is None and player.idle_mode is None and player.queue
                    and player.voice_warmer is not None):
                warm_task = asyncio.create_task(player.voice_warmer())
            if (not player.queue and player.current is not None and player.autoplay_enabled
                    and not (repeat and player.loop_current)):
                start_autoplay(player)
                if player.autoplay_task is not None:
                    try:
                        await asyncio.wait_for(asyncio.shield(player.autoplay_task), timeout=10)
                    except TimeoutError:
                        drop_autoplay(player)
                    except asyncio.CancelledError:
                        if task.cancelling():
                            raise
                        # Manual /play or /autoplay off canceled only the recommendation.
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
                player.loading_track = song
                settings = player.audio_settings
                resolve_started = time.monotonic()
                local_file = None
                prepared = None
                try:
                    source = song.source
                    if song.youtube:
                        use_lybot = (lybot is not None and not song.video
                                     and not stream_only_enabled() and not is_youtube_url(source))
                        if player.resolver is not None:
                            prepared = player.prepared
                            player.prepared = None
                            if (prepared is None or prepared.track is not song
                                    or prepared.prefer_stream != player.stream_enabled):
                                if prepared is not None:
                                    prepared.discard()
                                prepared = AsyncPreparedAudio(song, player.resolver, clean_temp,
                                                              prefer_stream=player.stream_enabled)
                            local_file, title, duration_seconds = await prepared.consume()
                        elif use_lybot:
                            prepared = player.prepared
                            player.prepared = None
                            if (prepared is None or prepared.track is not song
                                    or prepared.prefer_stream != player.stream_enabled):
                                if prepared is not None:
                                    prepared.discard()
                                prepared = PreparedAudio(song, download_song, clean_temp,
                                                         prefer_stream=player.stream_enabled)
                            local_file, title, duration_seconds = await resolve_with_fallback(
                                prepared, source, lybot)
                        else:
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
                        song.metadata_resolved = True
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
                    if warm_task is not None:
                        await warm_task
                        warm_task = None
                    if chat_id not in fresh_voice_chats:
                        try:
                            await asyncio.wait_for(clear_stale_voice(assistant, chat_id, identity.id), timeout=8)
                        except Exception as stale_exc:
                            log.info("Previous VC participation check unavailable (%s)", type(stale_exc).__name__)
                        fresh_voice_chats.add(chat_id)
                    log.info("VC play requested chat %s (direct=%s, source_ready=%.2fs)",
                             chat_id, allowed_audio_cdn(source), time.monotonic() - resolve_started)
                    connection_started = time.monotonic()
                    try:
                        # Stream construction may probe a remote URL and fail.
                        # Include it in the same fallback as calls.play().
                        if song.video:
                            stream = MediaStream(
                                source, AudioQuality.MEDIUM, VideoQuality.SD_360p,
                            )
                        else:
                            stream = (await prepared.consume_stream(source, settings)
                                      if prepared is not None else None)
                            if stream is None:
                                stream = (audio_stream(source) if settings == AudioSettings() else
                                          audio_stream(source, settings=settings))
                        await play_with_reconnect(calls, chat_id, stream)
                        if not song.video:
                            await wait_for_audio_frames(calls, chat_id)
                    except Exception as live_exc:
                        if isinstance(live_exc, AudioTransportUnavailable):
                            raise
                        if relay_source_id(source):
                            log.warning('YouTube relay VC audio unavailable (%s); trying existing sources', type(live_exc).__name__)
                            fallback_query = queue_track_title(song).rsplit(' · YouTube', 1)[0]
                            local_file, title, duration_seconds = await resolve_for_queue(
                                Track(fallback_query, fallback_query, youtube=True), player.stream_enabled)
                            song.duration_seconds, song.title = duration_seconds, title
                            song.metadata_resolved = True
                            source = local_file
                        elif song.youtube and not song.video and stream_only_enabled():
                            log.warning("Stream-only playback failed (%s); download fallback disabled",
                                        type(live_exc).__name__)
                            raise StreamOnlyPlaybackError(
                                "Direct stream produced no usable audio; download fallback disabled"
                            ) from live_exc
                        elif not (allowed_audio_cdn(source) and song.youtube):
                            raise
                        else:
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
                            song.metadata_resolved = True
                            source = local_file
                        stream = (audio_stream(source) if settings == AudioSettings() else
                                  audio_stream(source, settings=settings))
                        await play_with_reconnect(calls, chat_id, stream)
                        await wait_for_audio_frames(calls, chat_id)
                    log.info("VC play success chat %s (connection_and_frame_check=%.2fs)", chat_id, time.monotonic() - connection_started)
                    song.output_speed = settings.speed if not song.video else 1.0
                    player.playing_settings = settings
                    if song.youtube or song.original_duration_seconds is None:
                        song.original_duration_seconds = song.duration_seconds
                    if song.original_duration_seconds:
                        song.duration_seconds = song.original_duration_seconds / song.output_speed
                    song.control_actor=""
                    song.control_actor_id=None
                    player.current = song
                    player.control_menu = "main"
                    player.recent_tracks.append(title_key(song.title))
                    asyncio.create_task(record_play(chat_id, song.title))
                    if player.queue_changed is not None:
                        player.queue_changed()
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
                    elif isinstance(exc, BotSourceUnavailable):
                        error_message = "⚠️ သီချင်းရင်းမြစ်တွေက အချိန်မီ audio မပေးနိုင်ဘူး။ သီချင်းနာမည်နဲ့ အဆိုတော် ထည့်ပြီး ပြန်စမ်းပါ။"
                    elif type(exc).__name__ == "NoActiveGroupCall":
                        error_message = ("🎙 Group Voice Chat ကို Admin က ဖွင့်ပေးပါ။ "
                                         "Assistant က /play နဲ့ အလိုအလျောက် ဝင်ပေးမယ်။")
                    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
                        song.telegram_file and "download" in str(exc).lower()
                    ):
                        error_message = ("⚠️ Telegram ဖိုင် ယူမရသေးဘူး။ ခဏနေ ပြန်စမ်းပါ။" if song.telegram_file else
                                         "⚠️ သီချင်းရင်းမြစ်က အချိန်မီ audio မပေးနိုင်ဘူး။ ခဏနေ ပြန်စမ်းပါ။")
                    elif isinstance(exc, AudioTransportUnavailable):
                        error_message = "🔇 VC အသံပို့တဲ့ connection မရသေးဘူး။ Assistant ကို Allow to Speak ပေးထားလား စစ်ပါ။"
                    elif isinstance(exc, AudioFramesStalled):
                        error_message = "🔇 VC ချိတ်ထားပေမဲ့ အသံ data မထွက်လာဘူး။ ပြန်ချိတ်ဖို့ /stop ပြီး /play စမ်းပါ။"
                    elif type(exc).__name__ == "NoAudioSourceFound":
                        error_message = "🔇 သီချင်းရင်းမြစ်မှာ ဖွင့်နိုင်တဲ့ audio မတွေ့ဘူး။ အခြားသီချင်း သို့မဟုတ် audio file နဲ့ စမ်းပါ။"
                    elif song.youtube:
                        error_message = (
                            "⚠️ ရွေးထားတဲ့သီချင်းအတွက် ဖွင့်နိုင်တဲ့ audio ကို ရင်းမြစ်တွေမှာ မရသေးဘူး။ "
                            "အခြား version ရွေးပါ၊ သို့မဟုတ် သီချင်းနာမည်နဲ့ အဆိုတော် ထည့်ပြီး ပြန်ရှာပါ။"
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
            if warm_task is not None:
                warm_task.cancel()
                await asyncio.gather(warm_task, return_exceptions=True)
            if player.loading_task is task:
                player.loading_track = None
                player.loading_task = None
                player.cancel_download = None

    @bot.on_message(filters.group,group=-20)
    async def observe_music_group(_: Client,msg: Message) -> None:
        owner_settings.observe_group(msg.chat)

    from music_home import MusicHome
    home=MusicHome(bot,owner_settings,players,photo_cache)
    home.register()

    @bot.on_message(filters.command(["start", "help", "cmd"]))
    async def help_command(_: Client, msg: Message) -> None:
        await home.show(msg,help_page=(msg.command or ['start'])[0] in {'help','cmd'})

    @bot.on_message(
        filters.command(["play", "ytplay", "vplay", "ping", "stop", "queue", "radio", "idle", "stream"]) &
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

    @bot.on_message(filters.command(["play", "p", "ytplay", "vplay"]) & filters.group)
    async def play_command(_: Client, msg: Message) -> None:
        private_songs.preempt()
        video_mode = msg.command[0].lower() == "vplay"
        link_progress = None
        selected_track = getattr(msg, "selected_track", None)
        replied = msg.reply_to_message
        media = None
        if replied:
            media = replied.video if video_mode else replied.audio
            if not media and replied.document:
                doc = replied.document
                allowed_mime = "video/mp4" if video_mode else "audio/"
                if (doc.mime_type or "").startswith(allowed_mime):
                    media = doc

        if selected_track is not None:
            track = selected_track
        elif media:
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
                metadata_resolved=True,
            )
        elif len(msg.command) >= 2:
            query = msg.text.split(maxsplit=1)[1].strip()
            if not query or len(query) > (2048 if query.startswith(("https://", "http://")) else 120):
                await msg.reply_text("⚠️ သီချင်းနာမည် 1–120 စာလုံးအတွင်း ထည့်ပေးပါ။")
                return
            if (not video_mode and youtube_relay_enabled()
                    and (msg.command[0].lower() == 'ytplay' or is_youtube_url(query))):
                track = Track(query, query, youtube=True, relay_requested=True)
            elif msg.command[0].lower() == 'ytplay':
                await msg.reply_text('YouTube စမ်းသပ် source ကို လောလောဆယ် ပိတ်ထားတယ်။ /play နဲ့ သုံးပါ။')
                return
            elif query.startswith(("https://", "http://")):
                if is_youtube_url(query):
                    if not youtube_audio_enabled() and not video_mode:
                        link_progress = await msg.reply_text("🔎 Link ကနေ သီချင်းရှာနေတယ်…")
                        try:
                            title = await youtube_title(query)
                        except Exception:
                            await link_progress.edit_text("⚠️ ဒီ Link ရဲ့ သီချင်းနာမည် မရသေးဘူး။ /play <သီချင်းနာမည်> နဲ့ စမ်းပါ။")
                            return
                        track = Track(title, title, youtube=True)
                    elif not youtube_audio_enabled():
                        await msg.reply_text("⚠️ YouTube video ဖွင့်မရသေးဘူး။ MP4 ကို reply /vplay သုံးပါ။")
                        return
                    else:
                        track = Track("YouTube video" if video_mode else "YouTube audio",
                                      query, video=video_mode, youtube=True)
                elif not video_mode and allowed_url(query, {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}):
                    track = Track("သီချင်းရှာနေဆဲ", query, youtube=True)
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

        request_user = getattr(msg,"from_user",None)
        track.requester = (request_user.first_name or "—") if request_user else "Anonymous admin"
        track.requester_id = getattr(request_user,"id",None)
        player = players[msg.chat.id]
        player.resolver = resolve_for_queue
        player.voice_warmer = lambda chat_id=msg.chat.id: warm_voice(
            calls, assistant, chat_id, identity.id, fresh_voice_chats)
        player.autoplay_prepare = lambda: start_autoplay(player)
        drop_autoplay(player)
        player.queue_changed = lambda: schedule_receipts(player)
        log.info("Play mode chat %s: direct=%s", msg.chat.id, player.stream_enabled)
        try:
            progress = link_progress or await msg.reply_text("⏳ Assistant အလိုအလျောက်ဝင်ပြီး VC ချိတ်နေတယ်...")
            log.info("Music request acknowledged in chat %s", msg.chat.id)
        except Exception:
            log.exception("Cannot acknowledge /play in chat %s", msg.chat.id)
            return
        try:
            # An existing local VC session already proves assistant membership.
            # Avoid a redundant Telegram membership RPC for every queued /play.
            if ((player.current is None and player.idle_mode is None)
                    or msg.chat.id not in await calls.calls):
                await ensure_assistant_in_group(msg.chat)
        except RuntimeError as exc:
            await progress.edit_text("⚠️ " + str(exc))
            return
        except Exception:
            log.exception("Assistant auto-join failed in chat %s", msg.chat.id)
            await progress.edit_text("⚠️ Assistant Auto Join မရသေးဘူး။ Admin / Invite Settings စစ်ပါ။")
            return
        await progress.edit_text("🔎 သီချင်းရှာပြီး VC ဖွင့်ဖို့ ပြင်ဆင်နေတယ်...")
        from queue_guard import duplicate,duplicate_text
        existing=duplicate(player,track)
        if existing:
            await progress.edit_text(duplicate_text(player,existing),parse_mode=ParseMode.HTML)
            return
        # Do not make another /play wait for a long download to release
        # the chat lock; safely append while an existing loader owns it.
        if player.loading_task is not None and not player.loading_task.done():
            if len(player.queue) >= MAX_QUEUE:
                await progress.edit_text("⚠️ Queue ပြည့်နေပြီ (10 tracks)။")
                return
            from queue_guard import admit,duplicate_text
            existing=admit(player,track)
            if existing:
                await progress.edit_text(duplicate_text(player,existing),parse_mode=ParseMode.HTML)
                return
            prepare_next_song(player)
            describe_queued(player, track)
            await acknowledge_queued(player, track, progress)
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

            from queue_guard import admit,duplicate_text
            existing=admit(player,track)
            if existing:
                await progress.edit_text(duplicate_text(player,existing),parse_mode=ParseMode.HTML)
                return
            if player.current is not None:
                prepare_next_song(player)
                describe_queued(player, track)
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
                await acknowledge_queued(player, track, progress)

    @bot.on_message(filters.command("search") & filters.group)
    async def search_command(_: Client, msg: Message) -> None:
        query = (msg.text or "").split(maxsplit=1)[1].strip() if len(msg.command) > 1 else ""
        if not query or len(query) > 120 or query.startswith(("http://", "https://", "/")):
            await msg.reply_text("🔎 /search <သီချင်းနာမည်> — ၅ ပုဒ်အထိ ရွေးချယ်ဖွင့်နိုင်တယ်။")
            return
        key = (msg.chat.id, getattr(msg.from_user, "id", None))
        now = time.monotonic()
        if search_cooldowns.get(key, 0) > now:
            await msg.reply_text("⏳ ခဏနေ ပြန်ရှာပါ။")
            return
        if len(search_cooldowns) >= 256:
            search_cooldowns.clear()
        search_cooldowns[key] = now + 5
        progress = await msg.reply_text("🔎 သီချင်းစာရင်း ရှာနေတယ်…")
        async with search_slots:
            choices = await collect_choices(query, lybot.sources if lybot and not stream_only_enabled() else ())
        if not choices:
            await progress.edit_text("⚠️ သီချင်းမတွေ့သေးဘူး။ အဆိုတော်နာမည်ပါ ထည့်ပြီး ပြန်စမ်းပါ။")
            return
        token = uuid.uuid4().hex[:16]
        search_sessions.put(token, msg.chat.id, key[1], choices)
        buttons = []
        lines = ["🎵 ဖွင့်ချင်တဲ့သီချင်း ရွေးပါ", ""]
        for index, choice in enumerate(choices):
            duration = f" · {int(choice.duration)//60}:{int(choice.duration)%60:02d}" if choice.duration else ""
            lines.append(f"{index+1}. {choice.title[:100]}{duration}")
            buttons.append([InlineKeyboardButton(f"{index+1}. {choice.title[:52]}", callback_data=f"songpick:{token}:{index}")])
        await progress.edit_text("\n".join(lines), parse_mode=ParseMode.DISABLED,
                                 reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex(r"^songpick:[a-f0-9]{16}:[0-4]$"))
    async def search_pick(_: Client, cb: CallbackQuery) -> None:
        _, token, index = cb.data.split(":")
        if not cb.message or not cb.from_user:
            await cb.answer()
            return
        choice = search_sessions.select(token, int(index), cb.message.chat.id, cb.from_user.id)
        if choice is None:
            await cb.answer("ရှာထားသူပဲ ရွေးနိုင်တယ်။ သက်တမ်းကုန်ရင် /search ပြန်သုံးပါ။", show_alert=True)
            return
        await cb.answer("▶ ပြင်ဆင်နေတယ်…")
        await cb.message.edit_reply_markup(reply_markup=None)
        proxy = SimpleNamespace(chat=cb.message.chat, from_user=cb.from_user,
                                command=["play", choice.title], text="/play " + choice.title,
                                reply_to_message=None, reply_text=cb.message.edit_text,
                                selected_track=choice_track(choice))
        await play_command(bot, proxy)

    @bot.on_callback_query(filters.regex(r"^audio:(quality|normalize|effect):[a-z]+$"))
    async def retired_audio_menu(_: Client, cb: CallbackQuery) -> None:
        # Old standalone menus must not mutate current playback settings.
        await cb.answer("သီချင်းကဒ်ထဲက Effects / Quality ခလုတ်တွေကို သုံးပါ။",show_alert=True)

    @bot.on_message(filters.command("autoplay") & filters.group)
    async def autoplay_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        action = msg.command[1].lower() if len(msg.command) > 1 else "status"
        if action not in ("on", "off", "status"):
            await msg.reply_text("📻 /autoplay on|off")
            return
        if action != "status":
            player.autoplay_enabled = action == "on"
            if player.autoplay_enabled:
                player.resolver = resolve_for_queue
                player.autoplay_prepare = lambda: start_autoplay(player)
                start_autoplay(player)
            else:
                drop_autoplay(player)
                prepare_next_song(player)
        await msg.reply_text("📻 Autoplay: " + ("ON" if player.autoplay_enabled else "OFF") +
                             "\nQueue ကုန်ရင် ဆင်တူသီချင်း ဆက်ရှာမယ်။ /stop နဲ့ ရပ်မယ်။")

    @bot.on_message(filters.command(["toptracks", "stats"]) & filters.group)
    async def stats_command(_: Client, msg: Message) -> None:
        try:
            action = msg.command[1].lower() if len(msg.command) > 1 else None
            if action not in (None, "all", "group"):
                await msg.reply_text("🏆 /toptracks all|group · /stats all|group")
                return
            global_scope = action == "all" or (action is None and msg.command[0].lower() == "toptracks")
            await msg.reply_text(await stats_text(msg.chat.id, global_scope=global_scope), parse_mode=ParseMode.DISABLED)
        except Exception:
            await msg.reply_text("⚠️ သီချင်းမှတ်တမ်း ယာယီ မရသေးဘူး။")

    @bot.on_message(filters.command("findsong"))
    async def findsong_command(_: Client, msg: Message) -> None:
        media = recognition_media(msg.reply_to_message)
        if not media:
            await msg.reply_text("🎵 Video / Voice / Audio ကို reply လုပ်ပြီး /findsong သုံးပါ။")
            return
        if not 0 < int(getattr(media, "file_size", 0) or 0) <= MAX_AUDIO_BYTES:
            await msg.reply_text("⚠️ 20 MB အောက် Audio / Video ကို သုံးပါ။")
            return
        if recognizer is None:
            await msg.reply_text("⚠️ သီချင်းခွဲခြားတဲ့စနစ် မဖွင့်ထားသေးဘူး။")
            return
        clip_key = "clip:" + str(getattr(media, "file_unique_id", "") or "") if getattr(media, "file_unique_id", None) else None
        cached_audio = delivered_audio.get(clip_key)
        if recognizer.busy and not cached_audio:
            await msg.reply_text("⏳ အခြားသီချင်း ခွဲခြားနေတယ်။ ခဏနေ ပြန်စမ်းပါ။")
            return
        progress = await msg.reply_text("🔎 သီချင်းရှာနေတယ်…")
        audio_path = None
        token = None
        song_key = None
        try:
            started = time.monotonic()
            if cached_audio:
                title, own_file_id, audio_duration = cached_audio
                log.info("Recognition clip cache hit")
            else:
                title, candidate = await recognizer.recognize(msg.reply_to_message, with_audio=True)
                await progress.edit_text("🎵 " + title + "\n⏳ သီချင်းဖိုင် ယူနေတယ်…", parse_mode=ParseMode.DISABLED)
                if candidate is None:
                    candidate = await recognition_source.find(title)
                source_media = getattr(candidate.message, "audio", None) or getattr(candidate.message, "document", None)
                unique = getattr(source_media, "file_unique_id", None)
                song_key = "song:" + unique if unique else None
                known_song = delivered_audio.get(song_key)
                if known_song:
                    title, own_file_id, audio_duration = known_song
                    log.info("Recognition song cache hit")
                else:
                    own_file_id = None
                    audio_path, _, audio_duration = await recognition_source.download(candidate)
            now = time.monotonic()
            for key, result in list(recognition_results.items()):
                if result[3] < now:
                    recognition_results.pop(key, None)
            if len(recognition_results) >= 64:
                recognition_results.pop(next(iter(recognition_results)))
            token = uuid.uuid4().hex[:16]
            recognition_results[token] = (msg.chat.id, getattr(msg.from_user, "id", None), title, now + 600)
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("▶ ဖွင့်မယ်", callback_data="findplay:" + token)],
                                           [lyrics_ui.button(title,audio_duration)]])
            sent_audio = await send_recognized_audio(bot, msg, audio_path, title, audio_duration, markup,
                                                     file_id=own_file_id)
            if not sent_audio or not getattr(sent_audio, "audio", None):
                raise RuntimeError("Audio delivery did not complete")
            delivered_audio.put(clip_key, title, getattr(sent_audio, "audio", None))
            delivered_audio.put(song_key, title, getattr(sent_audio, "audio", None))
            try:
                await progress.delete()
            except Exception:
                pass
            log.info("Song recognition completed in chat %s after %.2fs", msg.chat.id, time.monotonic() - started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delivered_audio.discard(clip_key)
            delivered_audio.discard(song_key)
            log.info("Song recognition unavailable (%s)", type(exc).__name__)
            if token:
                recognition_results.pop(token, None)
            await progress.edit_text("⚠️ သီချင်းဖိုင် မရသေးဘူး။ ခဏနေ /findsong ပြန်စမ်းပါ။")
        finally:
            clean_temp(audio_path)

    @bot.on_callback_query(filters.regex(r"^findplay:[a-f0-9]{16}$"))
    async def recognition_play(_: Client, cb: CallbackQuery) -> None:
        token = cb.data.split(":", 1)[1]
        result = recognition_results.get(token)
        if not result or result[3] < time.monotonic() or not cb.message or cb.message.chat.id != result[0]:
            await cb.answer("သက်တမ်းကုန်ပြီ။ /findsong ပြန်သုံးပါ။", show_alert=True)
            return
        if not cb.from_user or cb.from_user.id != result[1]:
            await cb.answer("သီချင်းရှာထားသူပဲ ဒီခလုတ်ကို သုံးနိုင်တယ်။", show_alert=True)
            return
        if cb.message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            await cb.answer("VC ဖွင့်ဖို့ ဒီ Audio ကို group ထဲပို့ပြီး reply /play သုံးပါ။", show_alert=True)
            return
        recognition_results.pop(token, None)
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=None)
        proxy = SimpleNamespace(chat=cb.message.chat, from_user=cb.from_user,
                                command=["play", result[2]], text="/play " + result[2],
                                reply_to_message=cb.message, reply_text=cb.message.reply_text)
        await play_command(bot, proxy)

    def queue_text(player):
        return queue_display(
            queue_track_title(player.current) if player.current else
            "📻 Radio (idle)" if player.idle_mode == "radio" else None,
            [queue_track_title(song) for song in player.queue], loop=player.loop_current)

    @bot.on_message(filters.command(["queue", "now", "nowplaying"]) & filters.group)
    async def queue_command(_: Client, msg: Message) -> None:
        from pyrogram.enums import ParseMode
        await msg.reply_text(queue_text(players[msg.chat.id]), parse_mode=ParseMode.DISABLED)

    @bot.on_message(filters.command("radio") & filters.group)
    async def radio_command(_: Client, msg: Message) -> None:
        player = players[msg.chat.id]
        action = msg.command[1].lower() if len(msg.command) > 1 else "menu"
        if action in {"on", "off"}:
            if not await require_admin(msg):
                return
            async with player.lock:
                player.radio_enabled = action == "on"
                if player.current is None and player.idle_mode is not None:
                    await start_idle(msg.chat.id, player)
        if action == "status":
            station = radio_catalog.stations.get(player.radio_station)
            await msg.reply_text("📻 " + (station.name if station else "လိုင်းမရွေးရသေးဘူး") +
                "\nRadio: " + ("ON" if player.radio_enabled else "OFF") +
                "\n/radio နဲ့ လိုင်းရွေးနိုင်တယ်။")
            return
        text, keyboard = radio_catalog.menu(msg.chat.id, selected=player.radio_station)
        panel = await msg.reply_text(text, reply_markup=keyboard)
        player.radio_panel_id = panel.id

    @bot.on_callback_query(filters.regex(r"^rad:"))
    async def radio_callback(_: Client, cb: CallbackQuery) -> None:
        try:
            _, raw_chat, action, value = cb.data.split(":")
            chat_id = int(raw_chat)
        except (ValueError, TypeError):
            await cb.answer("Invalid radio control", show_alert=True)
            return
        player = players[chat_id]
        if not cb.message or cb.message.chat.id != chat_id or cb.message.id != player.radio_panel_id:
            await cb.answer("ဒီ menu သက်တမ်းကုန်ပြီ။ /radio ပြန်သုံးပါ။", show_alert=True)
            return
        if action in {"home", "local", "inter"}:
            await cb.answer()
            if action == "local":
                await radio_catalog.load_local()
            text, keyboard = radio_catalog.menu(chat_id, action, int(value), player.radio_station)
            try:
                await cb.message.edit_text(text, reply_markup=keyboard)
            except Exception:
                pass
            return
        if not await is_chat_admin(chat_id, cb.from_user.id if cb.from_user else None):
            await cb.answer("Group Admin ကပဲ လိုင်းပြောင်းနိုင်တယ်။", show_alert=True)
            return
        player.radio_revision += 1
        revision = player.radio_revision
        await cb.answer("📻 ချိတ်ဆက်နေတယ်…")
        if action == "off":
            async with player.lock:
                player.radio_enabled = False
                if player.current is None and player.idle_mode is not None:
                    await start_idle(chat_id, player)
            text, keyboard = radio_catalog.menu(chat_id, selected=player.radio_station)
            await cb.message.edit_text("📻 Radio OFF\n" + text, reply_markup=keyboard)
            return
        if action != "pick":
            return
        try:
            station = await radio_catalog.verify(value)
            async with player.lock:
                if player.radio_revision != revision or player.radio_panel_id != cb.message.id:
                    return
                player.radio_station = station.key
                player.radio_enabled = True
                player.radio_retry_after = 0
                if player.current is not None or (player.loading_task and not player.loading_task.done()):
                    result = "လက်ရှိသီချင်း/Queue ပြီးရင် ဒီလိုင်းကို ဆက်ဖွင့်မယ်။"
                else:
                    if voice_slot_count(players) >= max_groups and player.idle_mode is None:
                        if not await evict_oldest_idle_for_song(chat_id):
                            raise RuntimeError("VC capacity reached")
                    await ensure_assistant_in_group(cb.message.chat)
                    player.keep_idle = True
                    if chat_id not in fresh_voice_chats:
                        await asyncio.wait_for(clear_stale_voice(assistant, chat_id, identity.id),8)
                        fresh_voice_chats.add(chat_id)
                    if not await start_idle(chat_id, player) or player.idle_mode != "radio":
                        raise RuntimeError("Radio connection failed")
                    result = "VC ထဲမှာ တိုက်ရိုက်ဖွင့်နေတယ်။ /play နဲ့ သီချင်းတောင်းနိုင်တယ်။"
            text, keyboard = radio_catalog.menu(chat_id, station.region, selected=station.key)
            await cb.message.edit_text("📻 " + station.name + "\n" + result + "\n\n" + text,
                                       reply_markup=keyboard)
        except Exception as exc:
            log.info("Radio selection failed (%s)", type(exc).__name__)
            text, keyboard = radio_catalog.menu(chat_id, selected=player.radio_station)
            await cb.message.edit_text("⚠️ ဒီလိုင်းကို လက်ရှိချိတ်မရသေးဘူး။ တခြားလိုင်း ရွေးပါ။\n" + text,
                                       reply_markup=keyboard)

    def welcome_menu(chat_id):
        text = ("🎙 မြန်မာ ကြိုဆိုအသံ\nMode: " + welcome.mode(chat_id) +
                " · Voice: " + welcome.voice(chat_id) +
                "\nပုံစံ: " + ("လူတောင်းပွဲ" if welcome.styles.get(chat_id) == "party" else "ပုံမှန်ကြိုဆို") +
                "\n" + ("အသံဝန်ဆောင်မှု အသင့်ဖြစ်ပြီ။" if welcome.ready else "အသံဝန်ဆောင်မှု ယာယီမရသေးဘူး။") +
                "\nON/OFF ပြောင်းခြင်းက နောက်သီချင်း/ရေဒီယိုကစ သက်ရောက်မယ်။")
        def b(label, action):
            return InlineKeyboardButton(label, callback_data=f"wel:{chat_id}:{action}")
        return text, InlineKeyboardMarkup([
            [b("VC ဝင်သူ", "vc"), b("Group လူသစ်", "group")],
            [b("နှစ်မျိုးလုံး", "both"), b("OFF", "off")],
            [b("ပုံမှန်ကြိုဆို", "normal"), b("လူတောင်းပွဲ", "party")],
            [b("👨 Thiha", "male"), b("👩 Nilar", "female")],
            [b("🔊 အသံစမ်းမယ်", "test")]])

    @bot.on_message(filters.command(["လူတောင်းပွဲ", "partyvc", "join"]) & filters.group)
    async def welcome_party_command(_: Client, msg: Message) -> None:
        party = (getattr(msg,"command",None) or ["partyvc"])[0].lower() != "join"
        if not await require_admin(msg):
            return
        chat_id = msg.chat.id
        player = players[chat_id]
        revision = player.radio_revision
        if player.paused:
            await msg.reply_text("▶️ သီချင်းကို Resume လုပ်ပြီးမှ /လူတောင်းပွဲ သုံးပါ။")
            return
        if player.loading_task and not player.loading_task.done():
            await msg.reply_text("⏳ သီချင်းပြင်ဆင်နေတယ်။ ပြီးတာနဲ့ /လူတောင်းပွဲ ပြန်သုံးနိုင်တယ်။")
            return
        progress = await msg.reply_text("🎙 လူတောင်းပွဲ ကြိုဆိုအသံ ပြင်ဆင်နေတယ်…" if party else "🎙 VC ထဲ ဝင်နေတယ်…")
        own_task = asyncio.current_task()
        standalone = player.current is None and player.idle_mode is None
        if standalone:
            player.loading_task = own_task  # /stop can cancel preparation or VC join.
        try:
            if not welcome.ready:
                if welcome_warmup_task and not welcome_warmup_task.done():
                    await asyncio.wait_for(asyncio.shield(welcome_warmup_task),18)
                else:
                    await asyncio.wait_for(welcome.warmup(),18)
            if not welcome.ready:
                raise RuntimeError("Burmese speech unavailable")
            welcome.styles[chat_id] = "party" if party else "normal"
            welcome.modes[chat_id] = "vc"
            if standalone:
                await ensure_assistant_in_group(msg.chat)
            async with player.lock:
                if player.radio_revision != revision:
                    return
                if standalone and player.current is None and player.idle_mode is None:
                    if voice_slot_count(players) > max_groups:
                        await progress.edit_text("⚠️ လက်ရှိ VC အရေအတွက် ပြည့်နေတယ်။ ခဏနေ ပြန်စမ်းပါ။")
                        return
                    if chat_id not in fresh_voice_chats:
                        await asyncio.wait_for(clear_stale_voice(assistant,chat_id,identity.id),8)
                        fresh_voice_chats.add(chat_id)
                    player.keep_idle = True
                    if not await start_idle(chat_id,player,quiet=True):
                        raise RuntimeError("VC not available")
                if party and not welcome.request(chat_id, msg.from_user.id if msg.from_user else 0,
                                       "",force=True,delay=5):
                    await progress.edit_text("🎙 လူတောင်းပွဲ ပုံစံ ဖွင့်ထားပြီ။ ကြိုဆိုအသံ ပြောနေဆဲဆို ခဏစောင့်ပါ။ "
                                             "Mixer ပိတ်ထားခဲ့ရင် နောက်သီချင်း/Radio ကစ အသုံးဝင်မယ်။")
                    return
            text, keyboard = welcome_menu(chat_id)
            await progress.edit_text(("🎙 လူတောင်းပွဲ ဖွင့်ထားပြီ။ VC ဝင်လာသူကို ဒီပုံစံနဲ့ ကြိုဆိုမယ်။\n\n" if party else
                                      "🎙 VC ထဲ ရောက်ပြီ။ နောက်ဝင်လာသူတွေကို ပုံမှန် ကြိုဆိုမယ်။ /stop နဲ့ ထွက်နိုင်တယ်။\n\n") + text,
                                     reply_markup=keyboard)
            player.welcome_panel_id = progress.id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.info("Party welcome start failed (%s)",type(exc).__name__)
            await progress.edit_text("⚠️ Group VC ကို အရင်ဖွင့်ထားပြီး ပြန်စမ်းပါ။ "
                                     "Assistant permission နဲ့ အသံဝန်ဆောင်မှုလည်း ရဖို့လိုတယ်။")
        finally:
            if standalone and player.loading_task is own_task:
                player.loading_task = None

    @bot.on_message(filters.command("welcome") & filters.group)
    async def welcome_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        text, keyboard = welcome_menu(msg.chat.id)
        panel = await msg.reply_text(text, reply_markup=keyboard)
        players[msg.chat.id].welcome_panel_id = panel.id

    @bot.on_callback_query(filters.regex(r"^wel:"))
    async def welcome_callback(_: Client, cb: CallbackQuery) -> None:
        try:
            _, raw_chat, action = cb.data.split(":")
            chat_id = int(raw_chat)
        except (ValueError, TypeError):
            return
        if (not cb.message or cb.message.chat.id != chat_id
                or cb.message.id != players[chat_id].welcome_panel_id):
            await cb.answer("/welcome ပြန်သုံးပါ။", show_alert=True)
            return
        if not await is_chat_admin(chat_id, cb.from_user.id if cb.from_user else None):
            await cb.answer("Group Admin ကပဲ ပြောင်းနိုင်တယ်။", show_alert=True)
            return
        if action in {"off", "vc", "group", "both"}:
            welcome.modes[chat_id] = action
            if action == "off":
                # Keep feeding silence until next stream to avoid blocking FFmpeg.
                pipe = welcome.pipes.get(chat_id)
                if pipe:
                    while not pipe.pending.empty():
                        try:
                            pipe.pending.get_nowait()
                        except Exception:
                            break
        elif action in {"normal", "party"}:
            welcome.styles[chat_id] = action
        elif action in {"male", "female"}:
            welcome.voices[chat_id] = action
        elif action == "test":
            player = players[chat_id]
            if player.paused or (player.current is None and player.idle_mode is None):
                await cb.answer("သီချင်း/ရေဒီယို အရင်ဖွင့်ထားပါ။", show_alert=True)
                return
            if not welcome.request(chat_id, cb.from_user.id, cb.from_user.first_name, force=True):
                await cb.answer("နောက်သီချင်း/Radio ကစ စမ်းနိုင်မယ်။ အသံဝန်ဆောင်မှုလည်း အသင့်ဖြစ်ဖို့လိုတယ်။", show_alert=True)
                return
        await cb.answer("သတ်မှတ်ပြီးပြီ။" if action != "test" else "အသံပြင်ဆင်နေတယ်…")
        text, keyboard = welcome_menu(chat_id)
        try:
            await cb.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            pass

    @calls.on_update(call_filters.call_participant(GroupCallParticipant.Action.JOINED))
    async def welcome_vc_join(_: PyTgCalls, update: UpdatedGroupCallParticipant) -> None:
        if bandwidth_saver is not None:
            try:await bandwidth_saver.arrival(update.chat_id,update.participant.user_id)
            except Exception as exc:log.info('Bandwidth resume deferred (%s)',type(exc).__name__)
        player = players.get(update.chat_id)
        if (not player or player.paused or welcome.mode(update.chat_id) not in {"vc", "both"}
                or update.chat_id not in welcome.pipes or update.participant.user_id == identity.id):
            return
        pipe = welcome.pipes[update.chat_id]
        if time.monotonic() - pipe.created < 8:
            return  # Initial participant snapshot is not a new arrival.
        if welcome.styles.get(update.chat_id) == "party":
            async def party_participants():
                if player.paused or update.chat_id not in welcome.pipes:
                    return set()
                return {p.user_id for p in await calls.get_participants(update.chat_id)}
            welcome.arrive_party(update.chat_id,update.participant.user_id,party_participants)
            return
        try:
            user = await asyncio.wait_for(assistant.get_users(update.participant.user_id),4)
            if not user.is_bot and not user.is_deleted:
                async def still_present():
                    if player.paused or update.chat_id not in welcome.pipes:
                        return False
                    participants = await calls.get_participants(update.chat_id)
                    return any(p.user_id == user.id for p in participants)
                welcome.request(update.chat_id,user.id,user.first_name,present=still_present)
        except Exception:
            pass

    @bot.on_message(filters.new_chat_members & filters.group)
    async def welcome_group_join(_: Client, msg: Message) -> None:
        player = players.get(msg.chat.id)
        if not player or player.paused:
            return
        for user in (msg.new_chat_members or [])[:3]:
            if not user.is_bot and not user.is_deleted:
                welcome.request(msg.chat.id,user.id,user.first_name,'group')

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
        """Direct CDN audio is optional; normal playback uses the verified download path."""
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

    @bot.on_message(filters.command(["clear", "c"]) & filters.group)
    async def clear_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        async with player.lock:
            count = len(player.queue)
            drop_autoplay(player)
            player.queue.clear()
            cancel_queue_metadata(player)
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
                    # Cut the old audio before preparing/replacing its decoder.
                    # ensure_native_sender resumes capture after the new source.
                    try:
                        await asyncio.wait_for(calls.pause(chat_id), timeout=2)
                    except Exception as exc:
                        log.info("Skip pause unavailable (%s)", type(exc).__name__)
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

    @bot.on_message(filters.command(["skip", "s", "next"]) & filters.group)
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
        player.autoplay_enabled = False
        drop_autoplay(player)
        was_loading = cancel_active_loading(player)
        discard_prepared(player)
        if was_loading:
            log.info("Stop interrupted loading chat %s", msg.chat.id)
        async with player.lock:
            drop_autoplay(player)
            player.queue.clear()
            cancel_queue_metadata(player)
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
        welcome.close_chat(msg.chat.id)
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
            if bandwidth_saver is not None:bandwidth_saver.manual(msg.chat.id)
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
                player.current.control_actor=msg.from_user.first_name
                player.current.control_actor_id=msg.from_user.id
                await update_player_panel(msg.chat.id, player)
            except Exception:
                log.exception("Could not %s chat %s", action, msg.chat.id)
                await msg.reply_text("⚠️ VC connection ပြန်စစ်ပါ။")

    @bot.on_callback_query(filters.regex(r"^mud:(export|qmanage|qdel[a-f0-9]{12}|pause|resume|skip|stop|loop|queue|normalize|quality|effects|top|back|topall|topgroup|qhigh|qmedium|qlow|fx[a-z]+):"))
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
        if action == "export":
            await effect_exports.request(cb,player)
            return
        if action == "queue":
            await cb.answer(queue_popup(queue_track_title(player.current),
                                        [queue_track_title(song) for song in player.queue],
                                        loop=player.loop_current), show_alert=True)
            return
        if action in ("topall", "topgroup"):
            try:
                text = await stats_popup(chat_id, global_scope=action == "topall")
            except Exception:
                text = "⚠️ သီချင်းမှတ်တမ်း ယာယီမရသေးဘူး။"
            await cb.answer(text, show_alert=True)
            return
        if action in ("top", "back"):
            player.control_menu = "top" if action == "top" else "main"
            await cb.answer()
            await update_player_panel(chat_id, player)
            return
        if action=='qmanage':
            player.control_menu='queue'
            await cb.answer()
            await update_player_panel(chat_id,player)
            return
        if action.startswith('qdel'):
            token=action[4:]
            track=next((t for t in player.queue if t.queue_token==token),None)
            if track is None:
                await cb.answer('ဒီသီချင်းက စာရင်းမှာ မရှိတော့ဘူး။',show_alert=True);return
            uid=cb.from_user.id if cb.from_user else None
            if uid!=track.requester_id and not await is_chat_admin(chat_id,uid):
                await cb.answer('ကိုယ်တောင်းထားတဲ့သီချင်းကိုပဲ ဖြုတ်နိုင်တယ်။',show_alert=True);return
            # Recheck after the admin RPC; a prior click/skip may have removed it.
            if not any(t is track for t in player.queue):
                await cb.answer('စာရင်းက ပြောင်းသွားပြီ။',show_alert=True);return
            player.queue=deque(t for t in player.queue if t is not track)
            task=player.metadata_tasks.pop(id(track),None)
            if task and not task.done():task.cancel()
            prepare_next_song(player)
            await cb.answer('စာရင်းကနေ ဖယ်ပြီးပြီ။')
            await update_player_panel(chat_id,player)
            return
        if not await is_chat_admin(chat_id, cb.from_user.id if cb.from_user else None):
            await cb.answer("🛡 Group Admin တွေကပဲ Control နှိပ်နိုင်တယ်။", show_alert=True)
            return
        if action in ("normalize", "quality", "effects", "qhigh", "qmedium", "qlow") or (action.startswith("fx") and action[2:] in EFFECTS):
            if action == "normalize":
                player.audio_settings = replace(player.audio_settings, normalize=not player.audio_settings.normalize)
                player.control_menu = "main"
            elif action in ("quality", "effects"):
                player.control_menu = action
            elif action.startswith("q"):
                player.audio_settings = replace(player.audio_settings, quality=action[1:])
                player.control_menu = "quality"
            else:
                player.audio_settings = replace(player.audio_settings, effect=action[2:])
                player.control_menu = "effects"
            await cb.answer("နောက်သီချင်းကစ သက်ရောက်မယ်။" if action not in ("quality", "effects") else "ရွေးချယ်ပါ။")
            await update_player_panel(chat_id, player)
            return
        # Answer promptly; music downloads or reconnection may last >30 seconds.
        # Calling cb.answer() after that makes Telegram return QUERY_ID_INVALID.
        try:
            await cb.answer("⏳ " + action.title())
        except Exception as exc:
            log.info("Player callback acknowledgement expired: %s", type(exc).__name__)
        if action == "stop":
            player.autoplay_enabled = False
            drop_autoplay(player)
            log.info("Stop requested via player button chat %s", chat_id)
            cancel_active_loading(player)
            discard_prepared(player)
            async with player.lock:
                drop_autoplay(player)
                player.queue.clear()
                cancel_queue_metadata(player)
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
            welcome.close_chat(chat_id)
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
                if action in {"pause","resume"} and bandwidth_saver is not None:bandwidth_saver.manual(chat_id)
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
                if action in {"pause","resume"}:
                    player.current.control_actor=cb.from_user.first_name
                    player.current.control_actor_id=cb.from_user.id
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
        if shutdown.is_set():
            return
        log.warning("Telegram VC state changed in chat %s (status=%s)", update.chat_id, update.status)
        welcome.close_chat(update.chat_id)
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

    async def watch_voice_connections() -> None:
        """Repair a detached stream even if no StreamEnded update arrives.

        The registry check is local to the PyTgCalls binding and cheap.
        No background music download is attempted: reuse existing media.
        """
        while True:
            await asyncio.sleep(35)
            for cid in list(welcome.pipes):
                p = players.get(cid)
                if p is None or (p.current is None and p.idle_mode is None
                                 and (p.loading_task is None or p.loading_task.done())):
                    welcome.close_chat(cid)
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
                        if not source or (player.temp_file and not allowed_audio_cdn(source) and not relay_source_id(source)
                                          and not Path(source).is_file()):
                            log.warning("VC recovery file absent in chat %s", chat_id)
                        else:
                            try:
                                if song.video:
                                    media = MediaStream(
                                        source, AudioQuality.MEDIUM, VideoQuality.SD_360p
                                    )
                                else:
                                    media = (audio_stream(source) if player.playing_settings == AudioSettings() else
                                             audio_stream(source, settings=player.playing_settings))
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

    @calls.on_update(call_filters.stream_end())
    async def stream_end(_: PyTgCalls, update: StreamEnded) -> None:
        if shutdown.is_set():
            return
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
                        stream = (audio_stream(source) if player.playing_settings == AudioSettings() else
                                  audio_stream(source, settings=player.playing_settings))
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

    def protected_media_paths():
        paths = [p.temp_file for p in players.values()]
        paths.extend(effect_exports.protected_paths())
        for player in players.values():
            prepared = player.prepared
            if prepared and prepared.task.done() and not prepared.task.cancelled():
                try:
                    paths.append(prepared.task.result()[0])
                except Exception:
                    pass
        return paths

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

    feature_probe_task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    bandwidth_task = None
    bandwidth_saver = None
    probe_task: asyncio.Task | None = None
    bot_probe_task: asyncio.Task | None = None
    candidate_probe_task: asyncio.Task | None = None
    welcome_warmup_task: asyncio.Task | None = None
    radio_warmup_task: asyncio.Task | None = None
    radio_probe_task: asyncio.Task | None = None
    owner_probe_task: asyncio.Task | None = None
    lyrics_probe_task: asyncio.Task | None = None
    effects_probe_task: asyncio.Task | None = None
    cookie_probe_task: asyncio.Task | None = None
    relay_probe_task: asyncio.Task | None = None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)
    try:
        await calls.start()  # Starts the assistant MTProto client.
        identity = await assistant.get_me()
        if identity.is_bot:
            raise RuntimeError("SESSION_STRING is a bot session, not a user session.")
        log.info("Assistant session verified: user account")
        await owner_settings.load()
        await bot.start()
        ready = True
        from youtube_cookie_probe import run_cookie_probe
        cookie_probe_task=asyncio.create_task(run_cookie_probe(players))
        from youtube_relay import startup_probe as probe_youtube_relay
        relay_probe_task = asyncio.create_task(probe_youtube_relay(players))
        welcome_warmup_task = asyncio.create_task(welcome.warmup())
        radio_warmup_task = asyncio.create_task(radio_catalog.load_local())
        try:
            radio_probe_until = float(os.getenv("RADIO_WELCOME_PROBE_UNTIL", "0"))
        except ValueError:
            radio_probe_until = 0
        if 0 < radio_probe_until - time.time() <= 900:
            from radio_welcome_probe import run_radio_welcome_probe
            radio_probe_task = asyncio.create_task(run_radio_welcome_probe(radio_catalog, welcome, resolve_stream_only))
        try:
            party_probe_until = float(os.getenv("WELCOME_PARTY_PROBE_UNTIL", "0"))
        except ValueError:
            party_probe_until = 0
        if 0 < party_probe_until - time.time() <= 900 and radio_probe_task is None:
            from radio_welcome_probe import run_radio_welcome_probe
            radio_probe_task = asyncio.create_task(run_radio_welcome_probe(
                radio_catalog,welcome,resolve_stream_only,party_only=True))
        try:
            owner_probe_until=float(os.getenv("MUSIC_OWNER_PROBE_UNTIL","0"))
        except ValueError:
            owner_probe_until=0
        if 0 < owner_probe_until-time.time() <= 900:
            owner_probe_task=asyncio.create_task(owner_settings.probe_roundtrip())
        try:lyrics_probe_until=float(os.getenv('LYRICS_PROBE_UNTIL','0'))
        except ValueError:lyrics_probe_until=0
        if 0<lyrics_probe_until-time.time()<=900:
            lyrics_probe_task=asyncio.create_task(lyrics_ui.probe())
        try:effects_probe_until=float(os.getenv('MUSIC_EFFECTS_PROBE_UNTIL','0'))
        except ValueError:effects_probe_until=0
        if 0<effects_probe_until-time.time()<=900:
            from effect_probe import run_effect_probe
            effects_probe_task=asyncio.create_task(run_effect_probe(players))
        log.info("Music Bot started; maximum active voice chats: %s", max_groups)
        log.info("Named audio stream-only mode: %s", stream_only_enabled())
        log.info("Parallel music sources enabled: %s", source_names if lybot else [])
        probe_query = os.getenv("MUSIC_STREAM_PROBE_QUERY", "").strip()[:160]
        if probe_query:
            probe_task = asyncio.create_task(run_stream_probe(probe_query))
        try:
            bot_probe_until = float(os.getenv("MUSIC_BOT_PROBE_UNTIL", "0"))
        except ValueError:
            bot_probe_until = 0
        if bot_probe_until and lybot is not None:
            bot_probe_task = asyncio.create_task(run_live_probe(lybot, bot_probe_until))
        try:
            candidate_probe_until = float(os.getenv("MUSIC_CANDIDATE_PROBE_UNTIL", "0"))
        except ValueError:
            candidate_probe_until = 0
        if candidate_probe_until and lybot is not None:
            candidate_probe_task = asyncio.create_task(run_candidate_probes(assistant, lybot, candidate_probe_until))
        try:
            feature_probe_until = float(os.getenv("MUSIC_FEATURE_PROBE_UNTIL", "0"))
        except ValueError:
            feature_probe_until = 0
        if feature_probe_until:
            feature_probe_task = asyncio.create_task(run_feature_probe(feature_probe_until, resolve_stream_only))
        cleanup_task = asyncio.create_task(housekeeping(
            protected_media_paths, maintain_cache))
        watchdog_task = asyncio.create_task(watch_voice_connections())
        from bandwidth_saver import EmptyRoomSaver
        bandwidth_saver=EmptyRoomSaver(calls,players,identity.id,welcome,update_player_panel)
        bandwidth_task=asyncio.create_task(bandwidth_saver.run())
        await shutdown.wait()
    finally:
        ready = False
        await effect_exports.close()
        for task in (welcome_warmup_task, radio_warmup_task, radio_probe_task, owner_probe_task, lyrics_probe_task, effects_probe_task, cookie_probe_task, relay_probe_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (welcome_warmup_task, radio_warmup_task, radio_probe_task, owner_probe_task, lyrics_probe_task, effects_probe_task, cookie_probe_task, relay_probe_task) if t), return_exceptions=True)
        if bandwidth_task:
            bandwidth_task.cancel()
            await asyncio.gather(bandwidth_task,return_exceptions=True)
        await welcome.close()
        if feature_probe_task is not None:
            feature_probe_task.cancel()
            await asyncio.gather(feature_probe_task, return_exceptions=True)
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        if probe_task is not None:
            probe_task.cancel()
            await asyncio.gather(probe_task, return_exceptions=True)
        if candidate_probe_task is not None:
            candidate_probe_task.cancel()
            await asyncio.gather(candidate_probe_task, return_exceptions=True)
        if bot_probe_task is not None:
            bot_probe_task.cancel()
            await asyncio.gather(bot_probe_task, return_exceptions=True)
        if watchdog_task is not None:
            watchdog_task.cancel()
            await asyncio.gather(watchdog_task, return_exceptions=True)
        loading_tasks = []
        for player in players.values():
            if player.loading_task is not None and not player.loading_task.done():
                loading_tasks.append(player.loading_task)
            player.autoplay_enabled = False
            drop_autoplay(player)
            cancel_active_loading(player)
            discard_prepared(player)
            cancel_queue_metadata(player)
            for ui_task in (player.announce_task, player.progress_task, player.queue_refresh_task):
                if ui_task is not None:
                    ui_task.cancel()
        await asyncio.gather(*loading_tasks, return_exceptions=True)
        async def leave_own_voice(chat_id):
            try:
                await asyncio.wait_for(calls.leave_call(chat_id), timeout=8)
            except Exception:
                pass
        await asyncio.gather(*(leave_own_voice(chat_id) for chat_id in players))
        for player in players.values():
            if player.cancel_download is not None:
                player.cancel_download.set()
            discard_prepared(player)
            clean_temp(player.temp_file)
        await private_songs.close()
        await owner_ui.close()
        await owner_settings.close()
        await bot.stop()
        if assistant.is_connected:
            await assistant.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

