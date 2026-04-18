import logging
import sys
from pathlib import Path


class LoggerService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LoggerService, cls).__new__(cls)
            cls._instance._configure()
        return cls._instance

    def _configure(self):
        self.logger = logging.getLogger("Intellicut")
        self.logger.setLevel(logging.INFO)

        # Создание папки для логов
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)

        # Файловый хендлер
        fh = logging.FileHandler(log_dir / "intellicut.log", encoding='utf-8')
        fh.setLevel(logging.INFO)

        # Консольный хендлер
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)

        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)

        self.logger.addHandler(fh)
        self.logger.addHandler(ch)

    def get_logger(self):
        return self.logger


# Глобальный экземпляр
logger_service = LoggerService()