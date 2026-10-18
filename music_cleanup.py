"""Clean only old bot-owned temporary paths; active files always win."""
import asyncio
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path

_OWNED_DIR = re.compile(r'^mudyar_(media|recognize|recognition_upload|export)_[A-Za-z0-9_-]+$')
_OWNED_FILE = re.compile(r'^music_[a-f0-9]{32}\.(mp3|mp4)$')


def sweep(active_paths, *, root=None, now=None, max_age=21600):
    root = Path(root or tempfile.gettempdir())
    now = time.time() if now is None else now
    protected = {Path(p).resolve() for p in active_paths if p and not str(p).startswith(('https://','http://'))}
    removed = 0
    for path in root.iterdir():
        try:
            if path.is_symlink() or not (_OWNED_DIR.fullmatch(path.name) or _OWNED_FILE.fullmatch(path.name)):
                continue
            resolved = path.resolve()
            if any(p == resolved or resolved in p.parents for p in protected):
                continue
            modified = max([path.stat().st_mtime] + [p.stat().st_mtime for p in path.rglob('*') if p.is_file()]) if path.is_dir() else path.stat().st_mtime
            if now - modified < max_age:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


async def housekeeping(active_paths, prune_cache):
    while True:
        try:
            await asyncio.to_thread(prune_cache)
            count = await asyncio.to_thread(sweep, active_paths())
            if count:
                logging.getLogger('telegram_music').info('Removed %s stale music temporary paths', count)
        except Exception as exc:
            logging.getLogger('telegram_music').info('Music cleanup unavailable (%s)', type(exc).__name__)
        await asyncio.sleep(300)
