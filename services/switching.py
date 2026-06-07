from models.domain import SwitchEvent, ScenePreset
from config.settings import config_service
from utils.logger import logger_service
from typing import Optional
from services.clock import RecordingClock
import time


class SwitchingEngine:
    def __init__(self, clock: Optional[RecordingClock] = None):
        self.clock = clock
        self.current_source_id: Optional[int] = None
        self.last_switch_time: float = 0
        self.pending_source_id: Optional[int] = None
        self.pending_since: float = 0
        self.mode_auto = True
        self.logger = logger_service.get_logger()

    def _now_sec(self) -> float:
        if self.clock is not None and self.clock.is_started:
            return self.clock.now_ns() / 1_000_000_000
        return time.monotonic()

    def decide(self, scores: dict, sources) -> Optional[SwitchEvent]:
        if not self.mode_auto:
            return None
        if not scores:
            return None

        now = self._now_sec()

        # Cooldown: жесткая пауза после последнего переключения.
        if self.current_source_id is not None and now - self.last_switch_time < config_service.cooldown:
            return None

        best_source_id = max(scores, key=scores.get)
        best_score = scores[best_source_id]
        current_score = scores.get(self.current_source_id, 0.0)

        # Если лучший источник слишком тихий - не переключаемся.
        if best_score < config_service.audio_threshold:
            return None

        # Первая инициализация текущего источника.
        if self.current_source_id is None:
            prev_source_id = None
            self.current_source_id = best_source_id
            self.last_switch_time = now
            self.pending_source_id = None
            self.pending_since = 0
            self.logger.info(f"Switch decision: {prev_source_id} -> {best_source_id} (Score: {best_score})")
            return SwitchEvent(
                timestamp=now,
                from_source_id=prev_source_id,
                to_source_id=best_source_id,
                reason="audio_score"
            )

        # Переключаемся только если новый источник заметно громче текущего.
        if best_source_id == self.current_source_id:
            self.pending_source_id = None
            self.pending_since = 0
            return None
        if best_score <= current_score + config_service.switch_hysteresis:
            self.pending_source_id = None
            self.pending_since = 0
            return None

        # min_frame_duration: кандидат должен быть устойчиво лучшим заданное время.
        if self.pending_source_id != best_source_id:
            self.pending_source_id = best_source_id
            self.pending_since = now
            return None
        if now - self.pending_since < config_service.min_frame_duration:
            return None

        prev_source_id = self.current_source_id
        self.logger.info(
            f"Switch decision: {prev_source_id} -> {best_source_id} "
            f"(Score: {best_score:.3f}, Current: {current_score:.3f})"
        )
        self.current_source_id = best_source_id
        self.last_switch_time = now
        self.pending_source_id = None
        self.pending_since = 0
        return SwitchEvent(
            timestamp=now,
            from_source_id=prev_source_id,
            to_source_id=best_source_id,
            reason="audio_score"
        )

    def manual_switch(self, source_id: int, preset: ScenePreset = ScenePreset.SPEAKER) -> SwitchEvent:
        prev_source_id = self.current_source_id
        self.mode_auto = False
        self.current_source_id = source_id
        self.last_switch_time = self._now_sec()
        self.pending_source_id = None
        self.pending_since = 0
        self.logger.info(f"Manual switch to source {source_id}")
        return SwitchEvent(
            timestamp=self._now_sec(),
            from_source_id=prev_source_id,
            to_source_id=source_id,
            reason="manual_override",
            preset=preset
        )

    def enable_auto(self):
        self.mode_auto = True
        self.logger.info("Auto mode enabled")
