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
from song_cache import get_cached_song, save_cached_song
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import AudioQuality, GroupCallConfig, MediaStream, StreamEnded, VideoQuality

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
        if reason == "NoActiveGroupCall":
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
    if path:
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

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 0,
    }
    try:
        if cancel_signal is not None and cancel_signal.is_set():
            return []
        with YoutubeDL(opts) as searcher:
            result = searcher.extract_info("scsearch10:" + query, download=False)
            entries = list((result or {}).get("entries") or [])
        if cancel_signal is not None and cancel_signal.is_set():
            return []
    except Exception as exc:
        log.warning("SoundCloud search unavailable: %s", type(exc).__name__)
        return []

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
    return urls

def source_candidates(
    query: str, video: bool, cancel_signal: threading.Event | None = None
):
    """Lazy source choices: YouTube first, SoundCloud only if that fails.

    YouTube search metadata can fail on some cloud hosts; this does not try
    to bypass sign-in checks or protected media. Spotify URLs are unsupported.
    """
    if is_youtube_url(query):
        yield ("YouTube", query)
        return
    if video:
        yield ("YouTube", "ytsearch1:" + query)
        return
    # Search official artist recordings before third-party uploads.
    youtube = ("YouTube", "ytsearch1:" + query + " official")
    priority = os.getenv("MUSIC_SOURCE_PRIORITY", "youtube").lower()
    if priority != "soundcloud":
        yield youtube
    if cancel_signal is None or not cancel_signal.is_set():
        for url in soundcloud_results(query, max_tracks=3, cancel_signal=cancel_signal):
            yield ("SoundCloud", url)
    if priority == "soundcloud":
        yield youtube


