"""Bounded short-term memory for YouTube videos with verified silent native output.

Never disable the whole provider because one video ID is temporarily broken.
Only native decoder/clock startup faults count. Local in-memory state avoids
polling, database activity or persisted user searches.
"""
import logging
import time

from youtube_relay import source_id

LOG = logging.getLogger("telegram_music")
SILENT_ERRORS = frozenset(("AudioFramesStalled", "AudioStartupTimeout", "NoAudioSourceFound"))


class SilentRelayVideoCache:
    def __init__(self, *, clock=None, cooldown=180, maximum=128):
        if cooldown <= 0 or maximum < 1:
            raise ValueError("Invalid silent source recovery limits")
        self.clock = clock or time.monotonic
        self.cooldown = cooldown
        self.maximum = maximum
        self.entries = {}

    def available(self, source):
        video = source_id(source)
        if not video:
            return True
        until = self.entries.get(video, 0.0)
        if until <= self.clock():
            self.entries.pop(video, None)
            return True
        return False

    def failure(self, source, error):
        video = source_id(source)
        if not video or type(error).__name__ not in SILENT_ERRORS:
            return False
        now = self.clock()
        self.entries[video] = now + self.cooldown
        # Cache is always bounded even if a group submits many unique videos.
        if len(self.entries) > self.maximum:
            self.entries.pop(next(iter(self.entries)))
        LOG.info("Silent YouTube media temporarily excluded for %ss; alternative sources eligible",
                 self.cooldown)
        return True

    def success(self, source):
        video = source_id(source)
        if video:
            self.entries.pop(video, None)


silent_relay_videos = SilentRelayVideoCache()
