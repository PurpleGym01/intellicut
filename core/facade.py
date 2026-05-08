from services.ingest import IngestService
from services.analysis import AnalysisService, AudioActivityStrategy
from services.switching import SwitchingEngine
from services.render import FFmpegAdapter
from core.events import event_bus
from models.domain import SwitchEvent, ScenePreset, SourceStatus
from utils.logger import logger_service
from config.settings import config_service

class IntellicutFacade:
    def __init__(self):
        self.ingest = IngestService()
        self.analysis = AnalysisService(AudioActivityStrategy())
        self.switching = SwitchingEngine()
        self.render = FFmpegAdapter()
        self.logger = logger_service.get_logger()
        self.is_running = False
        self.scene_configured = False

    def setup_scene(self, source_names: list, reset: bool = False):
        if self.is_running and reset:
            self.stop()
        if self.scene_configured and not reset:
            self.logger.warning("Scene already configured; call setup_scene(..., reset=True) to rebuild.")
            return
        if reset:
            self.ingest.reset_scene()
            self.switching.current_source_id = None
            self.switching.pending_source_id = None
            self.switching.pending_since = 0
            self.switching.last_switch_time = 0

        detected = self.ingest.discovered_video_devices
        if detected:
            self.logger.info(f"Using detected video device indices for scene: {detected}")
        else:
            self.logger.warning("No detected video devices. Falling back to sequential indices.")

        for i, name in enumerate(source_names):
            if i < len(detected):
                device_id = detected[i]
            else:
                device_id = i
            self.ingest.add_source(name, device_id=device_id)
        self.scene_configured = True
        self.logger.info(f"Scene setup with {len(source_names)} sources")

    def start(self):
        if self.is_running:
            self.logger.warning("System already running")
            return
        if not self.ingest.get_sources():
            raise ValueError("No sources configured")
        self.is_running = True
        if not self.render.start_recording():
            self.logger.warning("Recording backend failed to start. Continuing without recording.")
        self.logger.info("System STARTED")
        event_bus.notify({"status": "started"})

    def stop(self):
        if not self.is_running:
            return
        self.is_running = False
        self.render.stop_recording()
        self.logger.info("System STOPPED")
        event_bus.notify({"status": "stopped"})

    def tick(self):
        """Основной цикл обработки (имитация потока)"""
        if not self.is_running:
            return

        # 1. Получение данных (Ingest)
        # Если часть камер не доступна, держим неактивные источники на нулевом уровне.
        if self.ingest.emulation_mode:
            audio_levels = [0.0 for _ in range(len(self.ingest.get_sources()))]
            self.ingest.update_audio_levels(audio_levels)

        # Обновляем данные из реальных источников (если есть)
        sources = self.ingest.get_sources()
        active_sources = [src for src in sources if src.status == SourceStatus.ACTIVE]
        if not active_sources:
            return

        # Если текущий источник перестал быть активным, сбрасываем выбор.
        active_ids = {src.id for src in active_sources}
        if self.switching.current_source_id not in active_ids:
            self.switching.current_source_id = None

        # 2. Анализ (Analysis)
        scores = self.analysis.evaluate_sources(
            active_sources,
            self.switching.current_source_id
        )

        # 3. Решение (Switching)
        event = self.switching.decide(scores, active_sources)

        if event:
            # 4. Рендер (Render)
            layout = "single" if event.preset == ScenePreset.SPEAKER else "split"
            self.render.switch_layout(layout)
            # 5. Уведомление (Observer)
            event_bus.notify(event)

    def manual_override(self, source_id: int, preset: ScenePreset = ScenePreset.SPEAKER):
        event = self.switching.manual_switch(source_id, preset)
        self.render.switch_layout(preset.value)
        event_bus.notify(event)

    def enable_auto(self):
        self.switching.enable_auto()
