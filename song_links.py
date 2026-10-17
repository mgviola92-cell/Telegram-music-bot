"""Metadata-only public song links. Never expose signed CDN URLs or bot download links."""
import threading
from collections import OrderedDict
from urllib.parse import urlsplit
_LOCK=threading.RLock();_LINKS=OrderedDict()
HOSTS={'soundcloud.com','www.soundcloud.com','m.soundcloud.com','youtube.com','www.youtube.com','youtu.be','music.youtube.com','song.link','odesli.co','open.spotify.com','music.apple.com'}
def safe_link(url):
    try:
        p=urlsplit(url)
        return isinstance(url,str) and len(url)<1500 and p.scheme=='https' and p.hostname in HOSTS and not p.username and not p.password and p.port in (None,443) and p.path not in ('','/') and not p.fragment
    except (ValueError,TypeError,AttributeError):return False

def remember_link(title,url):
    if not safe_link(url):return
    with _LOCK:
        from music_ui import clean_track_title
        key=clean_track_title(title).casefold();_LINKS[key]=url;_LINKS.move_to_end(key)
        while len(_LINKS)>96:_LINKS.popitem(last=False)
def link_for(title):
    from music_ui import clean_track_title
    with _LOCK:return _LINKS.get(clean_track_title(title).casefold())
def message_link(message):
    for entity in list(getattr(message,'entities',None) or [])+list(getattr(message,'caption_entities',None) or []):
        url=getattr(entity,'url',None)
        if safe_link(url):return url
    for row in getattr(getattr(message,'reply_markup',None),'inline_keyboard',[]) or []:
        for button in row:
            if safe_link(getattr(button,'url',None)):return button.url
    import re
    for url in re.findall(r'https://[^\s<>]+',str(getattr(message,'caption',None) or getattr(message,'text',None) or '')):
        if safe_link(url):return url
