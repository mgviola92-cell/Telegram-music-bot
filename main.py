"""Isolated Telegram Voice Chat Music Bot — MVP.

Telegram audio replies and allowlisted direct HTTPS audio streams only.
Secrets must be configured as Render environment variables.
"""
import asyncio
import logging
import os
import tempfile
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import Message
from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import StreamEnded

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram_music")

MAX_QUEUE = 10
MAX_AUDIO_BYTES = 25 * 1024 * 1024


@dataclass
class Track:
    title: str
    source: str
    telegram_file: bool = False


@dataclass
class Player:
    queue: deque = field(default_factory=deque)
    current: Track | None = None
    temp_file: str | None = None
    started_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def clean_temp(path: str | None) -> None:
    if path:
        try:
            Path(path).unlink(missing_ok=True)
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

    async def advance(chat_id: int) -> None:
        """Caller holds this chat's lock. Moves to next track or leaves VC."""
        player = players[chat_id]
        clean_temp(player.temp_file)
        player.temp_file = None
        player.current = None

        while player.queue:
            song: Track = player.queue.popleft()
            local_file = None
            try:
                source = song.source
                if song.telegram_file:
                    target = str(
                        Path(tempfile.gettempdir())
                        / ("music_" + uuid.uuid4().hex + ".mp3")
                    )
                    local_file = target
                    downloaded = await bot.download_media(source, file_name=target)
                    if not downloaded:
                        raise RuntimeError("Telegram audio download failed")
                    local_file = downloaded
                    source = local_file

                await calls.play(chat_id, source)
                player.current = song
                player.temp_file = local_file
                player.started_at = time.monotonic()
                try:
                    await bot.send_message(chat_id, "🎵 Now playing: " + song.title)
                except Exception:
                    log.warning("Could not send now-playing notice to %s", chat_id)
                return
            except Exception:
                clean_temp(local_file)
                log.exception("Could not play track in chat %s", chat_id)
                await bot.send_message(
                    chat_id,
                    "⚠️ ဒီသီချင်းဖွင့်မရဘူး။ VC ဖွင့်ထားပြီး "
                    "Assistant Account ကို Group ထဲထည့်ထားလား စစ်ပါ။",
                )

        try:
            await calls.leave_call(chat_id)
        except Exception:
            pass  # It may already have left or never joined the call.

    @bot.on_message(filters.command(["start", "help"]))
    async def help_command(_: Client, msg: Message) -> None:
        await msg.reply_text(
            "🎧 Telegram VC Music Bot\n\n"
            "Group VC ဖွင့်ပြီး MP3/Audio message ကို reply လုပ်ကာ /play ရိုက်ပါ။\n"
            "/play /queue /skip /pause /resume /stop\n"
            "/ping — bot online ဖြစ်မဖြစ် စစ်မယ်။\n\n"
            "Direct HTTPS audio link တွေအတွက် server-side host allowlist လိုတယ်။"
        )

    @bot.on_message(filters.command("ping"))
    async def ping_command(_: Client, msg: Message) -> None:
        await msg.reply_text("🏓 Pong — Music Bot online!")

    @bot.on_message(filters.command("play") & filters.group)
    async def play_command(_: Client, msg: Message) -> None:
        replied = msg.reply_to_message
        media = replied.audio if replied and replied.audio else None
        if not media and replied and replied.document:
            doc = replied.document
            if (doc.mime_type or "").startswith("audio/"):
                media = doc

        if media:
            if media.file_size and media.file_size > MAX_AUDIO_BYTES:
                await msg.reply_text("⚠️ 25 MB ထက်ကြီးတဲ့ audio ကို လောလောဆယ် မထောက်ပံ့သေးဘူး။")
                return
            title = getattr(media, "title", None) or media.file_name or "Telegram audio"
            track = Track(str(title)[:100], media.file_id, telegram_file=True)
        elif len(msg.command) >= 2:
            url = msg.text.split(maxsplit=1)[1].strip()
            if not allowed_url(url, allowed_hosts):
                await msg.reply_text(
                    "⚠️ ဒီ HTTPS audio host ကို ခွင့်မပြုထားသေးဘူး။ "
                    "Telegram audio ကို reply လုပ်ပြီး /play ရိုက်နိုင်တယ်။"
                )
                return
            track = Track("Direct audio stream", url)
        else:
            await msg.reply_text("🎵 Audio message ကို reply လုပ်ပြီး /play ရိုက်ပါ။")
            return

        player = players[msg.chat.id]
        async with player.lock:
            if len(player.queue) >= MAX_QUEUE:
                await msg.reply_text("⚠️ Queue ပြည့်နေပြီ (10 tracks)။")
                return
            if player.current is None and sum(
                p.current is not None for p in players.values()
            ) >= max_groups:
                await msg.reply_text(
                    "⚠️ လောလောဆယ် VC တွေပြည့်နေပြီ။ နောက်မှထပ်စမ်းပါ။"
                )
                return

            player.queue.append(track)
            if player.current is None:
                await advance(msg.chat.id)
            else:
                await msg.reply_text(
                    "✅ Queue ထဲထည့်ပြီးပြီ — " + track.title
                )

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
        async with player.lock:
            player.queue.clear()
            player.current = None
            clean_temp(player.temp_file)
            player.temp_file = None
            try:
                await calls.leave_call(msg.chat.id)
            except Exception:
                pass
        await msg.reply_text("⏹ Stopped and left VC.")

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
