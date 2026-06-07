class ConfigService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigService, cls).__new__(cls)
            cls._instance._load_defaults()
        return cls._instance

    def _load_defaults(self):
        # Switching
        self.min_frame_duration = 0.01
        self.cooldown = 1.0
        self.audio_threshold = 0.03
        self.switch_hysteresis = 0.02
        self.switch_penalty = 0.02

        # Sources / UI
        self.default_camera_slots = 2
        self.max_sources = 6
        self.camera_scan_max_index = 30

        # Не показывать как обычные камеры.
        # Desk View не выкидываю, потому что он может быть полезен.
        self.excluded_video_name_parts = [
            "capture screen",
            "захват экрана",
        ]

        # Video
        self.video_fps = 30

        # Audio capture
        self.audio_sample_rate = None
        self.audio_blocksize = 0
        self.audio_latency = "high"
        self.audio_channels = 1

        # Audio health
        self.audio_status_log_interval_sec = 2.0
        self.audio_restart_window_sec = 5.0
        self.audio_restart_max_errors = 6
        self.audio_restart_cooldown_sec = 10.0

        # Manual hints, optional
        self.source_audio_device_hints = {}

        # Timestamp buffers
        self.media_buffer_seconds = 10.0

        # Recording
        self.record_audio = True
        self.auto_cleanup_audio_temp = True
        self.auto_cleanup_audio_on_failure = True

        self.output_path = "output/recording.mp4"

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)


config_service = ConfigService()