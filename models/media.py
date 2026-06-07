from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class TimedVideoFrame:
    source_id: int
    timestamp_ns: int
    frame: np.ndarray
    seq: int


@dataclass
class TimedAudioChunk:
    source_id: int
    start_ns: int
    end_ns: int
    samples: np.ndarray
    sample_rate: int
    rms: float


@dataclass
class TimelineSegment:
    start_ns: int
    end_ns: Optional[int]
    source_id: int
    reason: str
    score: float
