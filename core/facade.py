from pathlib import Path

from services.ingest import IngestService
from services.analysis import AnalysisService, AudioActivityStrategy
from services.switching import SwitchingEngine
from services.render import FFmpegAdapter
from services.audio_mux import mux_audio_video
from services.audio_denoise import denoise_wav_with_deepfilter
from services.clock import RecordingClock
from services.timeline import Timeline
from core.events import event_bus
from models.domain import ScenePreset, SourceStatus
from utils.logger import logger_service
from config.settings import config_service


class IntellicutFacade:
    def __init__(self):
        self.clock = RecordingClock()
        self.timeline = Timeline()
        self.ingest = IngestService(self.clock)
        self.analysis = AnalysisService(AudioActivityStrategy())
        self.switching = SwitchingEngine(self.clock)
        self.render = FFmpegAdapter()
        self.logger = logger_service.get_logger()
        self.is_running = False
        self.scene_configured = False

    def setup_scene(
        self,
        source_names: list,
        reset: bool = False,
        video_device_ids: list = None,
        audio_device_ids: list = None,
    ):
        if self.is_running and reset:
            self.stop()
        if self.scene_configured and not reset:
            self.logger.warning("Scene already configured; call setup_scene(..., reset=True) to rebuild.")
            return
        if reset:
            self.ingest.reset_scene()
            self.clock.reset()
            self.timeline.reset()
            self.switching.current_source_id = None
            self.switching.pending_source_id = None
            self.switching.pending_since = 0
            self.switching.last_switch_time = 0

        detected = video_device_ids if video_device_ids is not None else self.ingest.discovered_video_devices
        if detected:
            self.logger.info(f"Using detected video device indices for scene: {detected}")
        else:
            self.logger.warning("No detected video devices. Falling back to sequential indices.")

        for i, name in enumerate(source_names):
            if i < len(detected):
                device_id = detected[i]
            elif video_device_ids is not None:
                self.logger.warning(f"Skipping {name}: no selected video device.")
                continue
            else:
                device_id = i
            audio_device_id = audio_device_ids[i] if audio_device_ids is not None and i < len(audio_device_ids) else None
            self.ingest.add_source(name, device_id=device_id, audio_device_id=audio_device_id)
        self.scene_configured = True
        self.logger.info(f"Scene setup with {len(source_names)} sources")

    def start(self):
        if self.is_running:
            self.logger.warning("System already running")
            return
        if not self.ingest.get_sources():
            raise ValueError("No sources configured")
        self.is_running = True
        if not self.render.start_recording(fps=int(getattr(config_service, "video_fps", 30) or 30)):
            self.logger.warning("Recording backend failed to start. Continuing without recording.")
        else:
            self.timeline.reset()
            self.ingest.clear_media_buffers()
            self.ingest.start_audio_recording(self.render.output_path, self.switching.current_source_id)
            initial_source_id = self.switching.current_source_id or self.ingest.default_active_source_id()
            self.clock.start()
            self.timeline.open(initial_source_id, 0, "recording_start", 0.0)
        self.logger.info("System STARTED")
        event_bus.notify({"status": "started"})

    def stop(self):
        if not self.is_running:
            return
        duration_ns = self.clock.now_ns() if self.clock.is_started else 0
        self.is_running = False
        if self.clock.is_started:
            self.render.write_timeline_frames(self.ingest, self.timeline, self.clock, up_to_ns=duration_ns)
            if self.render._frame_count:
                duration_ns = int((self.render._frame_count * 1_000_000_000) // max(self.render.fps, 1))
            self.timeline.close(duration_ns)
        if self.ingest.audio_recording_active:
            self.ingest.stop_audio_recording(self.timeline, duration_ns)
        video_path = self.render.stop_recording()
        audio_success = False
        mux_success = False
        if getattr(config_service, "record_audio", False) and video_path:
            audio_paths = list(self.ingest.audio_recording_paths or [])
            if audio_paths:
                audio_success = True
                audio_for_mux = denoise_wav_with_deepfilter(
                    input_wav=audio_paths[0],
                    output_dir=Path(video_path).parent,
                    enabled=bool(getattr(config_service, "denoise_audio", True)),
                )
                muxed = mux_audio_video(video_path, str(audio_for_mux))
                if muxed:
                    mux_success = True
                    self.render.last_output_path = muxed
                    self.logger.info(f"Audio muxed into output: {muxed}")
                else:
                    self.logger.warning("Audio mux failed; keeping video-only output")
            else:
                self.logger.info("No timeline audio file to mux; keeping video-only output")

        self.clock.stop()

        # Auto-cleanup temporary audio files if configured.
        if getattr(config_service, "auto_cleanup_audio_temp", False):
            cleanup_on_failure = bool(getattr(config_service, "auto_cleanup_audio_on_failure", False))
            should_cleanup = audio_success and mux_success
            if cleanup_on_failure:
                should_cleanup = True
            if should_cleanup:
                self.ingest.cleanup_audio_recording_files()

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
            if self.clock.is_started:
                self.timeline.switch_to(
                    event.to_source_id,
                    self.clock.now_ns(),
                    event.reason,
                    scores.get(event.to_source_id, 0.0),
                )
            # 5. Уведомление (Observer)
            event_bus.notify(event)

    def record_frame(self):
        if self.is_running and self.clock.is_started:
            self.render.write_timeline_frames(self.ingest, self.timeline, self.clock)

    def manual_override(self, source_id: int, preset: ScenePreset = ScenePreset.SPEAKER):
        event = self.switching.manual_switch(source_id, preset)
        self.render.switch_layout(preset.value)
        if self.clock.is_started:
            self.timeline.switch_to(source_id, self.clock.now_ns(), event.reason, 0.0)
        event_bus.notify(event)

    def enable_auto(self):
        self.switching.enable_auto()
