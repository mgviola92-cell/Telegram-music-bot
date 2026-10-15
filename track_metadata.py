"""Small metadata-only search cache; never stores audio bytes or signed URLs."""
import threading
import time

_LOCK=threading.Lock()
_ITEMS={}

def remember_metadata(query,title,duration):
    with _LOCK:
        key=query.strip().casefold()
        _ITEMS.pop(key,None)
        if len(_ITEMS)>=64:_ITEMS.pop(next(iter(_ITEMS)))
        _ITEMS[key]=(str(title),duration,time.monotonic()+600)

def lookup_metadata(query):
    with _LOCK:
        row=_ITEMS.get(query.strip().casefold())
        return row[:2] if row and row[2]>time.monotonic() else None
