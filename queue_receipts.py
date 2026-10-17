"""Update existing /play queue acknowledgements when real metadata arrives."""
import asyncio
from music_ui import queue_track_title
from pyrogram.enums import ParseMode
from card_layout import render
from player_visuals import format_clock


def receipt_text(track, *, playing=False, position=1):
    if getattr(track, 'metadata_failed', False) and not getattr(track, 'metadata_resolved', False):
        return '⚠️ Queue ထဲထည့်ထားတယ်။ သီချင်း Source ရှာမရသေးဘူး။'
    title = queue_track_title(track)
    return render('playing' if playing else 'queued',html_mode=True,requester_id=getattr(track,'requester_id',None),title=title,
                  duration=format_clock(getattr(track,'duration_seconds',None)),
                  requester=getattr(track,'requester','—'),position=position,status='Started Streaming')


async def sync_receipts(player):
    from queue_guard import collapse_resolved,duplicate_text
    removed=collapse_resolved(player)
    for track,previous in removed:
        message=getattr(track,'queue_receipt',None)
        if message:
            try:await message.edit_text(duplicate_text(player,previous),parse_mode=ParseMode.HTML)
            except Exception:pass
        task=player.metadata_tasks.pop(id(track),None)
        if task and not task.done():task.cancel()
    if removed:
        from main import prepare_next_song
        prepare_next_song(player)
    entries = list(player.queue)
    if player.current is not None:
        entries.append(player.current)
    for position,track in enumerate(entries,1):
        message = getattr(track, 'queue_receipt', None)
        if message is None:
            continue
        text = receipt_text(track, playing=track is player.current,position=position)
        if text == getattr(track, 'queue_receipt_text', None):
            continue
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
            track.queue_receipt_text = text
        except Exception:
            pass


def schedule_receipts(player):
    player.queue_revision += 1
    entries = list(player.queue) + ([player.current] if player.current else [])
    if not any(getattr(t, 'queue_receipt', None) is not None for t in entries):
        return
    if player.queue_refresh_task is not None and not player.queue_refresh_task.done():
        return
    async def refresh():
        while True:
            await asyncio.sleep(.2)
            revision = player.queue_revision
            await sync_receipts(player)
            if revision == player.queue_revision:
                return
    player.queue_refresh_task = asyncio.create_task(refresh())


async def acknowledge_queued(player, track, message):
    track.queue_receipt = message
    position=next((i for i,t in enumerate(player.queue,1) if t is track),1)
    text = receipt_text(track,position=position)
    await message.edit_text(text, parse_mode=ParseMode.HTML)
    track.queue_receipt_text = text
    schedule_receipts(player)
