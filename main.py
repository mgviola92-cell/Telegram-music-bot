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
from pyrogram.types import Message
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


@dataclass
class Player:
    queue: deque = field(default_factory=deque)
    current: Track | None = None
    temp_file: str | None = None
    started_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loading_task: asyncio.Task | None = None
    cancel_download: threading.Event | None = None


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

def download_song(query: str, video: bool, cancel_signal: threading.Event | None = None) -> tuple[str, str]:
    """Download a playable candidate; SoundCloud handles audio-search fallback."""
    from yt_dlp import YoutubeDL

    if is_youtube_url(query):
        candidates = [("YouTube", query)]
    elif video:
        candidates = [("YouTube", "ytsearch1:" + query)]
    else:
        candidates = [
            ("SoundCloud", url) for url in soundcloud_results(query, cancel_signal=cancel_signal)
        ]
        candidates.append(("YouTube", "ytsearch1:" + query))

    def abort_if_stopped(_: dict | None = None) -> None:
        if cancel_signal is not None and cancel_signal.is_set():
            raise InterruptedError("Download cancelled by stop")

    failures: list[str] = []
    for provider, target in candidates:
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
            return str(media_path), f"{title} · {provider}"
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

    async def advance(chat_id: int, progress: Message | None = None) -> None:
        """Caller holds this chat's lock. Moves to next track or leaves VC."""
        player = players[chat_id]
        task = asyncio.current_task()
        player.loading_task = task
        cancel_signal = threading.Event()
        player.cancel_download = cancel_signal
        try:
            clean_temp(player.temp_file)
            player.temp_file = None
            player.current = None

            while player.queue:
                song: Track = player.queue.popleft()
                local_file = None
                try:
                    source = song.source
                    if song.youtube:
                        local_file, title = await asyncio.wait_for(
                            asyncio.to_thread(download_song, source, song.video, cancel_signal),
                            timeout=110,
                        )
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
                    await asyncio.wait_for(
                        calls.play(chat_id, stream, GroupCallConfig(auto_start=False)),
                        timeout=45,
                    )
                    log.info("VC play success chat %s", chat_id)
                    player.current = song
                    player.temp_file = local_file
                    player.started_at = time.monotonic()
                    notice = ("🎬 " if song.video else "🎵 ") + "Now playing: " + song.title
                    try:
                        if progress:
                            await progress.edit_text(notice)
                            progress = None
                        else:
                            await bot.send_message(chat_id, notice)
                    except Exception:
                        log.warning("Could not announce now playing in chat %s", chat_id)
                    return
                except asyncio.CancelledError:
                    cancel_signal.set()
                    clean_temp(local_file)
                    log.info("Playback loading canceled chat %s", chat_id)
                    raise
                except Exception as exc:
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
            except Exception:
                pass  # It may already have left or never joined the call.
        finally:
            if player.loading_task is task:
                player.loading_task = None
                player.cancel_download = None

    @bot.on_message(filters.command(["start", "help"]))
    async def help_command(_: Client, msg: Message) -> None:
        await msg.reply_text(
            "🎧 Telegram VC Music Bot\n\n"
            "Group VC ဖွင့်ပြီး /play Faded လို့ သီချင်းနာမည် ရိုက်နိုင်တယ်။\n"
            "Audio File ကို Reply လုပ်ပြီး /play လည်းရတယ်။\n"
            "Video အတွက် /vplay <name> (သို့) MP4 reply /vplay သုံးပါ။\n"
            "Private Group: Music Bot ကို Admin + Invite Users ပေးထားပါ။\n"
            "/play /vplay /queue /skip /pause /resume /stop\n"
            "/ping — bot online ဖြစ်မဖြစ် စစ်မယ်။\n\n"
            "Direct HTTPS audio link တွေအတွက် server-side host allowlist လိုတယ်။"
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
            track = Track(str(title)[:100], media.file_id, telegram_file=True, video=video_mode)
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
                    await advance(msg.chat.id, progress)
                except asyncio.CancelledError:
                    try:
                        await progress.edit_text("⏹ /stop လုပ်ထားလို့ ရပ်လိုက်ပြီ။")
                    except Exception:
                        pass
                    return
            else:
                await progress.edit_text("✅ Queue ထဲထည့်ပြီးပြီ — " + track.title)

    @bot.on_message(filters.command("queue") & filters.group)
    async def queue_command(_: Client, msg: Message) -> None:
        player = players[msg.chat.id]
        current = player.current.title if player.current else "မရှိသေးဘူး"
        next_up = [f"{i}. {song.title}" for i, song in enumerate(player.queue, 1)]
        text = "🎶 Playing: " + current + "\n\n📜 Next:\n"
        await msg.reply_text(text + ("\n".join(next_up) or "Queue အလွတ်"))

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_command(_: Client, msg: Message) -> None:
        player = players[msg.chat.id]
        async with player.lock:
            if not player.current:
                await msg.reply_text("သီချင်းဖွင့်ထားတာ မရှိသေးဘူး။")
                return
            await advance(msg.chat.id)
            await msg.reply_text("⏭ Skipped.")

    @bot.on_message(filters.command("stop") & filters.group)
    async def stop_command(_: Client, msg: Message) -> None:
        player = players[msg.chat.id]
        # Preempt any ongoing search or VC join before acquiring the lock.
        was_loading = cancel_active_loading(player)
        if was_loading:
            log.info("Stop interrupted loading chat %s", msg.chat.id)
        async with player.lock:
            player.queue.clear()
            player.current = None
            player.started_at = 0.0
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
        player = players[msg.chat.id]
        async with player.lock:
            if player.current is None:
                await msg.reply_text("သီချင်းဖွင့်ထားတာ မရှိသေးဘူး။")
                return
            action = msg.command[0].lower()
            try:
                if action == "pause":
                    await calls.pause(msg.chat.id)
                else:
                    await calls.resume(msg.chat.id)
                await msg.reply_text("⏸ Paused." if action == "pause" else "▶️ Resumed.")
            except Exception:
                log.exception("Could not %s chat %s", action, msg.chat.id)
                await msg.reply_text("⚠️ VC connection ပြန်စစ်ပါ။")

    @calls.on_update(call_filters.stream_end())
    async def stream_end(_: PyTgCalls, update: StreamEnded) -> None:
        player = players.get(update.chat_id)
        if player is None:
            return
        async with player.lock:
            # The video track can produce separate audio/video end events.
            # Move queue only for the audio stream to avoid double-advance.
            if update.stream_type != StreamEnded.Type.AUDIO:
                return
            # Ignore delayed end events caused by replacing a stream.
            if player.current and time.monotonic() - player.started_at >= 2:
                await advance(update.chat_id)

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
