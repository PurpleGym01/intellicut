import cv2
import numpy as np
import sounddevice as sd
from models.domain import VideoSource, SourceType, SourceStatus
from models.media import TimedAudioChunk, TimedVideoFrame, TimelineSegment
from services.buffers import TimeRingBuffer
from services.clock import RecordingClock
from utils.logger import logger_service
from typing import Dict, List, Optional
import threading
import time
import sys
import subprocess
from config.settings import config_service
import wave
import math
from pathlib import Path

try:
    from scipy.signal import resample_poly
except Exception:
    resample_poly = None


class SourceFactory:
    @staticmethod
    def create_source(source_id: int, name: str, type_str: str) -> VideoSource:
        try:
            s_type = SourceType(type_str.lower())
        except ValueError:
            s_type = SourceType.FILE
        return VideoSource(id=source_id, name=name, type=s_type, status=SourceStatus.ACTIVE)


class CameraCapture:
    """Обертка для захвата с камеры"""

    def __init__(
        self,
        device_id: int,
        source_name: str,
        audio_device_id: Optional[int] = None,
        source_id: int = 0,
        clock: Optional[RecordingClock] = None,
        buffer_seconds: Optional[float] = None,
    ):
        self.source_id = int(source_id or 0)
        self.device_id = device_id
        self.source_name = source_name
        self.clock = clock or RecordingClock()
        self.cap = None
        self.frame = None
        self.last_timed_frame = None
        self.running = False
        self.frame_lock = threading.Lock()
        self.available = False
        self.seq = 0
        window_sec = float(buffer_seconds if buffer_seconds is not None else config_service.media_buffer_seconds)
        self.video_buffer = TimeRingBuffer(window_sec)
        self.audio_capture = MicrophoneCapture(
            source_name=source_name,
            device_id=audio_device_id,
            source_id=self.source_id,
            clock=self.clock,
            audio_buffer=TimeRingBuffer(window_sec),
        )
        self.capture_thread = None

    @staticmethod
    def _camera_backends():
        if sys.platform == "darwin":
            return [cv2.CAP_AVFOUNDATION, cv2.CAP_ANY]
        if sys.platform.startswith("win"):
            return [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY]
        return [cv2.CAP_V4L2, cv2.CAP_ANY]

    def start(self):
        backends = self._camera_backends()

        for backend in backends:
            self.cap = cv2.VideoCapture(self.device_id, backend)
            if self.cap.isOpened():
                self.available = True
                logger_service.get_logger().info(
                    f"Camera {self.device_id} opened with backend {backend} for {self.source_name}"
                )
                break
            self.cap.release()
            self.cap = None

        if self.cap is None or not self.cap.isOpened():
            logger_service.get_logger().warning(f"Camera {self.device_id} not available, using emulation")
            self.available = False
            return

        self.running = True
        self.audio_capture.start()
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()

    def _capture_loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if ret:
                self.seq += 1
                # Timestamp is recorded from the shared clock so video can be selected by timeline time.
                timed_frame = TimedVideoFrame(
                    source_id=self.source_id,
                    timestamp_ns=self.clock.now_ns(),
                    frame=frame,
                    seq=self.seq,
                )
                with self.frame_lock:
                    self.frame = frame
                    self.last_timed_frame = timed_frame
                if self.clock.is_started:
                    self.video_buffer.push(timed_frame)
            else:
                time.sleep(0.1)

    def clear_buffer(self):
        self.video_buffer.clear()
        with self.frame_lock:
            self.last_timed_frame = None

    def get_frame(self):
        with self.frame_lock:
            return self.frame.copy() if self.frame is not None else None

    def latest_frame(self):
        return self.get_frame()

    def get_timed_frame_at(self, timestamp_ns: int):
        frame = self.video_buffer.closest_before_or_at(timestamp_ns)
        if frame is None:
            frame = self.video_buffer.closest(timestamp_ns)
        if frame is None:
            with self.frame_lock:
                frame = self.last_timed_frame
        return frame

    def get_frame_at(self, timestamp_ns: int):
        timed_frame = self.get_timed_frame_at(timestamp_ns)
        if timed_frame is not None:
            return timed_frame.frame.copy()
        return self.get_frame()

    def get_audio_level(self):
        return self.audio_capture.get_level()

    def stop(self):
        self.running = False
        self.audio_capture.stop()
        # Сначала освобождаем камеру, чтобы разбудить/разблокировать read() в capture-thread.
        if self.cap:
            self.cap.release()
            self.cap = None
        if self.capture_thread and self.capture_thread.is_alive():
            self.capture_thread.join(timeout=1.0)
        self.capture_thread = None
        self.available = False


