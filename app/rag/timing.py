"""Aggregate wall-clock throughput across concurrent embedding workers."""

import time
from collections import deque


class IndexTiming:
    def __init__(self, started=None, window=60):
        self.started = time.monotonic() if started is None else started
        self.window = window
        self.encoding_started = None
        self.count = 0
        self.samples = deque()

    def begin_encoding(self, now=None):
        if self.encoding_started is None:
            now = time.monotonic() if now is None else now
            self.encoding_started = now
            self.samples.append((now, 0))

    def record(self, count, now=None):
        now = time.monotonic() if now is None else now
        self.begin_encoding(now)
        self.count += count
        self.samples.append((now, self.count))
        self._trim(now)

    def _trim(self, now):
        while len(self.samples) > 1 and self.samples[1][0] <= now - self.window:
            self.samples.popleft()

    def rate(self, fallback, now=None):
        now = time.monotonic() if now is None else now
        if self.encoding_started is None or now - self.encoding_started < 10:
            return fallback
        self._trim(now)
        start = max(self.encoding_started, now - self.window)
        stamp, baseline = self.samples[0]
        if len(self.samples) > 1 and stamp < start:
            next_stamp, next_count = self.samples[1]
            if next_stamp > stamp:
                baseline += (next_count - baseline) * (start - stamp) / (next_stamp - stamp)
        return max(0, self.count - baseline) / max(0.001, now - start)

    def elapsed(self, now=None):
        now = time.monotonic() if now is None else now
        return max(0, now - self.started)
