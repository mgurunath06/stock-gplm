"""Adaptive concurrency and request pacing for the cloud candle filler."""
from __future__ import annotations

import threading
import time


class AdaptiveWorkers:
    """Ramp worker count up on success and down on transport/provider errors."""

    def __init__(self, minimum=1, maximum=4, start=2, ramp_successes=25, ramp_failures=2):
        if not minimum <= start <= maximum:
            raise ValueError("worker bounds are invalid")
        self.minimum, self.maximum, self.target = minimum, maximum, start
        self.ramp_successes, self.ramp_failures = ramp_successes, ramp_failures
        self.successes = self.failures = 0
        self.extra_delay = 0.0
        self.next_call = 0.0
        self.lock = threading.Lock()

    def wait(self, base_pause: float) -> None:
        """Globally stagger calls; concurrency changes apply without stopping workers."""
        with self.lock:
            interval = max(float(base_pause), self.extra_delay) / self.target
            at = max(time.monotonic(), self.next_call)
            self.next_call = at + interval
        delay = at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def success(self) -> None:
        with self.lock:
            self.successes += 1
            self.failures = 0
            if self.successes >= self.ramp_successes:
                if self.target < self.maximum:
                    self.target += 1
                self.successes = 0
                self.extra_delay = max(0.0, self.extra_delay * 0.75)

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            self.successes = 0
            if self.failures >= self.ramp_failures:
                self.target = max(self.minimum, self.target - 1)
                self.extra_delay = min(10.0, max(1.0, self.extra_delay * 2))
                self.failures = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {"target": self.target, "minimum": self.minimum, "maximum": self.maximum,
                    "success_streak": self.successes, "failure_streak": self.failures,
                    "extra_delay": round(self.extra_delay, 3)}