def download_song(query: str, video: bool, cancel_signal: threading.Event | None = None) -> tuple[str, str, float | None]:
    """Download one playable source, trying SoundCloud if YouTube is blocked."""
    from yt_dlp import YoutubeDL

    if os.getenv("MUSIC_CACHE_ENABLED", "1") != "0":
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Download cancelled by stop")
        cached = get_cached_song(query, video)
        if cached is not None:
            return cached

    def abort_if_stopped(_: dict | None = None) -> None:
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Download cancelled by stop")

    failures: list[str] = []
    for provider, target in source_candidates(query, video, cancel_signal=cancel_signal):
        abort_if_stopped()
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
            log.info("Resolved music through %s (track title may differ from query)", provider)
            label = f"{title} · {provider}"
            seconds = float(duration) if duration is not None else None
            if os.getenv("MUSIC_CACHE_ENABLED", "1") != "0":
                save_cached_song(query, video, str(media_path), label, seconds)
            return str(media_path), label, seconds
        except Exception as exc:
            shutil.rmtree(directory, ignore_errors=True)
            abort_if_stopped()
            failures.append(provider)
            log.warning(
                "Audio source %s candidate unavailable (%s): %s",
                provider, type(exc).__name__, str(exc)[:230],
            )
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
    max_groups = max(1, int(os.getenv("MAX_ACTIVE_GROUPS", "2")))
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
    calls = PyTgCalls(assistant, workers=2)

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

    async def update_player_panel(chat_id: int, player: Player) -> None:
        if player.panel_id is None or player.current is None:
            return
        try:
            await bot.edit_message_reply_markup(
                chat_id, player.panel_id,
                reply_markup=player_controls(
                    chat_id, paused=player.paused, loop=player.loop_current
                ),
            )
        except Exception:
            pass

    async def clear_player_panel(chat_id: int, player: Player) -> None:
        old_id = player.panel_id
        player.panel_id = None
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

    async def advance(chat_id: int, progress: Message | None = None, *, repeat: bool = False) -> None:
        """Caller holds this chat's lock. Moves to next track or leaves VC."""
        player = players[chat_id]
        task = asyncio.current_task()
        player.loading_task = task
        cancel_signal = threading.Event()
        player.cancel_download = cancel_signal
        try:
            if repeat and player.loop_current and player.current is not None:
                player.queue.appendleft(player.current)
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
                        local_file, title, duration_seconds = await asyncio.wait_for(
                            asyncio.to_thread(download_song, source, song.video, cancel_signal),
                            timeout=110,
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

                    if song.video:
                        # Conservative video quality for the initial Render Free test.
                        stream = MediaStream(
                            source,
                            AudioQuality.MEDIUM,
                            VideoQuality.SD_360p,
                        )
                    else:
                        # Never try to create a video camera for audio-only media.
                        stream = MediaStream(
                            source,
                            AudioQuality.HIGH,
                            video_flags=MediaStream.Flags.IGNORE,
                        )
                    if cancel_signal.is_set():
                        raise asyncio.CancelledError()
                    log.info("VC play requested chat %s", chat_id)
                    await play_with_reconnect(calls, chat_id, stream)
                    log.info("VC play success chat %s", chat_id)
                    player.current = song
                    player.recovery_attempts = 0
                    player.temp_file = local_file
                    player.started_at = time.monotonic()
                    notice = ("🎬 " if song.video else "🎵 ") + "Now playing: " + song.title
                    previous_panel = player.panel_id
                    try:
                        markup = player_controls(chat_id, paused=False, loop=player.loop_current)
                        if progress:
                            await progress.edit_text(notice, reply_markup=markup)
                            new_panel = progress
                            progress = None
                        else:
                            new_panel = await bot.send_message(chat_id, notice, reply_markup=markup)
                        player.panel_id = new_panel.id
                        if previous_panel is not None and previous_panel != new_panel.id:
                            try:
                                await bot.delete_messages(chat_id, previous_panel)
                            except Exception:
                                pass
                    except Exception:
                        log.warning("Could not announce now playing in chat %s", chat_id)
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
                    if type(exc).__name__ == "NoActiveGroupCall":
                        error_message = ("🎙 Group Voice Chat ကို Admin က ဖွင့်ပေးပါ။ "
                                         "Assistant က /play နဲ့ အလိုအလျောက် ဝင်ပေးမယ်။")
                    elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or (
                        song.telegram_file and "download" in str(exc).lower()
                    ):
                        error_message = ("⚠️ Telegram Media Download မအောင်မြင်ဘူး။ "
                                         "HTTPS Bot API နဲ့ MTProto နှစ်ခုစလုံးမရနိုင်တာပါ။ "
                                         "နောက်မှ ပြန်စမ်းပေးပါ။")
                    elif song.youtube:
                        error_message = ("⚠️ Music source တွေက ဒီသီချင်းကို Download "
                                         "မပေးနိုင်သေးဘူး။ YouTube Login Check "
                                         "သို့မဟုတ် SoundCloud Access Error ဖြစ်နိုင်တယ်။ "
                                         "အခြားသီချင်း၊ သို့မဟုတ် Audio File ကို "
                                         "Reply လုပ်ပြီး /play စမ်းပါ။")
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

            try:
                await calls.leave_call(chat_id)
            except Exception as exc:
                log.info("VC leave after empty queue in %s: %s", chat_id, type(exc).__name__)
            log.info("Playlist finished; empty queue; VC left chat %s", chat_id)
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
            "Video အတွက် /vplay <name> (သို့) MP4 reply /vplay သုံးပါ။\n"
            "Private Group: Music Bot ကို Admin + Invite Users ပေးထားပါ။\n"
            "/play /vplay /queue /now /skip /pause /resume /stop\n"
            "/shuffle /remove 1 /clear /loop — Admin Controls\n"
            "/ping — bot online ဖြစ်မဖြစ် စစ်မယ်။\n\n"
            "Direct HTTPS audio link တွေအတွက် server-side host allowlist လိုတယ်။",
            reply_markup=add_button,
        )

    @bot.on_message(filters.command("ping"))
    async def ping_command(_: Client, msg: Message) -> None:
        await msg.reply_text("🏓 Pong — Music Bot online!")

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
                    track = Track("YouTube video" if video_mode else "YouTube audio",
                                  query, video=video_mode, youtube=True)
                elif allowed_url(query, allowed_hosts):
                    track = Track("Direct video" if video_mode else "Direct audio",
                                  query, video=video_mode)
                else:
                    await msg.reply_text("⚠️ YouTube Link သို့မဟုတ် /play <song name> ထည့်ပေးပါ။")
                    return
            else:
                track = Track(query, query, video=video_mode, youtube=True)
        else:
            await msg.reply_text(
                "🎬 /vplay <video name> သို့မဟုတ် MP4 ကို reply /vplay"
                if video_mode else "🎵 /play Faded လို့ သီချင်းနာမည် ထည့်ပါ။"
            )
            return

        player = players[msg.chat.id]
        progress = await msg.reply_text("⏳ Assistant အလိုအလျောက်ဝင်ပြီး VC ချိတ်နေတယ်...")
        try:
            await ensure_assistant_in_group(msg.chat)
        except RuntimeError as exc:
            await progress.edit_text("⚠️ " + str(exc))
            return
        except Exception:
            log.exception("Assistant auto-join failed in chat %s", msg.chat.id)
            await progress.edit_text("⚠️ Assistant Auto Join မရသေးဘူး။ Admin / Invite Settings စစ်ပါ။")
            return
        await progress.edit_text("🔎 သီချင်းရှာ/Download လုပ်ပြီး VC ချိတ်နေတယ်...")
        # Do not make another /play wait for a long download to release
        # the chat lock; safely append while an existing loader owns it.
        if player.loading_task is not None and not player.loading_task.done():
            if len(player.queue) >= MAX_QUEUE:
                await progress.edit_text("⚠️ Queue ပြည့်နေပြီ (10 tracks)။")
                return
            player.queue.append(track)
            await progress.edit_text("✅ Queue ထဲထည့်ပြီးပြီ — " + track.title)
            return
        async with player.lock:
            if len(player.queue) >= MAX_QUEUE:
                await progress.edit_text("⚠️ Queue ပြည့်နေပြီ (10 tracks)။")
                return
            if video_mode and sum(
                p.current is not None and p.current.video for p in players.values()
            ) >= 1:
                await progress.edit_text("⚠️ Video Streaming တစ်ခုသာ စမ်းသပ်ခွင့်ပြုထားတယ်။")
                return
            if player.current is None and sum(
                p.current is not None for p in players.values()
            ) >= max_groups:
                await progress.edit_text(
                    "⚠️ လောလောဆယ် VC တွေပြည့်နေပြီ။ နောက်မှထပ်စမ်းပါ။"
                )
                return

            player.queue.append(track)
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
                player.current.title if player.current else None,
                [song.title for song in player.queue],
                loop=player.loop_current,
            )
        await msg.reply_text(message)

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

    @bot.on_message(filters.command(["skip", "next"]) & filters.group)
    async def skip_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        async with player.lock:
            if not player.current:
                await msg.reply_text("သီချင်းဖွင့်ထားတာ မရှိသေးဘူး။")
                return
            try:
                await asyncio.create_task(advance(msg.chat.id))
            except asyncio.CancelledError:
                return
            await msg.reply_text("⏭ Skipped.")

    @bot.on_message(filters.command(["stop", "end"]) & filters.group)
    async def stop_command(_: Client, msg: Message) -> None:
        if not await require_admin(msg):
            return
        player = players[msg.chat.id]
        # Preempt any ongoing search or VC join before acquiring the lock.
        was_loading = cancel_active_loading(player)
        if was_loading:
            log.info("Stop interrupted loading chat %s", msg.chat.id)
        async with player.lock:
            player.queue.clear()
            player.current = None
            player.started_at = 0.0
            player.paused = False
            player.loop_current = False
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
                    player.paused = True
                else:
                    await calls.resume(msg.chat.id)
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
            cancel_active_loading(player)
            async with player.lock:
                player.queue.clear()
                player.current = None
                player.paused = False
                player.loop_current = False
                player.started_at = 0
                await clear_player_panel(chat_id, player)
                clean_temp(player.temp_file)
                player.temp_file = None
                try:
                    await asyncio.wait_for(calls.leave_call(chat_id), timeout=12)
                except Exception as exc:
                    log.info("Button stop leave status %s: %s", chat_id, type(exc).__name__)
            return
        async with player.lock:
            if player.current is None:
                return
            try:
                if action == "pause":
                    await calls.pause(chat_id)
                    player.paused = True
                elif action == "resume":
                    await calls.resume(chat_id)
                    player.paused = False
                elif action == "loop":
                    player.loop_current = not player.loop_current
                elif action == "skip":
                    await asyncio.create_task(advance(chat_id))
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

    @calls.on_update(call_filters.stream_end())
    async def stream_end(_: PyTgCalls, update: StreamEnded) -> None:
        player = players.get(update.chat_id)
        if player is None or update.stream_type != StreamEnded.Type.AUDIO:
            return
        async with player.lock:
            song = player.current
            if song is None:
                log.info("Ignored VC end event in chat %s (no active song)", update.chat_id)
                return
            elapsed = time.monotonic() - player.started_at
            if elapsed < 2:
                log.info("Ignored delayed VC audio end in chat %s (%.1fs)", update.chat_id, elapsed)
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

    try:
        await calls.start()  # Starts the assistant MTProto client.
        identity = await assistant.get_me()
        if identity.is_bot:
            raise RuntimeError("SESSION_STRING is a bot session, not a user session.")
        log.info("Assistant session verified: user account")
        await bot.start()
        ready = True
        log.info("Music Bot started; maximum active voice chats: %s", max_groups)
        await asyncio.Event().wait()
    finally:
        ready = False
        for player in players.values():
            clean_temp(player.temp_file)
        await bot.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
