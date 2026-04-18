from enum import Enum
from dataclasses import dataclass
from typing import Optional

class SourceType(Enum):
    CAMERA = "camera"
    FILE = "file"
    STREAM = "stream"

class SourceStatus(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ERROR = "error"

class ScenePreset(Enum):
    SPEAKER = "speaker"       # Один спикер
    SPLIT = "split"           # Разделенный экран
    WIDE = "wide"             # Общий план

@dataclass
class VideoSource:
    id: int
    name: str
    type: SourceType
    status: SourceStatus = SourceStatus.INACTIVE
    audio_level: float = 0.0  # Нормализованный уровень 0.0 - 1.0

@dataclass
class SwitchEvent:
    timestamp: float
    from_source_id: Optional[int]
    to_source_id: int
    reason: str
    preset: Optional[ScenePreset] = None