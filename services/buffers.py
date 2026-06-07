import threading
from typing import Any, List, Optional


class TimeRingBuffer:
    def __init__(self, window_seconds: float):
        self.window_ns = max(0, int(float(window_seconds) * 1_000_000_000))
        self._items: List[Any] = []
        self._lock = threading.Lock()

    def _item_start_ns(self, item) -> int:
        if hasattr(item, "start_ns"):
            return int(item.start_ns)
        return int(getattr(item, "timestamp_ns", 0))

    def _item_end_ns(self, item) -> int:
        if hasattr(item, "end_ns"):
            return int(item.end_ns)
        return int(getattr(item, "timestamp_ns", 0))

    def _item_point_ns(self, item) -> int:
        if hasattr(item, "timestamp_ns"):
            return int(item.timestamp_ns)
        return self._item_start_ns(item)

    def _prune_locked(self, newest_ns: int):
        if not self.window_ns:
            return
        cutoff_ns = newest_ns - self.window_ns
        while self._items and self._item_end_ns(self._items[0]) < cutoff_ns:
            self._items.pop(0)

    def clear(self):
        with self._lock:
            self._items.clear()

    def push(self, item):
        with self._lock:
            self._items.append(item)
            self._prune_locked(self._item_end_ns(item))

    def latest(self):
        with self._lock:
            return self._items[-1] if self._items else None

    def closest(self, timestamp_ns: int):
        with self._lock:
            if not self._items:
                return None
            return min(
                self._items,
                key=lambda item: abs(self._item_point_ns(item) - int(timestamp_ns)),
            )

    def closest_before_or_at(self, timestamp_ns: int):
        with self._lock:
            target_ns = int(timestamp_ns)
            candidate = None
            for item in self._items:
                if self._item_point_ns(item) <= target_ns:
                    candidate = item
                else:
                    break
            return candidate

    def between(self, start_ns: int, end_ns: Optional[int]):
        with self._lock:
            start_ns = int(start_ns)
            end_ns = int(end_ns) if end_ns is not None else None
            result = []
            for item in self._items:
                item_start = self._item_start_ns(item)
                item_end = self._item_end_ns(item)
                if end_ns is not None and item_start >= end_ns:
                    break
                if hasattr(item, "timestamp_ns"):
                    if end_ns is None:
                        if item_start >= start_ns:
                            result.append(item)
                    elif start_ns <= item_start < end_ns:
                        result.append(item)
                    continue
                if end_ns is None:
                    if item_end >= start_ns:
                        result.append(item)
                elif item_end > start_ns and item_start < end_ns:
                    result.append(item)
            return result
