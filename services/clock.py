import threading
import time


class RecordingClock:
    def __init__(self):
        self._lock = threading.Lock()
        self._start_monotonic_ns = None

    def start(self):
        with self._lock:
            self._start_monotonic_ns = time.monotonic_ns()

    def stop(self):
        with self._lock:
            self._start_monotonic_ns = None

    def reset(self):
        self.stop()

    @property
    def is_started(self) -> bool:
        with self._lock:
            return self._start_monotonic_ns is not None

    def now_ns(self) -> int:
        with self._lock:
            start_ns = self._start_monotonic_ns
        if start_ns is None:
            return 0
        return max(0, time.monotonic_ns() - start_ns)