class MicrophoneCapture:
    def __init__(
        self,
        source_name: str,
        device_id: Optional[int],
        source_id: int = 0,
        clock: Optional[RecordingClock] = None,
        audio_buffer: Optional[TimeRingBuffer] = None,
    ):
        self.source_id = int(source_id or 0)
        self.source_name = source_name
        self.device_id = device_id
        self.clock = clock or RecordingClock()
        self.audio_buffer = audio_buffer or TimeRingBuffer(float(config_service.media_buffer_seconds))
        self.stream = None
        self.level = 0.0
        self.available = False
        self.lock = threading.Lock()
        self.logger = logger_service.get_logger()
        self.sample_rate = None
        self.channels = 1
        self.blocksize = 0
        self.latency = "high"
        self.status_error_times = []
        self.last_status_log_ts = 0.0
        self.last_restart_ts = 0.0
        self.restart_requested = False
        self.restart_lock = threading.Lock()
        self.recording_lock = threading.Lock()
        self.recording_active = False
        self.recorded_chunks: List[TimedAudioChunk] = []

    def _audio_callback(self, indata, frames, time_info, status):
        del time_info
        now = time.monotonic()
        if status:
            log_interval = float(getattr(config_service, "audio_status_log_interval_sec", 2.0))
            if now - self.last_status_log_ts >= log_interval:
                self.logger.debug(f"Audio callback status for {self.source_name}: {status}")
                self.last_status_log_ts = now
            self._track_status_error(now)
        try:
            # Векторизованный RMS без лишних аллокаций.
            rms = float(np.sqrt(np.mean(indata * indata)))
        except Exception:
            rms = 0.0
        # Приводим к диапазону 0..1 и сглаживаем, чтобы не было резкой дерготни.
        normalized = min(1.0, rms * 12.0)
        with self.lock:
            self.level = 0.85 * self.level + 0.15 * normalized

        if self.clock.is_started:
            sample_rate = int(self.sample_rate or 16000)
            end_ns = self.clock.now_ns()
            duration_ns = int(round(int(frames) * 1_000_000_000 / max(sample_rate, 1)))
            start_ns = max(0, end_ns - duration_ns)
            chunk = TimedAudioChunk(
                source_id=self.source_id,
                start_ns=start_ns,
                end_ns=end_ns,
                samples=indata.copy(),
                sample_rate=sample_rate,
                rms=rms,
            )
            self.audio_buffer.push(chunk)
            with self.recording_lock:
                if self.recording_active:
                    self.recorded_chunks.append(chunk)

    def _track_status_error(self, now_ts: float):
        window_sec = float(getattr(config_service, "audio_restart_window_sec", 5.0))
        max_errors = int(getattr(config_service, "audio_restart_max_errors", 6))
        cooldown = float(getattr(config_service, "audio_restart_cooldown_sec", 10.0))
        if now_ts - self.last_restart_ts < cooldown:
            return
        self.status_error_times = [t for t in self.status_error_times if now_ts - t <= window_sec]
        self.status_error_times.append(now_ts)
        if len(self.status_error_times) >= max_errors:
            with self.restart_lock:
                self.restart_requested = True

    def _restart_if_needed(self):
        with self.restart_lock:
            if not self.restart_requested:
                return
            self.restart_requested = False
        self.last_restart_ts = time.monotonic()
        self.status_error_times.clear()
        self.logger.warning(f"Restarting audio stream for {self.source_name} due to repeated errors")
        self._close_stream()
        time.sleep(0.2)
        self.start()

    def _resolve_stream_params(self):
        # Опираемся на параметры устройства, чтобы не ломать аудиостек ОС.
        self.channels = max(1, int(getattr(config_service, "audio_channels", 1)))
        self.blocksize = int(getattr(config_service, "audio_blocksize", 0) or 0)
        self.latency = getattr(config_service, "audio_latency", "high") or "high"
        sample_rate = getattr(config_service, "audio_sample_rate", None)
        if sample_rate:
            self.sample_rate = int(sample_rate)
            return
        try:
            device_info = sd.query_devices(self.device_id)
            self.sample_rate = int(device_info.get("default_samplerate", 16000))
        except Exception:
            self.sample_rate = 16000

    def start(self):
        if self.device_id is None:
            self.logger.warning(f"No audio input assigned for {self.source_name}. Using level=0.")
            return
        try:
            self._resolve_stream_params()
            self.stream = sd.InputStream(
                device=self.device_id,
                channels=self.channels,
                samplerate=self.sample_rate,
                blocksize=self.blocksize,
                latency=self.latency,
                callback=self._audio_callback,
            )
            self.stream.start()
            self.available = True
            device_name = sd.query_devices(self.device_id)["name"]
            self.logger.info(
                "Audio input opened for %s: [%s] %s (sr=%s, block=%s, latency=%s)",
                self.source_name,
                self.device_id,
                device_name,
                self.sample_rate,
                self.blocksize,
                self.latency,
            )
        except Exception as e:
            self.available = False
            self.logger.warning(f"Audio input failed for {self.source_name}: {e}")

    def start_recording(self):
        with self.recording_lock:
            self.recorded_chunks = []
            self.recording_active = True
        self.audio_buffer.clear()
        return True

    def stop_recording(self):
        with self.recording_lock:
            self.recording_active = False
            return list(self.recorded_chunks)

    def get_level(self) -> float:
        self._restart_if_needed()
        with self.lock:
            return self.level

    def _close_stream(self):
        if self.stream:
            try:
                if self.stream.active:
                    self.stream.stop()
            except Exception as e:
                self.logger.debug(f"Audio stop failed for {self.source_name}: {e}")
            try:
                self.stream.close()
            except Exception as e:
                self.logger.debug(f"Audio close failed for {self.source_name}: {e}")
        self.stream = None
        self.available = False

    def stop(self):
        self._close_stream()
        with self.restart_lock:
            self.restart_requested = False
        self.status_error_times.clear()
        self.stop_recording()


