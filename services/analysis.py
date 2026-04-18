from abc import ABC, abstractmethod
from models.domain import VideoSource
from config.settings import config_service
from utils.logger import logger_service
from typing import List, Dict, Optional

class ScoringStrategy(ABC):
    @abstractmethod
    def calculate_score(self, source: VideoSource, is_current: bool) -> float:
        pass

class AudioActivityStrategy(ScoringStrategy):
    def calculate_score(self, source: VideoSource, is_current: bool) -> float:
        base_score = source.audio_level
        penalty = 0.0 if is_current else config_service.switch_penalty
        return base_score - penalty

class AnalysisService:
    def __init__(self, strategy: ScoringStrategy):
        self.strategy = strategy
        self.logger = logger_service.get_logger()

    def set_strategy(self, strategy: ScoringStrategy):
        self.strategy = strategy

    def evaluate_sources(self, sources: List[VideoSource], current_source_id: Optional[int]) -> Dict[int, float]:
        scores = {}
        for src in sources:
            is_current = (src.id == current_source_id)
            scores[src.id] = self.strategy.calculate_score(src, is_current)
        return scores
