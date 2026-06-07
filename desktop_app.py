import json
import logging
import os
import re
import subprocess
import sys
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import cv2
import numpy as np

from config.settings import config_service
from core.events import event_bus
from core.facade import IntellicutFacade
from models.domain import SwitchEvent
from services.ingest import CameraCapture, MicrophoneCapture
from ui.console_ui import ConsoleUI
from utils.logger import logger_service


APP_BG = "#202124"
PANEL_BG = "#292b2f"
CARD_BG = "#303238"
CARD_ACTIVE = "#26372c"
TEXT = "#f1f3f4"
MUTED = "#a7adb5"
LINE = "#44474f"
GREEN = "#4ade80"
BLUE = "#60a5fa"
RED = "#f87171"
YELLOW = "#facc15"
DISABLED = "#656a73"
CARD_GAP = 12
CARD_SIDE_PAD = 28
CARD_VERTICAL_CHROME = 166
PREVIEW_ASPECT = 16 / 9


@dataclass
class CameraAssignment:
    role: int
    video_id: int
    audio_id: int | None
    name: str
    video_name: str = ""
    audio_name: str = ""


@dataclass(frozen=True)
class CameraGridLayout:
    cols: int
    rows: int
    card_size: tuple[int, int]
    preview_size: tuple[int, int]
    top_pad: int = 0
    gap: int = CARD_GAP


class UserSettings:
    def __init__(self):
        self.path = Path.home() / ".intellicut" / "settings.json"
        self.data = {
            "camera_roles": {},
            "camera_roles_configured": False,
            "camera_slot_count": config_service.default_camera_slots,
            "output_folder": str(Path(config_service.output_path).parent),
        }
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except Exception:
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")

    def output_folder(self) -> Path:
        return Path(self.data.get("output_folder") or "output")

    def set_output_folder(self, folder: str):
        self.data["output_folder"] = folder
        config_service.output_path = str(Path(folder) / Path(config_service.output_path).name)
        self.save()

    def camera_slot_count(self) -> int:
        try:
            configured = int(self.data.get("camera_slot_count", config_service.default_camera_slots))
        except Exception:
            configured = config_service.default_camera_slots
        count = max(config_service.default_camera_slots, configured)
        return min(config_service.max_sources, count)

    def set_camera_slot_count(self, count: int, save: bool = True):
        count = max(config_service.default_camera_slots, min(config_service.max_sources, int(count)))
        self.data["camera_slot_count"] = count
        if save:
            self.save()

    def assignments(self) -> list[CameraAssignment]:
        roles = self.data.get("camera_roles") or {}
        assignments = []

        for key, value in roles.items():
            if not isinstance(value, dict):
                continue

            try:
                role = int(str(key).split("_")[-1])
                audio_id = value.get("audio_id")

                assignments.append(
                    CameraAssignment(
                        role=role,
                        video_id=int(value["video_id"]),
                        audio_id=int(audio_id) if audio_id is not None else None,
                        name=value.get("name") or f"Camera {role}",
                        video_name=value.get("video_name") or "",
                        audio_name=value.get("audio_name") or "",
                    )
                )
            except Exception:
                continue

        return sorted(assignments, key=lambda item: item.role)

    def save_assignments(self, assignments: list[CameraAssignment]):
        roles = {}

        for assignment in assignments:
            roles[f"camera_{assignment.role}"] = {
                "video_id": assignment.video_id,
                "video_name": assignment.video_name,
                "audio_id": assignment.audio_id,
                "audio_name": assignment.audio_name,
                "name": assignment.name,
            }

        self.data["camera_roles"] = roles
        self.data["camera_roles_configured"] = True

        max_role = max((assignment.role for assignment in assignments), default=0)
        self.data["camera_slot_count"] = max(
            self.camera_slot_count(),
            max_role,
            config_service.default_camera_slots,
        )

        self.save()

class TkLogHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

    def emit(self, record):
        try:
            self.callback(self.format(record))
        except Exception:
            pass


