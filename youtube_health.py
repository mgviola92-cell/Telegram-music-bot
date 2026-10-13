"""Thread-safe YouTube verification cooldown.

When a hosting IP repeatedly receives sign-in verification challenges, trying
more videos usually only delays permitted fallback sources. Never try to
evade verification or obtain secret authentication material.
"""
from collections import deque
from threading import Lock
from time import monotonic


def is_verification_challenge(error: BaseException | str) -> bool:
    message = str(error).casefold()
    return any(token in message for token in (
        "sign in to confirm you're not a bot",
        "sign in to confirm you’re not a bot",
        "unusual traffic",
        "verify you are human",
    ))


class YouTubeGuard:
    def __init__(self, max_failures: int = 2, window_seconds: float = 600,
                 cooldown_seconds: float = 900) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._lock = Lock()
        self._failures: deque[float] = deque()
        self._blocked_until: float = 0.0

    def seconds_remaining(self, now: float | None = None) -> float:
        now = monotonic() if now is None else now
        with self._lock:
            return max(0.0, self._blocked_until - now)

    def record_error(self, error: BaseException | str,
                     now: float | None = None) -> bool:
        """Return True if an upstream verification challenge was seen."""
        if not is_verification_challenge(error):
            return False
        now = monotonic() if now is None else now
        with self._lock:
            while self._failures and now - self._failures[0] > self.window_seconds:
                self._failures.popleft()
            self._failures.append(now)
            if len(self._failures) >= self.max_failures:
                self._blocked_until = max(self._blocked_until,
                                          now + self.cooldown_seconds)
                self._failures.clear()
        return True

    def record_success(self) -> None:
        with self._lock:
            self._blocked_until = 0.0
            self._failures.clear()


youtube_guard = YouTubeGuard()
