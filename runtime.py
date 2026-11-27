"""Runtime primitives for smooth rate limiting, cooldowns, watermarks and batch writes."""
import random
import threading
import time


def compute_next_due(started_at, interval, now=None, min_cooldown=5.0):
    """Schedule on a fixed rate from task start while keeping a minimum cooldown."""
    current = time.monotonic() if now is None else now
    floor = min(float(min_cooldown), max(1.0, float(interval) * 0.25))
    return max(current + floor, float(started_at) + float(interval))


class HostRateLimiter:
    """Reservation-based smooth pacer per upstream host with AIMD 429/403 backoff."""

    def __init__(self, gap_range=(0.12, 0.28), max_multiplier=8.0, recovery_step=0.1):
        low, high = float(gap_range[0]), float(gap_range[1])
        self.gap_range = (min(low, high), max(low, high))
        self.max_multiplier = max(1.0, float(max_multiplier))
        self.recovery_step = max(0.01, float(recovery_step))
        self._lock = threading.Lock()
        self._hosts = {}

    def _state(self, host):
        state = self._hosts.get(host)
        if state is None:
            state = {"next_ts": 0.0, "mult": 1.0, "hold_until": 0.0}
            self._hosts[host] = state
        return state

    def multiplier(self, host):
        with self._lock:
            return self._state(host)["mult"]

    def acquire(self, host, stop_event=None):
        if not host or (stop_event is not None and stop_event.is_set()):
            return
        with self._lock:
            now = time.monotonic()
            state = self._state(host)
            gap = random.uniform(*self.gap_range) * state["mult"]
            scheduled = max(now, state["next_ts"])
            state["next_ts"] = scheduled + gap
            wait = scheduled - now
        if wait <= 0:
            return
        if stop_event is None:
            time.sleep(wait)
        else:
            stop_event.wait(wait)

    def on_success(self, host):
        if not host:
            return
        with self._lock:
            state = self._state(host)
            if state["mult"] > 1.0 and time.monotonic() >= state["hold_until"]:
                state["mult"] = max(1.0, state["mult"] - self.recovery_step)

    def on_rate_limited(self, host, cooldown=15.0):
        if not host:
            return 1.0
        with self._lock:
            now = time.monotonic()
            state = self._state(host)
            state["mult"] = min(self.max_multiplier, max(2.0, state["mult"] * 2.0))
            pause = max(1.0, float(cooldown)) * random.uniform(0.85, 1.2)
            state["hold_until"] = max(state["hold_until"], now + pause)
            state["next_ts"] = max(state["next_ts"], now + min(pause, 8.0))
            return state["mult"]


class SymbolCooldown:
    """Exponentially back off repeatedly failing watchlist items to free worker slots."""

    def __init__(self, threshold=3, max_backoff=600.0):
        self.threshold = max(1, int(threshold))
        self.max_backoff = max(10.0, float(max_backoff))
        self._lock = threading.Lock()
        self._failures = {}

    def record_success(self, key):
        with self._lock:
            self._failures.pop(key, None)

    def record_failure(self, key, base_interval):
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count < self.threshold:
                return None
            exp = min(count - self.threshold + 1, 6)
            return min(self.max_backoff, max(float(base_interval), 15.0) * (2 ** exp))

    def forget_missing(self, active_keys):
        with self._lock:
            for key in list(self._failures.keys()):
                if key not in active_keys:
                    del self._failures[key]


def _bar_signature(bar):
    return (bar["open"], bar["high"], bar["low"], bar["close"], bar.get("volume"))


class CandleWatermark:
    """Two-phase candle fingerprint cache: filter before DB write, commit only after commit."""

    def __init__(self, max_bars_per_series=1600):
        self.max_bars_per_series = max(64, int(max_bars_per_series))
        self._lock = threading.Lock()
        self._series = {}
        self._latest = {}

    def latest_stamp(self, key, interval="1m"):
        with self._lock:
            return self._latest.get((key, interval))

    def seed_latest_stamp(self, key, interval, stamp):
        if stamp is None:
            return
        with self._lock:
            series_key = (key, interval)
            prev = self._latest.get(series_key)
            if prev is None or stamp > prev:
                self._latest[series_key] = int(stamp)

    def forget(self, key):
        with self._lock:
            for interval in ("1m", "1d"):
                self._series.pop((key, interval), None)
                self._latest.pop((key, interval), None)

    def filter_candles(self, key, candles):
        if not candles:
            return []
        deduped = {}
        for bar in candles:
            deduped[(bar["interval"], int(bar["stamp"]))] = bar
        changed = []
        with self._lock:
            for (interval, stamp), bar in deduped.items():
                known = self._series.get((key, interval))
                if known is None or known.get(stamp) != _bar_signature(bar):
                    changed.append(bar)
        return changed

    def commit_candles(self, key, candles):
        if not candles:
            return
        with self._lock:
            for bar in candles:
                series_key = (key, bar["interval"])
                stamp = int(bar["stamp"])
                known = self._series.setdefault(series_key, {})
                known[stamp] = _bar_signature(bar)
                prev = self._latest.get(series_key)
                if prev is None or stamp > prev:
                    self._latest[series_key] = stamp
                if len(known) > self.max_bars_per_series:
                    overflow = len(known) - self.max_bars_per_series
                    for oldest in sorted(known)[:overflow]:
                        known.pop(oldest, None)


class MicroBatchRecorder:
    """Single-thread background DB batch writer flushed by item count or sub-second timer."""

    def __init__(self, write_batch_fn, max_items=8, flush_interval=0.5, on_error=None):
        self.write_batch_fn = write_batch_fn
        self.max_items = max(1, int(max_items))
        self.flush_interval = max(0.05, float(flush_interval))
        self.on_error = on_error
        self._cond = threading.Condition()
        self._buffer = []
        self._first_ts = None
        self._writing = False
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="db-writer", daemon=True)
        self._thread.start()

    def submit(self, entry):
        with self._cond:
            while len(self._buffer) >= self.max_items * 8 and not self._closed:
                self._cond.wait(timeout=0.2)
            if self._closed:
                raise RuntimeError("MicroBatchRecorder is closed")
            if not self._buffer:
                self._first_ts = time.monotonic()
            self._buffer.append(entry)
            if len(self._buffer) >= self.max_items:
                self._cond.notify()

    def flush(self):
        with self._cond:
            self._cond.notify_all()
            while (self._buffer or self._writing) and not self._closed:
                self._cond.wait(timeout=0.05)

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._thread.join()

    def _loop(self):
        while True:
            with self._cond:
                while not self._buffer and not self._closed:
                    self._cond.wait()
                if not self._buffer and self._closed:
                    return
                if len(self._buffer) < self.max_items and not self._closed:
                    wait = self.flush_interval - (time.monotonic() - (self._first_ts or time.monotonic()))
                    if wait > 0:
                        self._cond.wait(timeout=wait)
                        if not self._buffer:
                            if self._closed:
                                return
                            continue
                batch = self._buffer[:self.max_items]
                del self._buffer[:self.max_items]
                self._first_ts = time.monotonic() if self._buffer else None
                self._writing = True
                self._cond.notify_all()

            try:
                self.write_batch_fn(batch)
            except Exception as exc:
                if self.on_error is not None:
                    self.on_error(exc)
            finally:
                with self._cond:
                    self._writing = False
                    self._cond.notify_all()
