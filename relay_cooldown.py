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


class RelayPlaybackHealth:
    """Short health gate for repeated *native* YouTube audio stalls.

    Unlike upstream HTTP failures, stalled RTC output is observed after a
    provider has already won the URL race. Production evidence (Oct 24–28)
    showed zero verified Vapters audio among 32 initial wins, with attempts
    often separated by many minutes. Count failures across an hour and skip
    that lane for 30 minutes after two native stalls. This preserves a
    periodic recovery trial instead of permanently disabling the source.
    This does not affect SoundCloud, NubCoders or Telegram bot media.
    """

    FAILURE_NAMES = frozenset({"AudioFramesStalled", "AudioStartupTimeout", "NoAudioSourceFound"})

    def __init__(self, *, clock=time.monotonic, threshold=2, window=3600, cooldown=1800):
        self.clock = clock
        self.threshold = threshold
        self.window = window
        self.cooldown = cooldown
        self.failures = deque(maxlen=threshold)
        self.until = 0.0

    def available(self):
        now = self.clock()
        if self.until and now >= self.until:
            self.until = 0.0
            self.failures.clear()
            LOG.info("YouTube native audio recovery window ended")
        return now >= self.until

    def failure(self, error):
        if type(error).__name__ not in self.FAILURE_NAMES:
            return False
        now = self.clock()
        if now < self.until:
            return False
        while self.failures and self.failures[0] <= now - self.window:
            self.failures.popleft()
        self.failures.append(now)
        LOG.info("YouTube native audio health: %s/%s failed within %ss",
                 len(self.failures), self.threshold, self.window)
        if len(self.failures) >= self.threshold:
            self.until = now + self.cooldown
            LOG.warning("YouTube native audio stalled repeatedly; using independent sources for %ss",
                        self.cooldown)
        return True

    def success(self):
        if self.clock() >= self.until:
            self.failures.clear()
            self.until = 0.0


relay_playback_health = RelayPlaybackHealth()
