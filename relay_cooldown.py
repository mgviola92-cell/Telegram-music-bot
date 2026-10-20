"""Short, in-memory backoff for confirmed relay input failures only.

No health requests, timers, disk writes or new media downloads. Generic native
sender failures are deliberately insufficient evidence to disable a provider.
"""
from collections import deque
import logging
import re
import time

LOG = logging.getLogger('telegram_music')


class RelayCoolingDown(RuntimeError):
    pass


def upstream_failure(error):
    if type(error).__name__ in {
        'CancelledError', 'AudioFramesStalled', 'AudioTransportUnavailable',
        'NoActiveGroupCall', 'NoAudioSourceFound', 'RelayCoolingDown',
    }:
        return False
    text = str(error).lower()
    input_error = type(error).__name__ == 'FFmpegError' or any(term in text for term in (
        'ffmpeg', 'error opening input', 'cold-lemming-3841.alexainc.deno.net'))
    if not input_error:
        return False
    # HTTP 403/404 and missing formats may be video-specific, not an outage.
    if re.search(r'(?:http (?:error |status )?|server returned )5\d\d\b', text):
        return True
    return any(term in text for term in (
        'connection timed out', 'connection refused', 'connection reset by peer',
        'network is unreachable', 'read timed out', 'i/o timeout',
    ))


class RelayCooldown:
    def __init__(self, *, clock=time.monotonic, threshold=3, window=60, cooldown=30):
        self.clock = clock
        self.threshold, self.window, self.cooldown = threshold, window, cooldown
        self.failures = deque(maxlen=threshold)
        self.until = 0.0

    def available(self):
        now = self.clock()
        if self.until and now >= self.until:
            self.until = 0.0
            self.failures.clear()
            LOG.info('YouTube relay cooldown finished; source eligible again')
        return now >= self.until

    def require_available(self):
        if not self.available():
            raise RelayCoolingDown('Relay temporarily cooling down; use other music sources')

    def failure(self, error):
        if not upstream_failure(error):
            return False
        now = self.clock()
        if now < self.until:
            return False  # Late failures cannot extend an already-open window.
        while self.failures and self.failures[0] <= now - self.window:
            self.failures.popleft()
        self.failures.append(now)
        if len(self.failures) >= self.threshold:
            self.until = now + self.cooldown
            LOG.warning('YouTube relay input failed %s times; using other sources for %ss',
                        self.threshold, self.cooldown)
        return True

    def success(self):
        # A late success during the cooldown must not undo a later outage.
        if self.clock() < self.until:
            return
        self.until = 0.0
        self.failures.clear()


relay_cooldown = RelayCooldown()