class IngestService:
    def __init__(self, clock: Optional[RecordingClock] = None):
        self.clock = clock or RecordingClock()
        self.sources: List[VideoSource] = []
        self.captures: List[CameraCapture] = []
        self.video_buffers: Dict[int, TimeRingBuffer] = {}
        self.audio_buffers: Dict[int, TimeRingBuffer] = {}
        self.source_audio_device_ids: Dict[int, Optional[int]] = {}
        self.logger = logger_service.get_logger()
        self.emulation_mode = False
        self.audio_input_devices = self._list_audio_input_devices()
        self.used_audio_device_ids = set()
        self.auto_audio_device_queue = self._build_auto_audio_queue()
        self.discovered_video_devices = self.discover_video_devices(config_service.camera_scan_max_index)
        self.stopped = False
        self.audio_recording_active = False
        self.audio_recording_paths = []
        self.audio_output_dir = None
        self.active_audio_recording_path = None

    def refresh_devices(self):
        self.audio_input_devices = self._list_audio_input_devices()
        self.used_audio_device_ids.clear()
        self.auto_audio_device_queue = self._build_auto_audio_queue()
        self.discovered_video_devices = self.discover_video_devices(config_service.camera_scan_max_index)

    def reset_scene(self):
        # Полная остановка перед пересборкой сцены.
        self.stop_all()
        self.stopped = False
        self.emulation_mode = False
        self.refresh_devices()

    def _list_audio_input_devices(self):
        devices = []
        try:
            all_devices = sd.query_devices()
            for idx, dev in enumerate(all_devices):
                if int(dev.get("max_input_channels", 0)) > 0:
                    devices.append({"index": idx, "name": dev.get("name", f"Input-{idx}")})
            if devices:
                self.logger.info(
                    "Audio inputs detected: "
                    + ", ".join(f"[{d['index']}] {d['name']}" for d in devices)
                )
            else:
                self.logger.warning("No audio input devices detected.")
        except Exception as e:
            self.logger.warning(f"Failed to query audio input devices: {e}")
        return devices

    def _reserve_audio_device(self, preferred_index: Optional[int] = None) -> Optional[int]:
        if preferred_index is not None:
            for dev in self.audio_input_devices:
                if dev["index"] == preferred_index and preferred_index not in self.used_audio_device_ids:
                    self.used_audio_device_ids.add(preferred_index)
                    return preferred_index

        for dev in self.audio_input_devices:
            idx = dev["index"]
            if idx not in self.used_audio_device_ids:
                self.used_audio_device_ids.add(idx)
                return idx
        return None

    def _build_auto_audio_queue(self) -> List[int]:
        return [dev["index"] for dev in self.audio_input_devices]

    def _reserve_audio_device_by_hint(self, hint) -> Optional[int]:
        if hint is None:
            return None
        if isinstance(hint, int):
            for dev in self.audio_input_devices:
                if dev["index"] == hint and hint not in self.used_audio_device_ids:
                    self.used_audio_device_ids.add(hint)
                    return hint
            self.logger.warning(f"Audio hint index {hint} not available or already used.")
            return None
        if isinstance(hint, str):
            lowered_hint = hint.lower()
            for dev in self.audio_input_devices:
                if dev["index"] in self.used_audio_device_ids:
                    continue
                if lowered_hint in str(dev["name"]).lower():
                    idx = dev["index"]
                    self.used_audio_device_ids.add(idx)
                    return idx
            self.logger.warning(f"Audio hint '{hint}' did not match any free audio input.")
        return None

    def reserve_audio_device_for_source(self, source_id: int, source_name: str, camera_device_id: int) -> Optional[int]:
        hints = getattr(config_service, "source_audio_device_hints", {}) or {}
        hinted = hints.get(source_id, hints.get(source_name))
        device_id = self._reserve_audio_device_by_hint(hinted)
        if device_id is not None:
            return device_id
        while self.auto_audio_device_queue:
            queued_id = self.auto_audio_device_queue.pop(0)
            if queued_id not in self.used_audio_device_ids:
                self.used_audio_device_ids.add(queued_id)
                return queued_id
        return self._reserve_audio_device(preferred_index=camera_device_id)

    def discover_video_devices(self, max_index: int) -> List[int]:
        cv2_found = []
        for device_id in range(max_index + 1):
            cap = None
            try:
                for backend in CameraCapture._camera_backends():
                    cap = cv2.VideoCapture(device_id, backend)
                    if cap.isOpened():
                        cv2_found.append(device_id)
                        break
                    cap.release()
                    cap = None
            except Exception:
                pass
            finally:
                if cap is not None:
                    cap.release()

        listed = []
        excluded_listed = []
        if sys.platform == "darwin":
            listed, excluded_listed = self._avfoundation_video_device_ids(max_index)
        found = sorted(set(cv2_found).union(listed).difference(excluded_listed))
        if found:
            self.logger.info(
                f"Video devices detected in range 0..{max_index}: {', '.join(str(i) for i in found)}"
            )
            if listed:
                self.logger.info(
                    f"AVFoundation listed video devices: {', '.join(str(i) for i in listed)}"
                )
            if excluded_listed:
                self.logger.info(
                    f"AVFoundation video devices hidden by filter: {', '.join(str(i) for i in excluded_listed)}"
                )
        else:
            self.logger.warning(f"No video devices found in range 0..{max_index}")
        return found

    def _avfoundation_video_device_ids(self, max_index: int) -> tuple[List[int], List[int]]:
        ids = []
        excluded_ids = []
        try:
            proc = subprocess.run(
                ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=4,
            )
        except Exception:
            return ids, excluded_ids

        in_video = False
        for line in proc.stdout.splitlines():
            if "AVFoundation video devices" in line:
                in_video = True
                continue
            if "AVFoundation audio devices" in line:
                in_video = False
                continue
            if not in_video or "] [" not in line:
                continue
            left = line.rsplit("[", 1)[-1]
            idx, _, name = left.partition("]")
            if idx.strip().isdigit():
                device_id = int(idx.strip())
                if 0 <= device_id <= max_index:
                    if config_service.is_excluded_video_name(name):
                        excluded_ids.append(device_id)
                    else:
                        ids.append(device_id)
        return sorted(set(ids)), sorted(set(excluded_ids))

    def _audio_device_exists(self, device_id: Optional[int]) -> bool:
        return any(dev["index"] == device_id for dev in self.audio_input_devices)

    def add_source(self, name: str, device_id: int = 0, audio_device_id: Optional[int] = None) -> VideoSource:
        if len(self.sources) >= config_service.max_sources:
            raise ValueError(f"Max sources limit ({config_service.max_sources}) reached")

        source = SourceFactory.create_source(len(self.sources) + 1, name, "camera")
        if audio_device_id is None:
            audio_device_id = self.reserve_audio_device_for_source(
                source.id,
                name,
                camera_device_id=device_id,
            )
        elif not self._audio_device_exists(audio_device_id):
            self.logger.warning(f"Audio device [{audio_device_id}] is not available; {name} will start without audio.")
            audio_device_id = None
        elif audio_device_id in self.used_audio_device_ids:
            requested_audio_device_id = audio_device_id
            audio_device_id = self._reserve_audio_device()
            if audio_device_id is None:
                self.logger.warning(
                    f"Audio device [{requested_audio_device_id}] is already used; {name} will start without audio."
                )
            else:
                self.logger.warning(
                    f"Audio device [{requested_audio_device_id}] is already used; "
                    f"{name} will use free audio device [{audio_device_id}] instead."
                )
        else:
            self.used_audio_device_ids.add(audio_device_id)
        self.source_audio_device_ids[source.id] = audio_device_id

        capture = CameraCapture(
            device_id,
            source_name=name,
            audio_device_id=audio_device_id,
            source_id=source.id,
            clock=self.clock,
            buffer_seconds=float(config_service.media_buffer_seconds),
        )

        capture.start()

        # Если камера не доступна, помечаем режим эмуляции
        if not capture.available:
            self.emulation_mode = True
            source.status = SourceStatus.INACTIVE
            self.logger.warning(f"Source {name} in emulation mode")
        else:
            self.logger.info(f"Source {name} started on device {device_id}")
            if audio_device_id is None:
                self.logger.warning(f"Source {name}: no audio device assigned")
            else:
                audio_name = next(
                    (d["name"] for d in self.audio_input_devices if d["index"] == audio_device_id),
                    "unknown",
                )
                self.logger.info(
                    f"Source mapping: source[{source.id}] {name} -> video[{device_id}] + audio[{audio_device_id}] {audio_name}"
                )

        self.captures.append(capture)
        self.sources.append(source)
        self.video_buffers[source.id] = capture.video_buffer
        self.audio_buffers[source.id] = capture.audio_capture.audio_buffer
        return source

    def get_sources(self) -> List[VideoSource]:
        for i, src in enumerate(self.sources):
            if i < len(self.captures):
                # Если камера работает - берем реальные данные, иначе - эмуляция уже записана
                if self.captures[i].available:
                    mic = self.captures[i].audio_capture
                    src.audio_level = mic.get_level()
                    self.logger.debug(
                        "SRC %s audio=%.3f",
                        src.id,
                        src.audio_level,
                    )
        return self.sources

    def get_frame(self, source_index: int):
        if 0 <= source_index < len(self.captures):
            return self.captures[source_index].get_frame()
        return None

    def audio_device_for_source(self, source_id: int) -> Optional[int]:
        return self.source_audio_device_ids.get(int(source_id))

    def get_timed_frame_at(self, source_id: int, timestamp_ns: int):
        idx = int(source_id) - 1
        if 0 <= idx < len(self.captures):
            return self.captures[idx].get_timed_frame_at(timestamp_ns)
        return None

    def get_frame_at(self, source_id: int, timestamp_ns: int):
        idx = int(source_id) - 1
        if 0 <= idx < len(self.captures):
            return self.captures[idx].get_frame_at(timestamp_ns)
        return None

    def update_audio_levels(self, levels: List[float]):
        """Fallback для эмуляции только неактивных источников."""
        if self.emulation_mode:
            for i, level in enumerate(levels):
                if i < len(self.sources):
                    if self.sources[i].status != SourceStatus.ACTIVE:
                        self.sources[i].audio_level = level

    def _audio_output_params(self):
        for cap in self.captures:
            mic = cap.audio_capture
            if mic.device_id is None:
                continue
            if mic.sample_rate is None:
                mic._resolve_stream_params()
            return int(mic.sample_rate or 48000), int(mic.channels or 1)
        return 48000, 1

    def _fallback_audio_source_id(self) -> Optional[int]:
        active = [
            (idx + 1, source.audio_level)
            for idx, source in enumerate(self.get_sources())
            if idx < len(self.captures) and self.captures[idx].available and source.status == SourceStatus.ACTIVE
        ]
        if not active:
            return None
        return max(active, key=lambda item: item[1])[0]

    def default_active_source_id(self) -> Optional[int]:
        source_id = self._fallback_audio_source_id()
        if source_id is not None:
            return source_id
        for idx, source in enumerate(self.sources, start=1):
            if source.status == SourceStatus.ACTIVE:
                return idx
        return None

    def clear_media_buffers(self):
        for cap in self.captures:
            cap.clear_buffer()
            cap.audio_capture.audio_buffer.clear()

    def start_audio_recording(self, output_path: str, active_source_id: Optional[int] = None):
        del active_source_id
        if not getattr(config_service, "record_audio", False):
            return []
        if self.audio_recording_active:
            return self.audio_recording_paths
        base_path = Path(output_path)
        audio_dir = base_path.with_suffix("").as_posix() + "_audio"
        Path(audio_dir).mkdir(parents=True, exist_ok=True)
        audio_path = str(Path(audio_dir) / "active_speaker.wav")
        self.audio_output_dir = audio_dir
        self.active_audio_recording_path = audio_path
        self.audio_recording_paths = [audio_path]
        self.clear_media_buffers()
        for cap in self.captures:
            cap.audio_capture.start_recording()
        self.audio_recording_active = True
        return self.audio_recording_paths

    def stop_audio_recording(self, timeline=None, duration_ns: int = 0):
        if not self.audio_recording_active:
            return []
        chunks_by_source = {}
        for cap in self.captures:
            chunks_by_source[cap.source_id] = cap.audio_capture.stop_recording()
        paths = []
        if self.active_audio_recording_path:
            if self._build_active_speaker_wav(
                self.active_audio_recording_path,
                timeline,
                chunks_by_source,
                int(duration_ns),
            ):
                paths = [self.active_audio_recording_path]
        self.audio_recording_active = False
        self.audio_recording_paths = paths
        self.active_audio_recording_path = None
        return paths

    def _build_active_speaker_wav(self, output_path: str, timeline, chunks_by_source: Dict[int, List[TimedAudioChunk]], duration_ns: int) -> bool:
        sample_rate, channels = self._audio_output_params()
        channels = max(1, int(channels or 1))
        duration_ns = max(0, int(duration_ns))
        total_frames = max(1, int(math.ceil(duration_ns * sample_rate / 1_000_000_000))) if duration_ns else 1
        output = np.zeros((total_frames, channels), dtype=np.float32)
        segments = timeline.segments() if timeline is not None else []

        if not segments:
            fallback_source_id = self.default_active_source_id()
            if fallback_source_id is not None:
                segments = [TimelineSegment(0, duration_ns, fallback_source_id, "fallback", 0.0)]

        for segment in segments:
            segment_start_ns = max(0, int(segment.start_ns))
            segment_end_ns = duration_ns if segment.end_ns is None else int(segment.end_ns)
            if duration_ns:
                segment_end_ns = min(segment_end_ns, duration_ns)
            if segment_end_ns <= segment_start_ns:
                continue
            chunks = chunks_by_source.get(int(segment.source_id), [])
            for chunk in chunks:
                if chunk.end_ns <= segment_start_ns or chunk.start_ns >= segment_end_ns:
                    continue
                overlap_start_ns = max(segment_start_ns, int(chunk.start_ns))
                overlap_end_ns = min(segment_end_ns, int(chunk.end_ns))
                if overlap_end_ns <= overlap_start_ns:
                    continue

                src_start = int(round((overlap_start_ns - chunk.start_ns) * chunk.sample_rate / 1_000_000_000))
                src_end = int(round((overlap_end_ns - chunk.start_ns) * chunk.sample_rate / 1_000_000_000))
                src_start = max(0, min(src_start, chunk.samples.shape[0]))
                src_end = max(src_start, min(src_end, chunk.samples.shape[0]))
                if src_end <= src_start:
                    continue

                data = self._prepare_audio_channels(chunk.samples[src_start:src_end], channels)
                data = self._resample_audio(data, int(chunk.sample_rate or sample_rate), sample_rate)
                target_len = int(round((overlap_end_ns - overlap_start_ns) * sample_rate / 1_000_000_000))
                data = self._fit_audio_length(data, target_len, channels)
                if data.shape[0] <= 0:
                    continue

                out_start = int(round(overlap_start_ns * sample_rate / 1_000_000_000))
                out_start = max(0, min(out_start, output.shape[0]))
                out_end = min(output.shape[0], out_start + data.shape[0])
                if out_end <= out_start:
                    continue
                # Timeline chooses one active source; assignment avoids double-volume overlaps.
                output[out_start:out_end] = data[: out_end - out_start]

        try:
            self._write_wav(output_path, output, sample_rate, channels)
            return True
        except Exception as exc:
            self.logger.warning(f"Active speaker audio build failed: {exc}")
            return False

    def _prepare_audio_channels(self, data: np.ndarray, channels: int) -> np.ndarray:
        data = np.asarray(data, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        if data.shape[1] == channels:
            return data
        if channels == 1:
            return data.mean(axis=1, keepdims=True)
        if data.shape[1] == 1:
            return np.repeat(data, channels, axis=1)
        return data[:, :channels]

    def _resample_audio(self, data: np.ndarray, source_sample_rate: int, target_sample_rate: int) -> np.ndarray:
        if not source_sample_rate or source_sample_rate == target_sample_rate:
            return data.astype(np.float32, copy=False)
        if resample_poly is not None:
            gcd = math.gcd(source_sample_rate, target_sample_rate)
            return resample_poly(
                data,
                target_sample_rate // gcd,
                source_sample_rate // gcd,
                axis=0,
            ).astype(np.float32, copy=False)

        source_len = data.shape[0]
        if source_len <= 1:
            return data.astype(np.float32, copy=False)
        target_len = max(1, int(round(source_len * target_sample_rate / source_sample_rate)))
        old_x = np.linspace(0.0, 1.0, source_len, endpoint=False)
        new_x = np.linspace(0.0, 1.0, target_len, endpoint=False)
        channels = [np.interp(new_x, old_x, data[:, ch]) for ch in range(data.shape[1])]
        return np.stack(channels, axis=1).astype(np.float32, copy=False)

    def _fit_audio_length(self, data: np.ndarray, target_len: int, channels: int) -> np.ndarray:
        target_len = max(0, int(target_len))
        if data.shape[0] == target_len:
            return data
        if data.shape[0] > target_len:
            return data[:target_len]
        pad = np.zeros((target_len - data.shape[0], channels), dtype=np.float32)
        return np.vstack([data, pad])

    def _write_wav(self, output_path: str, data: np.ndarray, sample_rate: int, channels: int):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        pcm = np.clip(data, -1.0, 1.0)
        if channels == 1:
            pcm = pcm.reshape(-1)
        pcm = (pcm * 32767.0).astype(np.int16, copy=False)
        with wave.open(output_path, "wb") as out_wav:
            out_wav.setnchannels(channels)
            out_wav.setsampwidth(2)
            out_wav.setframerate(sample_rate)
            out_wav.writeframes(pcm.tobytes())

    def cleanup_audio_recording_files(self):
        if not self.audio_recording_paths and not self.audio_output_dir:
            return
        for path in list(self.audio_recording_paths or []):
            try:
                if path:
                    Path(path).unlink(missing_ok=True)
            except Exception as exc:
                self.logger.debug(f"Audio temp cleanup failed for {path}: {exc}")
        try:
            if self.audio_output_dir:
                for file_path in Path(self.audio_output_dir).glob("*.wav"):
                    file_path.unlink(missing_ok=True)
        except Exception as exc:
            self.logger.debug(f"Audio cleanup failed in {self.audio_output_dir}: {exc}")
        try:
            if self.audio_output_dir:
                audio_dir = Path(self.audio_output_dir)
                if audio_dir.exists() and not any(audio_dir.iterdir()):
                    audio_dir.rmdir()
        except Exception as exc:
            self.logger.debug(f"Audio dir cleanup failed for {self.audio_output_dir}: {exc}")

        self.audio_recording_paths = []
        self.audio_output_dir = None

    def stop_all(self):
        if self.stopped:
            return
        self.stop_audio_recording()
        self.stopped = True
        self.used_audio_device_ids.clear()
        for cap in list(self.captures):
            cap.stop()
        try:
            # Глобальный stop может затронуть чужие потоки, используем аккуратно.
            sd.stop(ignore_errors=True)
        except TypeError:
            try:
                sd.stop()
            except Exception as e:
                self.logger.debug(f"Global audio stop failed: {e}")
        except Exception as e:
            self.logger.debug(f"Global audio stop failed: {e}")
        self.captures.clear()
        self.sources.clear()
        self.video_buffers.clear()
        self.audio_buffers.clear()
        self.source_audio_device_ids.clear()
