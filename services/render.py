import cv2
import numpy as np
from utils.logger import logger_service
from config.settings import config_service
from pathlib import Path
from datetime import datetime
import time


class FFmpegAdapter:
    def __init__(self):
        self.logger = logger_service.get_logger()
        self.writer = None
        self.output_path = None
        self.fps = 30
        self.frame_size = None
        self.last_output_path = None
        self._frame_interval_sec = 1.0 / 30.0
        self._next_frame_ts = None
        self._frame_count = 0
        self._start_ts = None
        self._last_good_frame = None

    @staticmethod
    def _build_output_path() -> str:
        base_path = Path(config_service.output_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{base_path.stem}_{timestamp}{base_path.suffix}"
        return str(base_path.with_name(filename))

    def start_recording(self, width=640, height=480, fps=30):
        del width, height
        self.output_path = self._build_output_path()
        self.last_output_path = self.output_path
        Path(self.output_path).parent.mkdir(exist_ok=True)
        self.fps = int(getattr(config_service, "video_fps", fps) or fps)
        self.frame_size = None
        self.writer = None
        self._frame_interval_sec = 1.0 / max(self.fps, 1)
        self._next_frame_ts = None
        self._frame_count = 0
        self._start_ts = None
        self._last_good_frame = None
        self.logger.info(f"Recording prepared (video-only): {self.output_path}")
        return True

    def _write_raw_frame(self, frame) -> bool:
        if frame is None or frame.size == 0:
            return False

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
                return False
            self.logger.info(f"Video recording started: {self.output_path} ({w}x{h}@{self.fps}fps)")

        if (w, h) != self.frame_size:
            frame = cv2.resize(frame, self.frame_size)

        self.writer.write(frame)
        self._last_good_frame = frame
        self._frame_count += 1
        return True

    def write_frame(self, frame):
        if frame is None or frame.size == 0:
            return

        # Legacy path for callers that still pass a GUI-selected frame directly.
        now = time.perf_counter()
        if self._start_ts is None:
            self._start_ts = now
        elapsed = now - self._start_ts
        expected_frames = int(elapsed * self.fps) + 1
        if expected_frames <= self._frame_count:
            return

        frames_to_write = expected_frames - self._frame_count
        for _ in range(frames_to_write):
            if not self._write_raw_frame(frame):
                break
        self._next_frame_ts = None

    def write_timeline_frames(self, ingest, timeline, clock, up_to_ns=None):
        if self.output_path is None or timeline is None:
            return
        if up_to_ns is None:
            up_to_ns = clock.now_ns() if clock is not None else 0
        up_to_ns = max(0, int(up_to_ns))
        expected_frames = int((up_to_ns * self.fps) // 1_000_000_000) + 1
        if expected_frames <= self._frame_count:
            return

        while self._frame_count < expected_frames:
            frame_index = self._frame_count
            # Output timestamps are derived from frame index, not UI-loop timing.
            output_ts_ns = int((frame_index * 1_000_000_000) // max(self.fps, 1))
            source_id = timeline.source_at(output_ts_ns)
            frame = None
            if source_id is not None:
                timed_frame = ingest.get_timed_frame_at(source_id, output_ts_ns)
                if timed_frame is not None:
                    frame = timed_frame.frame
            if frame is None:
                frame = self._last_good_frame
            if not self._write_raw_frame(frame):
                break
        self._next_frame_ts = None

    def switch_layout(self, layout_config: str):
        self.logger.info(f"Layout switch to {layout_config}")

    def stop_recording(self):
        if self.writer:
            self.writer.release()
            self.writer = None
            self.logger.info(f"Recording stopped: {self.output_path}")
        return self.output_path
