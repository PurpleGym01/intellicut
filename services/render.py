import cv2
import numpy as np
from utils.logger import logger_service
from config.settings import config_service
from pathlib import Path
from datetime import datetime


class FFmpegAdapter:
    def __init__(self):
        self.logger = logger_service.get_logger()
        self.writer = None
        self.output_path = None
        self.fps = 30
        self.frame_size = None

    @staticmethod
    def _build_output_path() -> str:
        base_path = Path(config_service.output_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{base_path.stem}_{timestamp}{base_path.suffix}"
        return str(base_path.with_name(filename))

    def start_recording(self, width=640, height=480, fps=30):
        del width, height
        self.output_path = self._build_output_path()
        Path(self.output_path).parent.mkdir(exist_ok=True)
        self.fps = fps
        self.frame_size = None
        self.writer = None
        self.logger.info(f"Recording prepared (video-only): {self.output_path}")
        return True

    def write_frame(self, frame):
        if frame is None or frame.size == 0:
            return

        # OpenCV VideoWriter on macOS may crash on non-contiguous views/slices.
        if not frame.flags["C_CONTIGUOUS"]:
            frame = np.ascontiguousarray(frame)

        h, w = frame.shape[:2]
        if self.writer is None:
            self.frame_size = (w, h)
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self.writer = cv2.VideoWriter(self.output_path, fourcc, self.fps, self.frame_size)
            if not self.writer.isOpened():
                self.logger.error(f"VideoWriter failed to open: {self.output_path}")
                self.writer = None
                return
            self.logger.info(f"Video recording started: {self.output_path} ({w}x{h}@{self.fps}fps)")

        if (w, h) != self.frame_size:
            frame = cv2.resize(frame, self.frame_size)
        self.writer.write(frame)

    def switch_layout(self, layout_config: str):
        self.logger.info(f"Layout switch to {layout_config}")

    def stop_recording(self):
        if self.writer:
            self.writer.release()
            self.writer = None
            self.logger.info(f"Recording stopped: {self.output_path}")
