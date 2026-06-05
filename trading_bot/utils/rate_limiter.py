"""
utils/rate_limiter.py — Binance request-weight rate limiting + retry decorator.

Binance Futures allows ~1200 request weight per minute. We use a simple
token-bucket: weight is refilled continuously and a call blocks until enough
weight is available. We deliberately slow down once we cross 80% (960) so we
never trip the hard limit.
"""

import time
import functools
import threading

# Binance Futures weight budget.
MAX_WEIGHT_PER_MIN = 1200
SOFT_LIMIT = int(MAX_WEIGHT_PER_MIN * 0.80)  # 960 -> start slowing down


class TokenBucket:
    """Continuously refilling weight bucket, thread-safe."""

    def __init__(self, capacity=MAX_WEIGHT_PER_MIN, refill_per_sec=MAX_WEIGHT_PER_MIN / 60.0):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = capacity
        self.last = time.time()
        self.lock = threading.Lock()

    def _refill(self):
        now = time.time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last = now

    def consume(self, weight=1):
        """Block until `weight` tokens are available, then consume them."""
        while True:
            with self.lock:
                self._refill()
                # Soft throttle: if we are below the soft headroom, add a tiny
                # pause so bursts spread out instead of slamming the limit.
                if self.tokens >= weight and (self.capacity - self.tokens) < SOFT_LIMIT:
                    self.tokens -= weight
                    return
                if self.tokens >= weight:
                    # Above soft limit usage -> consume but breathe a moment.
                    self.tokens -= weight
                    need_breath = True
                else:
                    need_breath = False
                    deficit = weight - self.tokens
                    wait = deficit / self.refill_per_sec
            if need_breath:
                time.sleep(0.2)
                return
            time.sleep(min(wait, 2.0))


# Shared global bucket for the whole process.
bucket = TokenBucket()


def rate_limited(weight=1):
    """Decorator that consumes `weight` tokens before running the call."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            bucket.consume(weight)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def with_retry(max_retries=5, base_delay=5):
    """
    Retry decorator with exponential backoff: 5s, 10s, 20s, 40s, 80s.

    Catches Binance API exceptions, timeouts and connection errors. A -1003
    (too many requests) error forces a 60s cooldown before the next attempt.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001 - we re-raise after retries
                    attempt += 1
                    msg = str(e)
                    is_rate = "-1003" in msg or "Too many requests" in msg
                    if attempt > max_retries:
                        raise
                    if is_rate:
                        time.sleep(60)
                    else:
                        time.sleep(base_delay * (2 ** (attempt - 1)))

        return wrapper

    return decorator
