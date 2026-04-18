from core.events import event_bus
from models.domain import SwitchEvent
from typing import Optional
import cv2
import numpy as np


class ConsoleUI:
    def __init__(self):
        event_bus.subscribe(self.update)
        self.current_scene = "None"

    def update(self, data):
        if isinstance(data, SwitchEvent):
            print(f"\n[UI] SWITCH: Source {data.to_source_id} | Reason: {data.reason}")
            self.current_scene = str(data.to_source_id)
        elif isinstance(data, dict):
            print(f"\n[UI] STATUS: {data.get('status')}")

    def render_preview(self, sources, emulation_mode: bool = False):
        mode = "EMULATION" if emulation_mode else "REAL"
        print(f"\n--- PREVIEW [{mode}] ---")
        for src in sources:
            # Исправленная логика проверки активного источника
            is_active = False
            if self.current_scene.isdigit():
                is_active = (src.id == int(self.current_scene))

            active_marker = ">>" if is_active else "  "
            print(f"{active_marker} [{src.id}] {src.name} | {src.status.value} | Audio: {src.audio_level:.2f}")
        print("---------------")

    @staticmethod
    def _trim_black_bars(frame):
        if frame is None or frame.size == 0:
            return frame
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        row_mean = gray.mean(axis=1)

        threshold = 20.0
        max_crop = int(frame.shape[0] * 0.30)

        top = 0
        while top < max_crop and row_mean[top] < threshold:
            top += 1

        bottom = frame.shape[0] - 1
        min_bottom = frame.shape[0] - 1 - max_crop
        while bottom > min_bottom and row_mean[bottom] < threshold:
            bottom -= 1

        if top >= bottom:
            return frame
        return frame[top:bottom + 1, :]

    @staticmethod
    def _resize_cover(frame, target_w=1280, target_h=720):
        if frame is None or frame.size == 0:
            return frame
        h, w = frame.shape[:2]
        scale = max(target_w / w, target_h / h)
        resized = cv2.resize(frame, (int(w * scale), int(h * scale)))
        rh, rw = resized.shape[:2]
        x0 = max((rw - target_w) // 2, 0)
        y0 = max((rh - target_h) // 2, 0)
        return resized[y0:y0 + target_h, x0:x0 + target_w]

    def render_selected_camera(self, ingest, sources, selected_source_id: Optional[int], emulation_mode: bool):
        frame = None
        chosen_source_id = None

        candidate_ids = []
        if selected_source_id is not None:
            candidate_ids.append(selected_source_id)
        candidate_ids.extend([src.id for src in sources if src.status.value == "active"])
        candidate_ids.extend([src.id for src in sources])

        seen = set()
        unique_candidate_ids = []
        for source_id in candidate_ids:
            if source_id not in seen:
                seen.add(source_id)
                unique_candidate_ids.append(source_id)

        for source_id in unique_candidate_ids:
            source_index = source_id - 1
            current_frame = ingest.get_frame(source_index)
            if current_frame is not None:
                frame = current_frame
                chosen_source_id = source_id
                break

        if frame is None:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "No camera frame available", (20, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 180, 255), 2)
        else:
            frame = self._trim_black_bars(frame)
            frame = self._resize_cover(frame, target_w=1280, target_h=720)

        mode = "EMULATION" if emulation_mode else "REAL"
        src_label = chosen_source_id if chosen_source_id is not None else "N/A"
        cv2.putText(frame, f"Output Source: {src_label}", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        cv2.putText(frame, f"Mode: {mode}", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
        cv2.putText(frame, "Press Q or Esc to exit", (20, frame.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)
        cv2.imshow("Intellicut Output", frame)

        key = cv2.waitKey(1) & 0xFF
        return key not in (ord("q"), 27), frame

    def close_windows(self):
        cv2.destroyAllWindows()
