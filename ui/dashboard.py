import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import cv2

from config.settings import config_service
from core.events import event_bus
from core.facade import IntellicutFacade
from models.domain import SwitchEvent
from services.ingest import CameraCapture, MicrophoneCapture
from ui.camera_card import CameraCard
from ui.debug import DebugLogsWindow, TkLogHandler
from ui.models import CameraAssignment, CameraGridLayout, UserSettings
from ui.panels import ControlPanel, OutputPanel
from ui.setup_dialog import CameraSetupDialog
from ui.theme import (
    APP_BG,
    CARD_GAP,
    F_HEADING,
    F_STATUS,
    F_TITLE,
    PANEL_BG,
    PREVIEW_ASPECT,
    LINE,
    MUTED,
    TEXT,
)
from ui.widgets import styled_button, StatusBadge, read_cap_frame
from ui.console_ui import ConsoleUI
from utils.logger import logger_service


class DashboardWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("IntelliCut")
        self.root.geometry("1280x760")
        self.root.minsize(960, 640)
        self.root.configure(bg=APP_BG)

        self.settings = UserSettings()
        self.settings.set_output_folder(str(self.settings.output_folder()))
        self.system = IntellicutFacade()
        self.log_buffer: list[str] = []
        self.debug_window: DebugLogsWindow | None = None
        self.latest_output_file = None
        self.output_status = "Not recorded yet"
        self.camera_cards: list[CameraCard] = []
        self.display_names = {}
        self.device_names = {}
        self.source_id_by_role = {}
        self.setup_devices = []

        self._install_log_handler()
        event_bus.subscribe(self._handle_event)
        self._build_ui()
        self._configure_scene_from_settings()
        self._update_output_panel()
        self._set_status("Ready" if self.system.ingest.get_sources() else "Error")
        self.control_panel.set_recording(False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(40, self._tick_loop)

    def _build_ui(self):
        header = tk.Frame(self.root, bg=APP_BG)
        header.pack(fill=tk.X, padx=22, pady=(18, 12))
        tk.Label(header, text="IntelliCut", bg=APP_BG, fg=TEXT, font=("Helvetica Neue", F_TITLE, "bold")).pack(side=tk.LEFT)
        self.status_badge = StatusBadge(header)
        self.status_badge.pack(side=tk.RIGHT)

        content = tk.Frame(self.root, bg=APP_BG)
        content.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 22))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0, minsize=240)
        content.rowconfigure(0, weight=1)

        self.camera_grid = tk.Frame(content, bg=APP_BG)
        self.camera_grid.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        for idx in range(config_service.max_sources):
            self.camera_grid.columnconfigure(idx % 3, weight=1)
            card = CameraCard(self.camera_grid, idx + 1, self.change_card_role, self.remove_camera_slot)
            self.camera_cards.append(card)

        side = tk.Frame(content, bg=APP_BG)
        side.grid(row=0, column=1, sticky="nsew")

        self.control_panel = ControlPanel(
            side,
            {
                "start": self.start,
                "stop": self.stop,
                "setup": self.open_setup,
                "add_camera": self.add_camera_slot,
                "auto_assign": self.reset_default_devices,
                "logs": self.show_logs,
            },
        )
        self.control_panel.pack(fill=tk.X, pady=(0, 10))

        self.output_panel = OutputPanel(side, self.choose_output_folder, self.open_output_file, self.open_output_folder)
        self.output_panel.pack(fill=tk.X)

        self.empty_panel = tk.Frame(self.camera_grid, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
        tk.Label(self.empty_panel, text="No cameras found", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", F_HEADING, "bold")).pack(padx=42, pady=(34, 8))
        tk.Label(self.empty_panel, text="Check permissions or connect a camera.", bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", F_STATUS)).pack(padx=42, pady=(0, 18))
        styled_button(self.empty_panel, "Refresh devices", self.open_setup).pack(pady=(0, 34))

    def _install_log_handler(self):
        logger = logger_service.get_logger()
        handler = TkLogHandler(self._append_log)
        logger.addHandler(handler)

    def _append_log(self, line: str):
        self.log_buffer.append(line)
        self.log_buffer = self.log_buffer[-1000:]
        if self.debug_window and self.debug_window.winfo_exists():
            self.debug_window.append(line)

    def _handle_event(self, data):
        if isinstance(data, SwitchEvent):
            self._append_log(f"Switch: Source {data.from_source_id} -> {data.to_source_id} ({data.reason})")
        elif isinstance(data, dict):
            status = data.get("status")
            if status:
                self._append_log(f"Status: {status}")

    def _configure_scene_from_settings(self):
        self.system.ingest.reset_scene()
        self.system.scene_configured = False
        slot_count = self.settings.camera_slot_count()
        available_video_ids = set(self._available_video_ids())
        assignments = [
            item
            for item in self._resolved_settings_assignments()
            if item.video_id in available_video_ids and 1 <= item.role <= slot_count
        ]
        should_save_assignments = False

        if not assignments and not self.settings.data.get("camera_roles_configured"):
            assignments = self._default_assignments(slot_count)
            should_save_assignments = True
        else:
            completed = self._fill_missing_slot_assignments(assignments, slot_count)
            if completed != assignments:
                assignments = completed
                should_save_assignments = True

        normalized = self._ensure_unique_audio_assignments(assignments)
        if normalized != assignments:
            assignments = normalized
            should_save_assignments = True

        if should_save_assignments:
            self.settings.save_assignments(assignments)

        source_names = [f"Camera {item.role}" for item in assignments]
        video_ids = [item.video_id for item in assignments]
        audio_ids = [item.audio_id for item in assignments]
        self.display_names = {idx + 1: item.name for idx, item in enumerate(assignments)}
        self.device_names = {
            idx + 1: item.video_name or self._camera_device_name(item.video_id)
            for idx, item in enumerate(assignments)
        }
        self.source_id_by_role = {item.role: idx + 1 for idx, item in enumerate(assignments)}

        if source_names:
            self.system.setup_scene(source_names, reset=False, video_device_ids=video_ids, audio_device_ids=audio_ids)

    def _fill_missing_slot_assignments(self, assignments: list[CameraAssignment], slot_count: int):
        by_role = {assignment.role: assignment for assignment in assignments}
        used_video_ids = {assignment.video_id for assignment in assignments}
        for role in range(1, slot_count + 1):
            if role in by_role:
                continue
            assignment = self._default_assignment_for_role(role, used_video_ids)
            if assignment is None:
                continue
            by_role[role] = assignment
            used_video_ids.add(assignment.video_id)
        return sorted(by_role.values(), key=lambda item: item.role)

    def _ensure_unique_audio_assignments(self, assignments: list[CameraAssignment]) -> list[CameraAssignment]:
        ordered_assignments = sorted(assignments, key=lambda item: item.role)
        audio_names = self._audio_name_map()
        valid_audio_ids = set(audio_names)
        default_audio_map = self._default_audio_assignments([assignment.video_id for assignment in ordered_assignments])
        requested_audio_counts = {}
        for assignment in ordered_assignments:
            if assignment.audio_id is not None and assignment.audio_id in valid_audio_ids:
                requested_audio_counts[assignment.audio_id] = requested_audio_counts.get(assignment.audio_id, 0) + 1
        protected_audio_ids = {
            audio_id
            for audio_id, count in requested_audio_counts.items()
            if count == 1
        }
        used_audio_ids = set()
        normalized = []

        for assignment in ordered_assignments:
            audio_id = assignment.audio_id

            if audio_id is not None and audio_id not in valid_audio_ids:
                logger_service.get_logger().warning(
                    "Saved audio device [%s] for Camera %s is not available; disabling audio for this camera.",
                    audio_id,
                    assignment.role,
                )
                audio_id = None

            if audio_id is not None and audio_id in used_audio_ids:
                duplicate_audio_id = audio_id
                audio_id = self._replacement_audio_id(
                    assignment.video_id,
                    default_audio_map,
                    used_audio_ids,
                    valid_audio_ids,
                    protected_audio_ids,
                )
                logger_service.get_logger().warning(
                    "Saved audio device [%s] is already assigned; Camera %s will use [%s].",
                    duplicate_audio_id,
                    assignment.role,
                    audio_id if audio_id is not None else "no audio",
                )

            if audio_id is not None:
                used_audio_ids.add(audio_id)

            normalized.append(
                CameraAssignment(
                    role=assignment.role,
                    video_id=assignment.video_id,
                    audio_id=audio_id,
                    name=assignment.name,
                    video_name=assignment.video_name,
                    audio_name=audio_names.get(audio_id, "") if audio_id is not None else "",
                )
            )

        return normalized

    def _replacement_audio_id(self, video_id: int, default_audio_map, used_audio_ids, valid_audio_ids, protected_audio_ids):
        preferred = default_audio_map.get(video_id)
        if (
            preferred is not None
            and preferred in valid_audio_ids
            and preferred not in used_audio_ids
            and preferred not in protected_audio_ids
        ):
            return preferred
        for item in self.system.ingest.audio_input_devices:
            audio_id = item["index"]
            if audio_id in valid_audio_ids and audio_id not in used_audio_ids and audio_id not in protected_audio_ids:
                return audio_id
        return None

    def _available_video_ids(self):
        result = []
        for video_id in self.system.ingest.discovered_video_devices:
            name = self._camera_device_name(video_id)
            if config_service.is_excluded_video_name(name):
                continue
            result.append(video_id)
            if len(result) >= config_service.max_sources:
                break
        return result

    def _default_assignments(self, slot_count: int | None = None):
        slot_count = self.settings.camera_slot_count() if slot_count is None else slot_count
        video_ids = self._available_video_ids()[: min(slot_count, config_service.max_sources)]
        audio_map = self._default_audio_assignments(video_ids)
        assignments = []
        for idx, video_id in enumerate(video_ids, start=1):
            audio_id = audio_map.get(video_id)
            assignments.append(
                CameraAssignment(
                    role=idx,
                    video_id=video_id,
                    audio_id=audio_id,
                    name=f"Camera {idx}",
                    video_name=self._camera_device_name(video_id),
                    audio_name=self._audio_name_map().get(audio_id, "") if audio_id is not None else "",
                )
            )
        return assignments

    def _default_assignment_for_role(self, role: int, used_video_ids=None):
        used_video_ids = used_video_ids or set()
        video_ids = self._available_video_ids()
        preferred = video_ids[role - 1] if 0 <= role - 1 < len(video_ids) else None
        if preferred in used_video_ids:
            preferred = None
        video_id = preferred
        if video_id is None:
            video_id = next((item for item in video_ids if item not in used_video_ids), None)
        if video_id is None:
            return None
        audio_map = self._default_audio_assignments(video_ids)
        audio_id = audio_map.get(video_id)

        return CameraAssignment(
            role=role,
            video_id=video_id,
            audio_id=audio_id,
            name=f"Camera {role}",
            video_name=self._camera_device_name(video_id),
            audio_name=self._audio_name_map().get(audio_id, "") if audio_id is not None else "",
        )

    def _default_audio_assignments(self, video_ids):
        audio_devices = list(self.system.ingest.audio_input_devices)
        audio_queue = list(self.system.ingest.auto_audio_device_queue)
        assignments = {}
        used_audio_ids = set()

        for fallback_idx, video_id in enumerate(video_ids):
            camera_name = self._camera_device_name(video_id)
            audio_id = self._match_audio_device_by_name(camera_name, audio_devices, used_audio_ids)
            if audio_id is None:
                audio_id = self._fallback_audio_id(audio_queue, used_audio_ids, fallback_idx)
            assignments[video_id] = audio_id
            if audio_id is not None:
                used_audio_ids.add(audio_id)
        return assignments

    def _fallback_audio_id(self, audio_queue, used_audio_ids, preferred_idx: int):
        if preferred_idx < len(audio_queue) and audio_queue[preferred_idx] not in used_audio_ids:
            return audio_queue[preferred_idx]
        for audio_id in audio_queue:
            if audio_id not in used_audio_ids:
                return audio_id
        return None

    def _match_audio_device_by_name(self, camera_name: str, audio_devices, used_audio_ids):
        camera_norm = self._normalize_device_name(camera_name)
        camera_tokens = self._device_name_tokens(camera_norm)
        best_audio_id = None
        best_score = 0

        for audio_device in audio_devices:
            audio_id = audio_device["index"]
            if audio_id in used_audio_ids:
                continue
            audio_name = str(audio_device.get("name", ""))
            audio_norm = self._normalize_device_name(audio_name)
            audio_tokens = self._device_name_tokens(audio_norm)
            score = self._device_match_score(camera_norm, audio_norm, camera_tokens, audio_tokens)
            if score > best_score:
                best_score = score
                best_audio_id = audio_id

        return best_audio_id if best_score >= 20 else None

    @staticmethod
    def _normalize_device_name(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()

    @staticmethod
    def _device_name_tokens(normalized_name: str):
        stopwords = {
            "audio",
            "avfoundation",
            "camera",
            "default",
            "device",
            "external",
            "hd",
            "input",
            "microphone",
            "mic",
            "pro",
            "video",
        }
        return {
            token
            for token in normalized_name.split()
            if len(token) >= 3 and token not in stopwords and not token.isdigit()
        }

    def _device_match_score(self, camera_norm: str, audio_norm: str, camera_tokens, audio_tokens) -> int:
        score = 0
        if "iphone" in camera_tokens or "continuity" in camera_tokens:
            if "iphone" in audio_tokens:
                score += 100
            if "continuity" in audio_tokens:
                score += 70
        if "ipad" in camera_tokens:
            if "ipad" in audio_tokens:
                score += 100
            if "continuity" in audio_tokens:
                score += 60
        if camera_tokens.intersection({"macbook", "facetime", "builtin", "built"}):
            if audio_tokens.intersection({"macbook", "builtin", "built", "internal"}):
                score += 100
        overlap = camera_tokens & audio_tokens
        score += len(overlap) * 15
        if len(camera_norm) > 5 and camera_norm in audio_norm:
            score += 30
        if len(audio_norm) > 5 and audio_norm in camera_norm:
            score += 30
        return score

    def _audio_name_map(self):
        return {
            item["index"]: str(item.get("name", ""))
            for item in self.system.ingest.audio_input_devices
        }

    def _video_name_map(self):
        return {
            video_id: self._camera_device_name(video_id)
            for video_id in self.system.ingest.discovered_video_devices
        }

    @staticmethod
    def _is_bad_saved_name(name: str) -> bool:
        name = str(name or "").strip()
        if not name:
            return True
        lowered = name.lower()
        return (
                lowered.startswith("camera device ")
                or lowered.startswith("unknown audio")
                or set(name) <= {"?"}
        )

    def _resolve_device_id(self, saved_id, saved_name: str, current_map: dict[int, str]):
        """
        Сначала пробуем старый id.
        Если id больше не указывает на то же имя — ищем по имени.
        """
        try:
            saved_id = int(saved_id)
        except Exception:
            saved_id = None

        saved_name = str(saved_name or "").strip()

        if saved_id in current_map:
            current_name = str(current_map.get(saved_id, ""))

            # Если имени не было или имя совпало — индекс еще валиден.
            if self._is_bad_saved_name(saved_name):
                return saved_id

            if self._normalize_device_name(current_name) == self._normalize_device_name(saved_name):
                return saved_id

        # Если имя нормальное, ищем устройство по имени.
        if not self._is_bad_saved_name(saved_name):
            saved_norm = self._normalize_device_name(saved_name)

            for device_id, current_name in current_map.items():
                if self._normalize_device_name(current_name) == saved_norm:
                    return device_id

            for device_id, current_name in current_map.items():
                current_norm = self._normalize_device_name(current_name)
                if saved_norm and (saved_norm in current_norm or current_norm in saved_norm):
                    return device_id

        return saved_id if saved_id in current_map else None

    def _resolve_saved_assignment(self, assignment: CameraAssignment) -> CameraAssignment | None:
        video_names = self._video_name_map()
        audio_names = self._audio_name_map()

        video_id = self._resolve_device_id(
            assignment.video_id,
            assignment.video_name,
            video_names,
        )

        if video_id is None:
            return None

        audio_id = None
        if assignment.audio_id is not None:
            audio_id = self._resolve_device_id(
                assignment.audio_id,
                assignment.audio_name,
                audio_names,
            )

        return CameraAssignment(
            role=assignment.role,
            video_id=video_id,
            audio_id=audio_id,
            name=assignment.name,
            video_name=video_names.get(video_id, assignment.video_name or ""),
            audio_name=audio_names.get(audio_id, assignment.audio_name or "") if audio_id is not None else "",
        )

    def _resolved_settings_assignments(self) -> list[CameraAssignment]:
        resolved = []

        for assignment in self.settings.assignments():
            fixed = self._resolve_saved_assignment(assignment)
            if fixed is not None:
                resolved.append(fixed)

        return sorted(resolved, key=lambda item: item.role)

    def _camera_device_name(self, video_id: int):
        names = self._ffmpeg_video_names()
        return names.get(video_id) or f"Camera device {video_id + 1}"

    def _ffmpeg_video_names(self):
        if hasattr(self, "_cached_video_names"):
            return self._cached_video_names
        names = {}
        if sys.platform == "darwin":
            try:
                proc = subprocess.run(
                    ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                    cwd=str(Path(__file__).resolve().parent),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=4,
                )
                in_video = False
                for line in proc.stdout.splitlines():
                    if "AVFoundation video devices" in line:
                        in_video = True
                        continue
                    if "AVFoundation audio devices" in line:
                        in_video = False
                    if in_video and "] [" in line:
                        left = line.rsplit("[", 1)[-1]
                        idx, _, name = left.partition("]")
                        if idx.strip().isdigit():
                            names[int(idx.strip())] = name.strip()
            except Exception:
                pass
        self._cached_video_names = names
        return names

    def _tick_loop(self):
        try:
            if self.system.is_running:
                self.system.tick()
                self.system.record_frame()
            self._update_camera_cards()
            self._update_output_panel()
        except Exception as exc:
            self._set_status("Error")
            self._append_log(f"UI loop error: {exc}")
        self.root.after(40, self._tick_loop)

    def _selected_output_frame(self):
        sources = self.system.ingest.get_sources()
        selected_id = self.system.switching.current_source_id
        candidates = []
        if selected_id:
            candidates.append(selected_id)
        candidates.extend(source.id for source in sources if source.status.value == "active")

        seen = set()
        for source_id in candidates:
            if source_id in seen:
                continue
            seen.add(source_id)
            frame = self.system.ingest.get_frame(source_id - 1)
            if frame is not None:
                return ConsoleUI._resize_cover(ConsoleUI._trim_black_bars(frame), target_w=1280, target_h=720)
        return None

    def _update_camera_cards(self):
        sources = self.system.ingest.get_sources()
        self.empty_panel.place_forget()

        active_id = self.system.switching.current_source_id
        slot_count = self.settings.camera_slot_count()
        if not self.system.is_running:
            add_state = tk.NORMAL if slot_count < config_service.max_sources else tk.DISABLED
            self.control_panel.add_camera_button.configure(state=add_state)
        layout = self._camera_grid_layout(slot_count)
        can_remove = slot_count > config_service.default_camera_slots
        for col in range(3):
            self.camera_grid.columnconfigure(
                col,
                weight=1 if col < layout.cols else 0,
                minsize=layout.card_size[0] if col < layout.cols else 0,
                uniform="camera_cols" if col < layout.cols else "",
            )
        for row in range(3):
            self.camera_grid.rowconfigure(
                row,
                weight=0,
                minsize=layout.card_size[1] if row < layout.rows else 0,
                uniform="",
            )

        for idx, card in enumerate(self.camera_cards):
            if idx < slot_count:
                role = idx + 1
                source_id = self.source_id_by_role.get(role)
                source = sources[source_id - 1] if source_id is not None and 0 <= source_id - 1 < len(sources) else None
                frame = self.system.ingest.get_frame(source_id - 1) if source is not None else None
                if source is not None:
                    device_name = self.device_names.get(source.id, f"Camera device {idx + 1}")
                    display_name = self.display_names.get(source.id, source.name)
                    status = "" if source.status.value == "active" else "Camera unavailable"
                    is_active = source.id == active_id
                else:
                    device_name = "No camera assigned"
                    display_name = f"Camera {role}"
                    status = "No camera assigned"
                    is_active = False
                card.set_layout(layout.card_size, layout.preview_size, can_remove)
                card.update_card(source, frame, display_name, device_name, is_active, status)
                row, col, columnspan = self._camera_grid_position(idx, slot_count, layout.cols)
                padx = (layout.gap if col > 0 and columnspan == 1 else 0, 0)
                pady = (layout.top_pad if row == 0 else layout.gap, 0)
                card.grid(row=row, column=col, columnspan=columnspan, sticky="n", padx=padx, pady=pady)
            else:
                card.grid_remove()

    def _camera_grid_layout(self, slot_count: int) -> CameraGridLayout:
        grid_w = self.camera_grid.winfo_width()
        grid_h = self.camera_grid.winfo_height()
        if grid_w < 300:
            grid_w = 858
        if grid_h < 300:
            grid_h = 660

        # Adaptive column count based on slots and available width
        if slot_count <= 1:
            preferred_cols = 1
        elif slot_count == 2:
            preferred_cols = 2
        elif slot_count == 3:
            preferred_cols = 3
        elif slot_count == 4:
            preferred_cols = 2
        else:
            preferred_cols = 3

        max_cols_by_width = max(1, int((grid_w + CARD_GAP) / (220 + CARD_GAP)))
        cols = min(preferred_cols, max_cols_by_width)
        rows = max(1, (slot_count + cols - 1) // cols)
        top_pad = 48 if rows == 1 else 0

        card_w = max(220, min(1200, int((grid_w - CARD_GAP * (cols - 1)) / cols)))
        card_h = max(240, min(780, int((grid_h - top_pad - CARD_GAP * (rows - 1)) / rows)))

        # Scale side padding and vertical chrome proportionally
        side_pad = max(16, int(card_w * 0.06))
        vertical_chrome = max(120, min(166, int(card_h * 0.38)))

        preview_max_w = max(160, card_w - side_pad)
        preview_max_h = max(90, card_h - vertical_chrome)
        preview_w = min(preview_max_w, int(preview_max_h * PREVIEW_ASPECT))
        preview_h = max(90, int(round(preview_w / PREVIEW_ASPECT)))
        if preview_h > preview_max_h:
            preview_h = preview_max_h
            preview_w = int(round(preview_h * PREVIEW_ASPECT))

        return CameraGridLayout(
            cols=cols,
            rows=rows,
            card_size=(card_w, card_h),
            preview_size=(preview_w, preview_h),
            top_pad=top_pad,
        )

    @staticmethod
    def _camera_grid_position(idx: int, slot_count: int, cols: int):
        if slot_count == 3 and cols == 2 and idx == 2:
            return 1, 0, 2
        return idx // cols, idx % cols, 1

    def start(self):
        if self.system.is_running:
            return
        if not self.system.ingest.get_sources():
            self._set_status("Error")
            messagebox.showerror("No cameras found", "No cameras configured. Open Setup cameras and refresh devices.")
            return
        try:
            self.output_status = "Recording"
            self._set_status("Recording")
            self.control_panel.set_recording(True)
            self.system.start()
        except Exception as exc:
            self.output_status = "Error"
            self._set_status("Error")
            self.control_panel.set_recording(False)
            messagebox.showerror("Start failed", str(exc))

    def stop(self):
        if not self.system.is_running:
            return
        self.output_status = "Processing"
        self._set_status("Processing")
        self.control_panel.set_recording(False)
        self.root.after(50, self._stop_pipeline)

    def _stop_pipeline(self):
        try:
            self.system.stop()
            self.latest_output_file = self.system.render.last_output_path
            self.output_status = "Done" if self.latest_output_file else "Stopped"
            self._set_status("Done")
        except Exception as exc:
            self.output_status = "Error"
            self._set_status("Error")
            messagebox.showerror("Stop failed", str(exc))

    def open_setup(self):
        if self.system.is_running:
            return
        self.system.ingest.reset_scene()
        self.system.scene_configured = False
        devices = self._build_setup_devices()
        dialog = CameraSetupDialog(self.root, devices, config_service.max_sources)
        self.root.wait_window(dialog)
        self._close_setup_devices(devices)
        if dialog.result is not None:
            self.settings.save_assignments(dialog.result)
        self._configure_scene_from_settings()
        self._set_status("Ready" if self.system.ingest.get_sources() else "Error")

    def _build_setup_devices(self):
        audio_devices = self.system.ingest.audio_input_devices
        audio_names = {item["index"]: item["name"] for item in audio_devices}
        audio_choice_labels = ["No audio"] + [f"[{item['index']}] {item['name']}" for item in audio_devices]
        audio_choice_to_id = {"No audio": None}
        for item in audio_devices:
            audio_choice_to_id[f"[{item['index']}] {item['name']}"] = item["index"]
        saved_assignments = self._ensure_unique_audio_assignments(self._resolved_settings_assignments())
        saved = {item.video_id: item for item in saved_assignments}
        slot_count = self.settings.camera_slot_count()
        video_ids = self._available_video_ids()
        default_video_ids = self._available_video_ids()[:slot_count]
        default_role_by_video_id = {video_id: idx + 1 for idx, video_id in enumerate(default_video_ids)}
        default_audio_map = self._default_audio_assignments(video_ids)
        devices = []

        for idx, video_id in enumerate(video_ids):
            video_name = self._camera_device_name(video_id)
            saved_assignment = saved.get(video_id)
            default_role = default_role_by_video_id.get(video_id, 0)
            default_audio_id = default_audio_map.get(video_id)
            audio_id = saved_assignment.audio_id if saved_assignment else default_audio_id
            cap = self._open_temp_camera(video_id)
            mic = self._open_setup_mic(video_id, audio_id)
            role = saved_assignment.role if saved_assignment else default_role
            default_name = f"Camera {default_role}" if default_role else f"Camera {idx + 1}"
            name = saved_assignment.name if saved_assignment else default_name
            selected_audio_label = self._setup_audio_label(audio_id, audio_names)
            device_audio_choice_labels = list(audio_choice_labels)
            device_audio_choice_to_id = dict(audio_choice_to_id)
            if selected_audio_label not in device_audio_choice_to_id:
                device_audio_choice_labels.append(selected_audio_label)
                device_audio_choice_to_id[selected_audio_label] = audio_id
            device = {
                "video_id": video_id,
                "video_name": video_name,
                "audio_id": audio_id,
                "name": name,
                "role_label": f"Camera {role}" if role else "Do not use",
                "default_role_label": f"Camera {default_role}" if default_role else "Do not use",
                "default_name": default_name,
                "default_audio_id": default_audio_id,
                "device_label": self._camera_device_name(video_id),
                "audio_label": selected_audio_label,
                "audio_choice_labels": device_audio_choice_labels,
                "audio_choice_to_id": device_audio_choice_to_id,
                "audio_names": audio_names,
                "cap": cap,
                "mic": mic,
                "get_frame": lambda cap=cap: read_cap_frame(cap),
            }
            device["set_audio_id"] = lambda audio_id, device=device: self._set_setup_audio_device(device, audio_id)
            device["get_audio_level"] = lambda device=device: device["mic"].get_level() if device.get("mic") else 0.0
            devices.append(device)
        return devices

    def _setup_audio_label(self, audio_id, audio_names):
        if audio_id is None:
            return "No audio"
        return f"[{audio_id}] {audio_names.get(audio_id, 'Unknown audio')}"

    def _open_setup_mic(self, video_id, audio_id):
        if audio_id is None:
            return None
        mic = MicrophoneCapture(f"setup video {video_id}", audio_id)
        mic.start()
        return mic

    def _set_setup_audio_device(self, device, audio_id):
        old_mic = device.get("mic")
        if old_mic:
            old_mic.stop()
        device["audio_id"] = audio_id
        device["audio_label"] = self._setup_audio_label(audio_id, device["audio_names"])
        device["mic"] = self._open_setup_mic(device["video_id"], audio_id)

    def add_camera_slot(self):
        if self.system.is_running:
            return
        current_count = self.settings.camera_slot_count()
        if current_count >= config_service.max_sources:
            messagebox.showinfo("Camera limit", f"Maximum camera count is {config_service.max_sources}.")
            return

        new_count = current_count + 1
        assignments = self.settings.assignments()
        used_video_ids = {assignment.video_id for assignment in assignments}
        if not any(assignment.role == new_count for assignment in assignments):
            default_assignment = self._default_assignment_for_role(new_count, used_video_ids)
            if default_assignment is not None:
                assignments.append(default_assignment)

        self.settings.set_camera_slot_count(new_count, save=False)
        self.settings.save_assignments(sorted(assignments, key=lambda item: item.role))
        self._configure_scene_from_settings()
        self._set_status("Ready" if self.system.ingest.get_sources() else "Error")

    def remove_camera_slot(self, role_index: int):
        if self.system.is_running:
            return
        current_count = self.settings.camera_slot_count()
        if current_count <= config_service.default_camera_slots:
            return

        assignments = []
        for assignment in self.settings.assignments():
            if assignment.role == role_index:
                continue
            role = assignment.role - 1 if assignment.role > role_index else assignment.role
            assignments.append(
                CameraAssignment(
                    role=role,
                    video_id=assignment.video_id,
                    audio_id=assignment.audio_id,
                    name=f"Camera {role}" if assignment.name.startswith("Camera ") else assignment.name,
                    video_name=assignment.video_name,
                    audio_name=assignment.audio_name,
                )
            )

        self.settings.set_camera_slot_count(current_count - 1, save=False)
        self.settings.save_assignments(sorted(assignments, key=lambda item: item.role))
        self._configure_scene_from_settings()
        self._set_status("Ready" if self.system.ingest.get_sources() else "Error")

    def _close_setup_devices(self, devices):
        for device in devices:
            mic = device.get("mic")
            cap = device.get("cap")
            if mic:
                mic.stop()
            if cap:
                cap.release()

    @staticmethod
    def _open_temp_camera(video_id):
        for backend in CameraCapture._camera_backends():
            cap = cv2.VideoCapture(video_id, backend)
            if cap.isOpened():
                return cap
            cap.release()
        return None

    def reset_default_devices(self):
        if self.system.is_running:
            return
        assignments = self._default_assignments()
        if assignments:
            self.settings.save_assignments(assignments)
            self._configure_scene_from_settings()

    def change_card_role(self, source, role_label: str):
        if self.system.is_running:
            return
        selected_role = None if role_label == "Do not use" else int(role_label.split()[-1])
        captures = self.system.ingest.captures
        sources = self.system.ingest.get_sources()
        assignments = []
        for src, capture in zip(sources, captures):
            if src.id == source.id:
                if role_label == "Do not use":
                    continue
                role = selected_role
            else:
                try:
                    role = int(src.name.split()[-1])
                except Exception:
                    role = src.id
                if selected_role is not None and role == selected_role:
                    continue
            audio_id = capture.audio_capture.device_id

            assignments.append(
                CameraAssignment(
                    role=role,
                    video_id=capture.device_id,
                    audio_id=audio_id,
                    name=self.display_names.get(src.id, src.name),
                    video_name=self._camera_device_name(capture.device_id),
                    audio_name=self._audio_name_map().get(audio_id, "") if audio_id is not None else "",
                )
            )
        if selected_role is not None:
            self.settings.set_camera_slot_count(max(self.settings.camera_slot_count(), selected_role), save=False)
        self.settings.save_assignments(sorted(assignments, key=lambda item: item.role))
        self._configure_scene_from_settings()

    def choose_output_folder(self):
        folder = filedialog.askdirectory(initialdir=str(self.settings.output_folder()))
        if not folder:
            return
        self.settings.set_output_folder(folder)
        self._update_output_panel()

    def open_output_file(self):
        if self.latest_output_file and Path(self.latest_output_file).exists():
            from ui.widgets import open_path
            open_path(Path(self.latest_output_file))

    def open_output_folder(self):
        folder = self.settings.output_folder()
        folder.mkdir(parents=True, exist_ok=True)
        from ui.widgets import open_path
        open_path(folder)

    def show_logs(self):
        if self.debug_window and self.debug_window.winfo_exists():
            self.debug_window.lift()
            return
        self.debug_window = DebugLogsWindow(self.root, self.log_buffer)

    def _set_status(self, value: str):
        self.status_badge.set(value)

    def _update_output_panel(self):
        self.output_panel.update_output(self.settings.output_folder(), self.latest_output_file, self.output_status)

    def close(self):
        try:
            if self.system.is_running:
                self.system.stop()
        except Exception:
            pass
        try:
            self.system.ingest.stop_all()
        except Exception:
            pass
        self.root.destroy()
