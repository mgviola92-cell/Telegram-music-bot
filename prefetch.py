"""Bounded one-track-ahead prefetch for Telegram music VC queues.

Each group prepares at most one next audio track. The downloader runs off the
event loop. Discarded results are cleaned even if their worker finishes late.
Never persist media beyond the existing temporary file/cache policy.
"""
import asyncio
import logging
import threading
from typing import Callable, Any

LOG = logging.getLogger("telegram_music")


class PreparedAudio:
    def __init__(self, track: Any, downloader: Callable, cleanup: Callable, *,
                 prefer_stream: bool = False) -> None:
        self.track = track
        self.prefer_stream = prefer_stream
        self.cancel_signal = threading.Event()
        self.cleanup = cleanup
        self.discarded = False
        self.consumed = False
        self.cleaned = False
        args = (track.source, track.video, self.cancel_signal)
        if prefer_stream:
            args += (True,)
        self.task = asyncio.create_task(asyncio.to_thread(downloader, *args))
        self.task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task) -> None:
        try:
            result = task.result()
        except (asyncio.CancelledError, Exception):
            return
        if self.discarded and not self.consumed and not self.cleaned and result:
            self.cleaned = True
            self.cleanup(result[0])

    def discard(self) -> None:
        if self.consumed or self.discarded:
            return
        self.discarded = True
        self.cancel_signal.set()
        if self.task.done():
            self._on_done(self.task)

    async def consume(self) -> tuple[str, str, float | None]:
        if self.discarded:
            raise InterruptedError("Prefetch has been discarded")
        try:
            # Shield so a /stop does not kill the downloader task before it
            # can handle its cooperative cancellation event.
            result = await asyncio.shield(self.task)
        except BaseException:
            self.discard()
            raise
        if self.discarded:
            raise InterruptedError("Prefetch discarded during download")
        self.consumed = True
        return result
