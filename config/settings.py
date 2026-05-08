from dataclasses import dataclass

class ConfigService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigService, cls).__new__(cls)
            cls._instance._load_defaults()
        return cls._instance

    def _load_defaults(self):
        # Параметры из ТЗ: минимальная длительность, cooldown, порог
        self.min_frame_duration = 0.01  # секунды: подтверждение нового активного спикера
        self.cooldown = 1.0            # секунды: минимальная длина текущего кадра
        self.audio_threshold = 0.03    # нормализованный порог
        self.switch_hysteresis = 0.02  # на сколько новый источник должен быть выше текущего
        self.switch_penalty = 0.02     # штраф за переключение в скоринге
        self.max_sources = 3
        self.camera_scan_max_index = 30
        # Параметры аудио-захвата (стабильность потока важнее низкой задержки).
        # audio_sample_rate=None -> использовать default_samplerate устройства.
        self.audio_sample_rate = None
        self.audio_blocksize = 0
        self.audio_latency = "high"
        self.audio_channels = 1
        # Явная привязка источника к аудио-инпуту:
        # - int: индекс устройства sounddevice
        # - str: подстрока имени устройства (регистронезависимо)
        # - None/отсутствует: авто-выбор
        self.source_audio_device_hints = {
            # "Camera 1": 0,
            # "Camera 2": "iPhone Microphone",
        }
        # Временный флаг для быстрой диагностики "перепутанных" микрофонов.
        # Если True, первые два автоматически выбранных аудио-девайса меняются местами.
        self.swap_first_two_audio_sources = True
        # Включать ли запись аудио в финальный mp4.
        self.record_audio = False
        # Индекс аудио-устройства для ffmpeg (macOS avfoundation). Обычно 0.
        self.ffmpeg_audio_device_index = 0
        self.output_path = "output/recording.mp4"

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

config_service = ConfigService()
