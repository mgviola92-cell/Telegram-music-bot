"""Lightweight provider failure cooldown policy (experimental, not wired into playback)."""
import time

class ProviderHealth:
    def __init__(self, *, threshold=2, cooldown_seconds=45, clock=None):
        if threshold<1 or cooldown_seconds<=0: raise ValueError('Invalid limits')
        self.threshold=threshold
        self.cooldown_seconds=cooldown_seconds
        self.clock=clock or time.monotonic
        self.failures={}
        self.blocked_until={}

    def available(self, provider):
        now=self.clock()
        if self.blocked_until.get(provider,0)>now:
            return False
        if provider in self.blocked_until:
            self.blocked_until.pop(provider,None)
            self.failures.pop(provider,None)
        return True

    def success(self, provider):
        self.failures.pop(provider,None)
        self.blocked_until.pop(provider,None)

    def failure(self, provider):
        if not self.available(provider):
            return
        count=self.failures.get(provider,0)+1
        self.failures[provider]=count
        if count>=self.threshold:
            self.blocked_until[provider]=self.clock()+self.cooldown_seconds
