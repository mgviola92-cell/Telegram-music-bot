"""Atomic event-loop queue admission and stable removal tokens."""
import re
import unicodedata
from html import escape
from card_layout import mention
from music_ui import queue_track_title

def norm(text):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',str(text)).casefold()).strip()

def identities(track):
    keys={('source',bool(track.video),norm(track.source))}
    candidate=getattr(track,'selected',None)
    message=getattr(candidate,'message',None)
    audio=getattr(message,'audio',None) or getattr(message,'document',None)
    unique=getattr(audio,'file_unique_id',None)
    if unique:keys.add(('telegram',bool(track.video),unique))
    if getattr(track,'metadata_resolved',False):keys.add(('title',bool(track.video),norm(queue_track_title(track))))
    return keys

def duplicate(player,track):
    # A playing/loading track is not a pending queue entry. Allow one replay.
    entries=list(player.queue)
    wanted=identities(track)
    for other in entries:
        if other is not None and other is not track and wanted & identities(other):return other
    return None

def admit(player,track):
    existing=duplicate(player,track)
    if existing:return existing
    player.queue.append(track)
    return None

def duplicate_text(player,track):
    position=next((str(i) for i,t in enumerate(player.queue,1) if t is track),'လက်ရှိပြင်ဆင် / ဖွင့်နေဆဲ')
    return ('➜ ဒီသီချင်းက စာရင်းမှာ ရှိပြီးသားပါ။\n\n▸ သီချင်း : '+escape(queue_track_title(track))+
            '\n▸ တောင်းထားသူ : '+mention(getattr(track,'requester','—'),getattr(track,'requester_id',None))+
            '\n▸ အစဉ် : '+position)


def collapse_resolved(player):
    # Compare pending entries with each other, never against current playback.
    known=[]
    removed=[];kept=[]
    for track in player.queue:
        previous=next((t for t in known if identities(t)&identities(track)),None)
        if previous:removed.append((track,previous))
        else:known.append(track);kept.append(track)
    if removed:
        from collections import deque
        player.queue=deque(kept)
    return removed
