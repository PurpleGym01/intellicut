import cv2
import numpy as np
import sounddevice as sd
from models.domain import VideoSource, SourceType, SourceStatus
from utils.logger import logger_service
from typing import List, Optional
import threading
import time
import sys
from config.settings import config_service


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

    def __init__(self, device_id: int, source_name: str, audio_device_id: Optional[int] = None):
        self.device_id = device_id
        self.source_name = source_name
        self.cap = None
        self.frame = None
        self.running = False
        self.frame_lock = threading.Lock()
        self.available = False
        self.audio_capture = MicrophoneCapture(source_name=source_name, device_id=audio_device_id)
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

        if not self.cap.isOpened():
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
                with self.frame_lock:
                    self.frame = frame
            else:
                time.sleep(0.1)

    def get_frame(self):
        with self.frame_lock:
            return self.frame.copy() if self.frame is not None else None

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
    def __init__(self, source_name: str, device_id: Optional[int]):
        self.source_name = source_name
        self.device_id = device_id
        self.stream = None
        self.level = 0.0
        self.available = False
        self.lock = threading.Lock()
        self.logger = logger_service.get_logger()
        self.sample_rate = None
        self.channels = 1
        self.blocksize = 0
        self.latency = "high"

    def _audio_callback(self, indata, frames, time_info, status):
        del frames, time_info
        if status:
            self.logger.debug(f"Audio callback status for {self.source_name}: {status}")
        try:
            # Векторизованный RMS без лишних аллокаций.
            rms = float(np.sqrt(np.mean(indata * indata)))
        except Exception:
            rms = 0.0
        # Приводим к диапазону 0..1 и сглаживаем, чтобы не было резкой дерготни.
        normalized = min(1.0, rms * 12.0)
        with self.lock:
            self.level = 0.85 * self.level + 0.15 * normalized

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

    def get_level(self) -> float:
        with self.lock:
            return self.level

    def stop(self):
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


class IngestService:
    def __init__(self):
        self.sources: List[VideoSource] = []
        self.captures: List[CameraCapture] = []
        self.logger = logger_service.get_logger()
        self.emulation_mode = False
        self.audio_input_devices = self._list_audio_input_devices()
        self.used_audio_device_ids = set()
        self.auto_audio_device_queue = self._build_auto_audio_queue()
        self.discovered_video_devices = self.discover_video_devices(config_service.camera_scan_max_index)
        self.stopped = False

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
        queue = [dev["index"] for dev in self.audio_input_devices]
        if getattr(config_service, "swap_first_two_audio_sources", False) and len(queue) >= 2:
            queue[0], queue[1] = queue[1], queue[0]
            self.logger.warning(
                f"Audio auto-assignment swapped: first two inputs are now [{queue[0]}], [{queue[1]}]"
            )
        return queue

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

    def reserve_audio_device_for_source(self, source_name: str, camera_device_id: int) -> Optional[int]:
        hints = getattr(config_service, "source_audio_device_hints", {}) or {}
        hinted = hints.get(source_name)
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
        found = []
        for device_id in range(max_index + 1):
            cap = None
            try:
                for backend in CameraCapture._camera_backends():
                    cap = cv2.VideoCapture(device_id, backend)
                    if cap.isOpened():
                        found.append(device_id)
                        break
                    cap.release()
                    cap = None
            except Exception:
                pass
            finally:
                if cap is not None:
                    cap.release()

        if found:
            self.logger.info(
                f"Video devices detected in range 0..{max_index}: {', '.join(str(i) for i in found)}"
            )
        else:
            self.logger.warning(f"No video devices found in range 0..{max_index}")
        return found

    def add_source(self, name: str, device_id: int = 0) -> VideoSource:
        if len(self.sources) >= config_service.max_sources:
            raise ValueError(f"Max sources limit ({config_service.max_sources}) reached")

        source = SourceFactory.create_source(len(self.sources) + 1, name, "camera")
        audio_device_id = self.reserve_audio_device_for_source(name, camera_device_id=device_id)
        capture = CameraCapture(device_id, source_name=name, audio_device_id=audio_device_id)

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
                    f"Source mapping: {name} -> video[{device_id}] + audio[{audio_device_id}] {audio_name}"
                )

        self.captures.append(capture)
        self.sources.append(source)
        return source

    def get_sources(self) -> List[VideoSource]:
        for i, src in enumerate(self.sources):
            if i < len(self.captures):
                # Если камера работает - берем реальные данные, иначе - эмуляция уже записана
                if self.captures[i].available:
                    src.audio_level = self.captures[i].get_audio_level()
        return self.sources

    def get_frame(self, source_index: int):
        if 0 <= source_index < len(self.captures):
            return self.captures[source_index].get_frame()
        return None

    def update_audio_levels(self, levels: List[float]):
        """Fallback для эмуляции только неактивных источников."""
        if self.emulation_mode:
            for i, level in enumerate(levels):
                if i < len(self.sources):
                    if self.sources[i].status != SourceStatus.ACTIVE:
                        self.sources[i].audio_level = level

    def stop_all(self):
        if self.stopped:
            return
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