class DebugLogsWindow(tk.Toplevel):
    def __init__(self, master, log_buffer: list[str]):
        super().__init__(master)
        self.title("Debug Logs")
        self.geometry("980x560")
        self.configure(bg=APP_BG)
        self.text = scrolledtext.ScrolledText(
            self,
            bg="#151619",
            fg=TEXT,
            insertbackground=TEXT,
            font=("Menlo", 11),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self.text.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        for line in log_buffer:
            self.append(line)

    def append(self, line: str):
        self.text.insert(tk.END, line + "\n")
        self.text.see(tk.END)


class StatusBadge(tk.Label):
    COLORS = {
        "Stopped": ("#3b3f46", TEXT),
        "Ready": ("#1f4d35", "#d9fbe4"),
        "Recording": ("#6b2f2f", "#ffe0e0"),
        "Processing": ("#5f4d1f", "#fff2ba"),
        "Done": ("#1f4d35", "#d9fbe4"),
        "Error": ("#71303a", "#ffe0e8"),
    }

    def __init__(self, master):
        super().__init__(master, text="Stopped", font=("Helvetica Neue", 12, "bold"), padx=14, pady=6)
        self.set("Stopped")

    def set(self, value: str):
        bg, fg = self.COLORS.get(value, self.COLORS["Stopped"])
        self.configure(text=value, bg=bg, fg=fg)


class CameraCard(tk.Frame):
    def __init__(self, master, role_index: int, role_change_callback, remove_callback):
        super().__init__(master, bg=CARD_BG, highlightthickness=1, highlightbackground=LINE)
        self.role_index = role_index
        self.role_change_callback = role_change_callback
        self.remove_callback = remove_callback
        self.photo = None
        self.source = None
        self._updating_role = False
        self.card_size = (408, 320)
        self.preview_size = (316, 178)
        self.configure(width=self.card_size[0], height=self.card_size[1])
        self.pack_propagate(False)

        self.header = tk.Frame(self, bg=CARD_BG)
        self.header.pack(fill=tk.X, padx=12, pady=(6, 4))

        self.role_label = tk.Label(self.header, text=f"Camera {role_index}", bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", 15, "bold"))
        self.role_label.pack(side=tk.LEFT)

        self.badge = tk.Label(self.header, text="", bg=CARD_BG, fg=GREEN, font=("Helvetica Neue", 10, "bold"))
        self.badge.pack(side=tk.LEFT, padx=(8, 0))

        self.remove_button = styled_button(self.header, "Remove", self._remove, bg="#4a2c2c", active="#633838")
        self.remove_button.pack(side=tk.RIGHT)

        self.preview_shell = tk.Frame(self, bg="#161719", width=self.preview_size[0], height=self.preview_size[1])
        self.preview_shell.pack(anchor="center", padx=12)
        self.preview_shell.pack_propagate(False)
        self.preview = tk.Label(self.preview_shell, bg="#161719", bd=0, highlightthickness=0)
        self.preview.place(relx=0.5, rely=0.5, anchor="center")

        self.name_label = tk.Label(self, text="Name: -", bg=CARD_BG, fg=TEXT, anchor="w", font=("Helvetica Neue", 11))
        self.name_label.pack(fill=tk.X, padx=12, pady=(6, 0))

        role_row = tk.Frame(self, bg=CARD_BG)
        role_row.pack(fill=tk.X, padx=12, pady=(4, 0))
        self.role_row = role_row
        tk.Label(role_row, text="Role", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", 10)).pack(side=tk.LEFT)
        self.role_var = tk.StringVar(value=f"Camera {role_index}")
        self.role_dropdown = dark_option_menu(
            role_row,
            self.role_var,
            [f"Camera {i}" for i in range(1, config_service.max_sources + 1)] + ["Do not use"],
            self._on_role_changed,
        )
        self.role_dropdown.pack(side=tk.RIGHT)

        self.audio_text = tk.Label(self, text="Audio level: 0%", bg=CARD_BG, fg=MUTED, anchor="w", font=("Helvetica Neue", 10))
        self.audio_text.pack(fill=tk.X, padx=12, pady=(4, 2))

        self.meter = ttk.Progressbar(self, maximum=100, mode="determinate")
        self.meter.configure(style="Audio.Horizontal.TProgressbar")
        self.meter.pack(fill=tk.X, padx=12, pady=(0, 6))

    def set_layout(self, card_size, preview_size, can_remove: bool):
        self.card_size = card_size
        self.preview_size = preview_size
        self.configure(width=card_size[0], height=card_size[1])
        self.preview_shell.configure(width=preview_size[0], height=preview_size[1])
        self.remove_button.configure(state=tk.NORMAL if can_remove else tk.DISABLED)

    def _remove(self):
        self.remove_callback(self.role_index)

    def _on_role_changed(self, event=None):
        del event
        if self._updating_role or self.source is None:
            return
        self.role_change_callback(self.source, self.role_var.get())

    def update_card(self, source, frame, display_name: str, device_name: str, is_active: bool, status_text: str = ""):
        self.source = source
        bg = CARD_ACTIVE if is_active else CARD_BG
        border = GREEN if is_active else LINE
        self.configure(bg=bg, highlightbackground=border, highlightthickness=2 if is_active else 1)
        for child in (self.header, self.role_label, self.badge, self.name_label, self.role_row, self.audio_text):
            child.configure(bg=bg)
        for child in self.role_row.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(bg=bg)

        role_text = source.name if source else f"Camera {self.role_index}"
        self.role_label.configure(text=role_text)
        self._updating_role = True
        allowed_roles = {f"Camera {i}" for i in range(1, config_service.max_sources + 1)}
        self.role_var.set(role_text if role_text in allowed_roles else f"Camera {self.role_index}")
        self._updating_role = False
        self.badge.configure(text="Active" if is_active else "")
        self.name_label.configure(text=f"Name: {display_name or role_text}")

        level = int((source.audio_level if source else 0.0) * 100)
        self.audio_text.configure(text=f"Audio level: {level}%")
        self.meter["value"] = level

        preview_w, preview_h = self._current_preview_size()
        if frame is None:
            frame = np.zeros((preview_h, preview_w, 3), dtype=np.uint8)
            message = status_text or "Camera unavailable"
            cv2.putText(frame, message, (18, preview_h // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (120, 160, 255), 2, cv2.LINE_AA)
        else:
            frame = self._resize_contain(frame, preview_w, preview_h)

        self.photo = frame_to_photo(frame)
        self.preview.configure(image=self.photo)

    def _current_preview_size(self):
        return self.preview_size

    @staticmethod
    def _resize_contain(frame, width: int, height: int):
        if frame is None or frame.size == 0:
            return frame
        src_h, src_w = frame.shape[:2]
        scale = min(width / src_w, height / src_h)
        target_w = max(1, int(round(src_w * scale)))
        target_h = max(1, int(round(src_h * scale)))
        interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        resized = cv2.resize(frame, (target_w, target_h), interpolation=interpolation)
        canvas = np.zeros((height, width, 3), dtype=frame.dtype)
        x0 = (width - target_w) // 2
        y0 = (height - target_h) // 2
        canvas[y0:y0 + target_h, x0:x0 + target_w] = resized
        return canvas


class OutputPanel(tk.Frame):
    def __init__(self, master, choose_callback, open_file_callback, open_folder_callback):
        super().__init__(master, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
        self.choose_callback = choose_callback
        self.open_file_callback = open_file_callback
        self.open_folder_callback = open_folder_callback

        tk.Label(self, text="Output", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", 16, "bold")).pack(anchor="w", padx=16, pady=(14, 8))

        self.folder_var = tk.StringVar(value="-")
        self.file_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="Not recorded yet")

        self._row("Folder", self.folder_var)
        self._row("Latest file", self.file_var)
        self._row("Status", self.status_var)

        buttons = tk.Frame(self, bg=PANEL_BG)
        buttons.pack(fill=tk.X, padx=16, pady=(12, 16))
        self.choose_button = styled_button(buttons, "Choose folder", choose_callback)
        self.choose_button.pack(side=tk.LEFT, padx=(0, 8))
        self.open_file_button = styled_button(buttons, "Open file", open_file_callback)
        self.open_file_button.pack(side=tk.LEFT, padx=(0, 8))
        self.open_folder_button = styled_button(buttons, "Open folder", open_folder_callback)
        self.open_folder_button.pack(side=tk.LEFT)

    def _row(self, label: str, variable: tk.StringVar):
        tk.Label(self, text=label, bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", 10)).pack(anchor="w", padx=16, pady=(8, 0))
        tk.Label(self, textvariable=variable, bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", 11), wraplength=320, justify=tk.LEFT).pack(anchor="w", padx=16, pady=(2, 0))

    def update_output(self, folder: Path, latest_file: str | None, status: str):
        self.folder_var.set(str(folder))
        self.file_var.set(Path(latest_file).name if latest_file else "-")
        self.status_var.set(status)
        state = tk.NORMAL if latest_file and Path(latest_file).exists() else tk.DISABLED
        self.open_file_button.configure(state=state)


class ControlPanel(tk.Frame):
    def __init__(self, master, callbacks):
        super().__init__(master, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
        tk.Label(self, text="Controls", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", 16, "bold")).pack(anchor="w", padx=16, pady=(14, 10))

        self.start_button = styled_button(self, "Start", callbacks["start"], bg="#2f6f4e", active="#3b875f")
        self.start_button.pack(fill=tk.X, padx=16, pady=5)

        self.stop_button = styled_button(self, "Stop", callbacks["stop"], bg="#7f3434", active="#9a4141")
        self.stop_button.pack(fill=tk.X, padx=16, pady=5)

        self.setup_button = styled_button(self, "Setup cameras", callbacks["setup"])
        self.setup_button.pack(fill=tk.X, padx=16, pady=5)

        self.add_camera_button = styled_button(self, "Add camera", callbacks["add_camera"])
        self.add_camera_button.pack(fill=tk.X, padx=16, pady=5)

        self.auto_button = styled_button(self, "Reset default devices", callbacks["auto_assign"])
        self.auto_button.pack(fill=tk.X, padx=16, pady=5)

        self.debug_button = styled_button(self, "Show logs", callbacks["logs"])
        self.debug_button.pack(fill=tk.X, padx=16, pady=(5, 16))

    def set_recording(self, recording: bool):
        self.start_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if recording else tk.DISABLED)
        self.setup_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.add_camera_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.auto_button.configure(state=tk.DISABLED if recording else tk.NORMAL)


class SetupCameraCard(tk.Frame):
    def __init__(self, master, device, roles):
        super().__init__(master, bg=CARD_BG, highlightthickness=1, highlightbackground=LINE)
        self.device = device
        self.photo = None

        self.title = tk.Label(self, text=device["device_label"], bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", 13, "bold"))
        self.title.pack(anchor="w", padx=14, pady=(12, 6))

        self.preview = tk.Label(self, bg="#17181b", height=170)
        self.preview.pack(fill=tk.BOTH, padx=14, pady=(0, 8))

        tk.Label(self, text="Name", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", 10)).pack(anchor="w", padx=14)
        self.name_var = tk.StringVar(value=device["name"])
        tk.Entry(self, textvariable=self.name_var, bg="#24262b", fg=TEXT, insertbackground=TEXT, relief=tk.FLAT).pack(fill=tk.X, padx=14, pady=(2, 8))

        tk.Label(self, text="Microphone", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", 10)).pack(anchor="w", padx=14)
        self.audio_choice_to_id = dict(device["audio_choice_to_id"])
        self.audio_device_var = tk.StringVar(value=device["audio_label"])
        dark_option_menu(
            self,
            self.audio_device_var,
            device["audio_choice_labels"],
            self._on_audio_changed,
        ).pack(fill=tk.X, padx=14, pady=(2, 8))

        self.audio_var = tk.StringVar(value="Audio level: 0%")
        tk.Label(self, textvariable=self.audio_var, bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", 10)).pack(anchor="w", padx=14)
        self.meter = ttk.Progressbar(self, maximum=100, mode="determinate")
        self.meter.configure(style="Audio.Horizontal.TProgressbar")
        self.meter.pack(fill=tk.X, padx=14, pady=(4, 10))

        self.role_var = tk.StringVar(value=device["role_label"])
        dark_option_menu(self, self.role_var, roles).pack(fill=tk.X, padx=14, pady=(0, 10))

    def _on_audio_changed(self, label: str):
        audio_id = self.audio_choice_to_id.get(label)
        self.device["set_audio_id"](audio_id)

    def reset_to_default(self):
        self.role_var.set(self.device["default_role_label"])
        self.name_var.set(self.device["default_name"])
        self.device["set_audio_id"](self.device["default_audio_id"])
        self.audio_device_var.set(self.device["audio_label"])

    def update_preview(self):
        frame = self.device["get_frame"]()

        target_w = 300
        target_h = 190

        if frame is None:
            frame = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            cv2.putText(
                frame,
                "Unavailable",
                (48, 100),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (120, 160, 255),
                2,
                cv2.LINE_AA,
            )
        else:
            # В setup не используем resize_cover и trim_black_bars,
            # чтобы preview не прыгал и не выглядел как движущаяся камера.
            frame = CameraCard._resize_contain(frame, target_w, target_h)

        self.photo = frame_to_photo(frame)
        self.preview.configure(image=self.photo)

        level = int(self.device["get_audio_level"]() * 100)
        self.audio_var.set(f"Audio level: {level}%")
        self.meter["value"] = level

    def assignment(self):
        role_label = self.role_var.get()

        if role_label == "Do not use":
            return None

        role = int(role_label.split()[-1])
        audio_id = self.device.get("audio_id")
        audio_names = self.device.get("audio_names", {})

        return CameraAssignment(
            role=role,
            video_id=self.device["video_id"],
            audio_id=audio_id,
            name=self.name_var.get().strip() or role_label,
            video_name=self.device.get("video_name") or self.device.get("device_label", ""),
            audio_name=audio_names.get(audio_id, "") if audio_id is not None else "",
        )


class CameraSetupDialog(tk.Toplevel):
    def __init__(self, master, devices, max_sources: int):
        super().__init__(master)
        self.title("Setup cameras")
        self.geometry("1160x720")
        self.configure(bg=APP_BG)
        self.result = None
        self.cards: list[SetupCameraCard] = []
        self.devices = devices

        tk.Label(self, text="Setup cameras", bg=APP_BG, fg=TEXT, font=("Helvetica Neue", 24, "bold")).pack(anchor="w", padx=22, pady=(18, 4))
        tk.Label(self, text=f"Assign devices to camera slots. Up to {config_service.max_sources} cameras are supported.", bg=APP_BG, fg=MUTED, font=("Helvetica Neue", 12)).pack(anchor="w", padx=22)

        body = tk.Frame(self, bg=APP_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)

        roles = [f"Camera {i}" for i in range(1, max_sources + 1)] + ["Do not use"]
        if not devices:
            empty = tk.Frame(body, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
            empty.pack(fill=tk.BOTH, expand=True)
            tk.Label(empty, text="No cameras found", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", 18, "bold")).pack(pady=(120, 8))
            tk.Label(empty, text="Check camera permissions or connect a device.", bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", 12)).pack()
        else:
            setup_cols = min(3, max(1, len(devices)))
            for idx, device in enumerate(devices):
                card = SetupCameraCard(body, device, roles)
                row = idx // setup_cols
                col = idx % setup_cols
                card.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 12, 0), pady=(0 if row == 0 else 12, 0))
                self.cards.append(card)
            for col in range(setup_cols):
                body.columnconfigure(col, weight=1, uniform="setup_cols")
            for row in range((len(devices) + setup_cols - 1) // setup_cols):
                body.rowconfigure(row, weight=1, uniform="setup_rows")

        footer = tk.Frame(self, bg=APP_BG)
        footer.pack(fill=tk.X, padx=22, pady=(0, 18))
        styled_button(footer, "Reset default devices", self.reset_default_devices).pack(side=tk.LEFT)
        styled_button(footer, "Cancel", self.cancel).pack(side=tk.RIGHT, padx=(8, 0))
        styled_button(footer, "Save", self.save, bg="#2f6f4e", active="#3b875f").pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(60, self._update_loop)
        self.grab_set()

    def _update_loop(self):
        if not self.winfo_exists():
            return
        for card in self.cards:
            if card.winfo_exists():
                card.update_preview()
        self.after(80, self._update_loop)

    def reset_default_devices(self):
        for card in self.cards:
            card.reset_to_default()

    def save(self):
        assignments = []
        used_roles = set()
        for card in self.cards:
            assignment = card.assignment()
            if assignment is None:
                continue
            if assignment.role in used_roles:
                messagebox.showerror("Duplicate role", f"Camera {assignment.role} is assigned more than once.")
                return
            used_roles.add(assignment.role)
            assignments.append(assignment)
        self.result = sorted(assignments, key=lambda item: item.role)
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()


class DashboardWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("IntelliCut")
        self.root.geometry("1280x760")
        self.root.minsize(1120, 680)
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
        tk.Label(header, text="IntelliCut", bg=APP_BG, fg=TEXT, font=("Helvetica Neue", 30, "bold")).pack(side=tk.LEFT)
        self.status_badge = StatusBadge(header)
        self.status_badge.pack(side=tk.RIGHT)

        content = tk.Frame(self.root, bg=APP_BG)
        content.pack(fill=tk.BOTH, expand=True, padx=22, pady=(0, 22))
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=0)
        content.rowconfigure(0, weight=1)

        self.camera_grid = tk.Frame(content, bg=APP_BG)
        self.camera_grid.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        for idx in range(config_service.max_sources):
            self.camera_grid.columnconfigure(idx % 3, weight=1)
            card = CameraCard(self.camera_grid, idx + 1, self.change_card_role, self.remove_camera_slot)
            self.camera_cards.append(card)

        side = tk.Frame(content, bg=APP_BG, width=360)
        side.grid(row=0, column=1, sticky="nsew")
        side.grid_propagate(False)

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
        self.control_panel.pack(fill=tk.X, pady=(0, 16))

        self.output_panel = OutputPanel(side, self.choose_output_folder, self.open_output_file, self.open_output_folder)
        self.output_panel.pack(fill=tk.X)

        self.empty_panel = tk.Frame(self.camera_grid, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
        tk.Label(self.empty_panel, text="No cameras found", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", 18, "bold")).pack(padx=42, pady=(34, 8))
        tk.Label(self.empty_panel, text="Check permissions or connect a camera.", bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", 12)).pack(padx=42, pady=(0, 18))
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

        if not assignments and not self.settings.data.get("camera_roles_configured"):
            assignments = self._default_assignments(slot_count)
            self.settings.save_assignments(assignments)
        else:
            completed = self._fill_missing_slot_assignments(assignments, slot_count)
            if completed != assignments:
                assignments = completed
                self.settings.save_assignments(assignments)

        source_names = [f"Camera {item.role}" for item in assignments]
        video_ids = [item.video_id for item in assignments]
        audio_ids = [item.audio_id for item in assignments]
        self.display_names = {idx + 1: item.name for idx, item in enumerate(assignments)}
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

    def _available_video_ids(self):
        excluded = [
            str(item).lower()
            for item in getattr(config_service, "excluded_video_name_parts", [])
        ]
        result = []
        for video_id in self.system.ingest.discovered_video_devices:
            name = self._camera_device_name(video_id).lower()
            if any(part in name for part in excluded):
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
        for row in range(2):
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
        cols = 2 if slot_count <= 4 else 3
        rows = max(1, (slot_count + cols - 1) // cols)
        grid_w = self.camera_grid.winfo_width()
        grid_h = self.camera_grid.winfo_height()
        if grid_w < 300:
            grid_w = 858
        if grid_h < 300:
            grid_h = 660

        target_card_w = 408 if cols == 2 else 270
        target_card_h = 390 if slot_count == 2 else 324 if cols == 2 else 316
        top_pad = 48 if slot_count == 2 else 0

        max_card_w = max(220, int((grid_w - CARD_GAP * (cols - 1)) / cols))
        max_card_h = max(240, int((grid_h - top_pad - CARD_GAP * (rows - 1)) / rows))
        card_w = min(target_card_w, max_card_w)
        card_h = min(target_card_h, max_card_h)

        preview_max_w = max(160, card_w - CARD_SIDE_PAD)
        preview_max_h = max(90, card_h - CARD_VERTICAL_CHROME)
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
        if slot_count == 3 and idx == 2:
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
        saved = {item.video_id: item for item in self._resolved_settings_assignments()}
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
            open_path(Path(self.latest_output_file))

    def open_output_folder(self):
        folder = self.settings.output_folder()
        folder.mkdir(parents=True, exist_ok=True)
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


class FlatButton(tk.Frame):
    def __init__(self, master, text, command, bg="#3a3d44", active="#4b4f58"):
        super().__init__(master, bg=bg, highlightthickness=1, highlightbackground="#50545e", cursor="hand2")
        self.command = command
        self.normal_bg = bg
        self.active_bg = active
        self.disabled_bg = "#2a2c31"
        self.state = tk.NORMAL
        self.label = tk.Label(
            self,
            text=text,
            bg=bg,
            fg=TEXT,
            font=("Helvetica Neue", 11, "bold"),
            padx=12,
            pady=9,
            cursor="hand2",
        )
        self.label.pack(fill=tk.BOTH, expand=True)
        self._bind_clicks()

    def _bind_clicks(self):
        for widget in (self, self.label):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _click(self, event=None):
        del event
        if self.state == tk.DISABLED:
            return
        if self.command:
            self.command()

    def _enter(self, event=None):
        del event
        if self.state != tk.DISABLED:
            self._set_bg(self.active_bg)

    def _leave(self, event=None):
        del event
        if self.state != tk.DISABLED:
            self._set_bg(self.normal_bg)

    def _set_bg(self, color):
        super().configure(bg=color)
        self.label.configure(bg=color)

    def configure(self, cnf=None, **kwargs):
        cnf = cnf or {}
        kwargs.update(cnf)
        state = kwargs.pop("state", None)
        text = kwargs.pop("text", None)
        if text is not None:
            self.label.configure(text=text)
        if state is not None:
            self.state = state
            if state == tk.DISABLED:
                self._set_bg(self.disabled_bg)
                self.label.configure(fg=DISABLED, cursor="arrow")
                super().configure(cursor="arrow", highlightbackground="#3a3d44")
            else:
                self._set_bg(self.normal_bg)
                self.label.configure(fg=TEXT, cursor="hand2")
                super().configure(cursor="hand2", highlightbackground="#50545e")
        if kwargs:
            super().configure(**kwargs)

    config = configure


def styled_button(master, text, command, bg="#3a3d44", active="#4b4f58"):
    return FlatButton(master, text, command, bg=bg, active=active)


def dark_option_menu(master, variable, values, command=None):
    if values and not variable.get():
        variable.set(values[0])
    menu = tk.OptionMenu(master, variable, *values, command=command)
    menu.configure(
        bg="#24262b",
        fg=TEXT,
        activebackground="#343741",
        activeforeground=TEXT,
        disabledforeground=DISABLED,
        highlightthickness=1,
        highlightbackground=LINE,
        relief=tk.FLAT,
        borderwidth=0,
        padx=10,
        pady=5,
        font=("Helvetica Neue", 10, "bold"),
        indicatoron=False,
    )
    menu["menu"].configure(
        bg="#24262b",
        fg=TEXT,
        activebackground="#3a3d44",
        activeforeground=TEXT,
        borderwidth=0,
        font=("Helvetica Neue", 10),
    )
    return menu


def frame_to_photo(frame):
    if frame is None or frame.size == 0:
        frame = np.zeros((240, 360, 3), dtype=np.uint8)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    header = f"P6 {width} {height} 255\n".encode("ascii")
    return tk.PhotoImage(data=header + rgb.tobytes(), format="PPM")


def read_cap_frame(cap):
    if cap is None:
        return None
    ok, frame = cap.read()
    return frame if ok else None


def open_path(path: Path):
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Audio.Horizontal.TProgressbar",
        troughcolor="#1b1d21",
        background=GREEN,
        bordercolor="#1b1d21",
        lightcolor=GREEN,
        darkcolor=GREEN,
        thickness=10,
    )
    DashboardWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
