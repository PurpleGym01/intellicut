import threading
from typing import List, Optional

from models.media import TimelineSegment


class Timeline:
    def __init__(self):
        self._lock = threading.Lock()
        self._segments: List[TimelineSegment] = []

    def reset(self):
        with self._lock:
            self._segments.clear()

    def open(self, source_id: Optional[int], start_ns: int = 0, reason: str = "recording_start", score: float = 0.0):
        if source_id is None:
            return
        with self._lock:
            self._segments = [TimelineSegment(int(start_ns), None, int(source_id), reason, float(score))]

    def switch_to(self, source_id: Optional[int], timestamp_ns: int, reason: str, score: float = 0.0):
        if source_id is None:
            return None
        timestamp_ns = max(0, int(timestamp_ns))
        with self._lock:
            if self._segments and self._segments[-1].source_id == int(source_id):
                return self._segments[-1]
            if self._segments and self._segments[-1].end_ns is None:
                self._segments[-1].end_ns = max(self._segments[-1].start_ns, timestamp_ns)
            segment = TimelineSegment(timestamp_ns, None, int(source_id), reason, float(score))
            self._segments.append(segment)
            return segment

    def close(self, end_ns: int):
        with self._lock:
            if self._segments and self._segments[-1].end_ns is None:
                self._segments[-1].end_ns = max(self._segments[-1].start_ns, int(end_ns))

    def source_at(self, timestamp_ns: int) -> Optional[int]:
        timestamp_ns = int(timestamp_ns)
        with self._lock:
            if not self._segments:
                return None
            for segment in self._segments:
                if segment.start_ns <= timestamp_ns and (segment.end_ns is None or timestamp_ns < segment.end_ns):
                    return segment.source_id
            if timestamp_ns < self._segments[0].start_ns:
                return self._segments[0].source_id
            return self._segments[-1].source_id

    def segments(self) -> List[TimelineSegment]:
        with self._lock:
            return [
                TimelineSegment(
                    segment.start_ns,
                    segment.end_ns,
                    segment.source_id,
                    segment.reason,
                    segment.score,
                )
                for segment in self._segments
            ]
