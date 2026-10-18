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
                 prefer_stream: bool = False, publish_metadata: bool = True) -> None:
        self.track = track
        self.publish_metadata = publish_metadata
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

    def prime_stream(self, factory, settings):
        """Move the one required audio probe off the Skip critical path."""
        if getattr(self, 'stream_task', None) is not None:
            return
        async def prime():
            try:
                result = await asyncio.shield(self.task)
                if self.discarded:
                    return None
                from playback_prepare import CheckedAudioStream, decoder_slots
                slots = decoder_slots()
                if slots.locked():
                    return None  # Never queue a speculative probe behind other groups.
                stream = CheckedAudioStream(factory(result[0], settings=settings))
                async with slots:
                    await stream.check_stream()
                LOG.info('Next queued audio decoder prepared')
                return result[0], settings, stream
            except Exception as exc:
                LOG.info('Next decoder preparation unavailable (%s)', type(exc).__name__)
                return None
        self.stream_task = asyncio.create_task(prime())

    async def consume_stream(self, source, settings):
        task = getattr(self, 'stream_task', None)
        if task is None:
            return None
        result = await asyncio.shield(task)
        if result is not None and result[0] == source and result[1] == settings:
            return result[2]
        return None

    def _on_done(self, task: asyncio.Task) -> None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            if self.publish_metadata and not self.discarded:
                self.track.metadata_failed = True
                notify = getattr(self, "on_metadata", None)
                if notify is not None:
                    notify()
            return
        if self.publish_metadata and not self.discarded and result:
            self.track.title = result[1]
            self.track.duration_seconds = result[2]
            self.track.metadata_resolved = True
            self.track.metadata_failed = False
            notify = getattr(self, "on_metadata", None)
            if notify is not None:
                notify()
        if self.discarded and not self.consumed and not self.cleaned and result:
            probe = getattr(self, 'stream_task', None)
            if probe is not None and not probe.done():
                # Keep local input alive until its bounded FFprobe finishes.
                if not getattr(self, '_cleanup_waiting', False):
                    self._cleanup_waiting = True
                    probe.add_done_callback(lambda _: self._on_done(task))
                return
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


class AsyncPreparedAudio(PreparedAudio):
    """Same ownership/cancellation contract for the parallel async resolver."""
    def __init__(self, track, resolver, cleanup, *, prefer_stream=False):
        self.track, self.prefer_stream = track, prefer_stream
        self.publish_metadata = True
        self.cancel_signal = threading.Event()
        self.cleanup = cleanup
        self.discarded = self.consumed = self.cleaned = False
        self.task = asyncio.create_task(resolver(track, prefer_stream))
        self.task.add_done_callback(self._on_done)

    def discard(self):
        if self.consumed or self.discarded:return
        super().discard()
        if not self.task.done():self.task.cancel()
