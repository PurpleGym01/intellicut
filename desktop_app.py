import json
import logging
import os
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


@dataclass
class CameraAssignment:
    role: int
    video_id: int
    audio_id: int | None
    name: str


class UserSettings:
    def __init__(self):
        self.path = Path.home() / ".intellicut" / "settings.json"
        self.data = {
            "camera_roles": {},
            "camera_roles_configured": False,
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
                "audio_id": assignment.audio_id,
                "name": assignment.name,
            }
        self.data["camera_roles"] = roles
        self.data["camera_roles_configured"] = True
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
    def __init__(self, master, role_index: int, identify_callback, role_change_callback):
        super().__init__(master, bg=CARD_BG, highlightthickness=1, highlightbackground=LINE)
        self.role_index = role_index
        self.identify_callback = identify_callback
        self.role_change_callback = role_change_callback
        self.photo = None
        self.identify_until = 0.0
        self.source = None
        self._updating_role = False

        self.header = tk.Frame(self, bg=CARD_BG)
        self.header.pack(fill=tk.X, padx=14, pady=(12, 8))

        self.role_label = tk.Label(self.header, text=f"Camera {role_index}", bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", 15, "bold"))
        self.role_label.pack(side=tk.LEFT)

        self.badge = tk.Label(self.header, text="", bg=CARD_BG, fg=GREEN, font=("Helvetica Neue", 10, "bold"))
        self.badge.pack(side=tk.RIGHT)

        self.preview = tk.Label(self, bg="#161719", height=210)
        self.preview.pack(fill=tk.BOTH, expand=True, padx=14)

        self.name_label = tk.Label(self, text="Name: -", bg=CARD_BG, fg=TEXT, anchor="w", font=("Helvetica Neue", 11))
        self.name_label.pack(fill=tk.X, padx=14, pady=(10, 2))

        self.device_label = tk.Label(self, text="Device: -", bg=CARD_BG, fg=MUTED, anchor="w", font=("Helvetica Neue", 10))
        self.device_label.pack(fill=tk.X, padx=14, pady=2)

        role_row = tk.Frame(self, bg=CARD_BG)
        role_row.pack(fill=tk.X, padx=14, pady=(8, 2))
        self.role_row = role_row
        tk.Label(role_row, text="Role", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", 10)).pack(side=tk.LEFT)
        self.role_var = tk.StringVar(value=f"Camera {role_index}")
        self.role_dropdown = dark_option_menu(
            role_row,
            self.role_var,
            ["Camera 1", "Camera 2", "Camera 3", "Do not use"],
            self._on_role_changed,
        )
        self.role_dropdown.pack(side=tk.RIGHT)

        self.audio_text = tk.Label(self, text="Audio level: 0%", bg=CARD_BG, fg=MUTED, anchor="w", font=("Helvetica Neue", 10))
        self.audio_text.pack(fill=tk.X, padx=14, pady=(8, 4))

        self.meter = ttk.Progressbar(self, maximum=100, mode="determinate")
        self.meter.configure(style="Audio.Horizontal.TProgressbar")
        self.meter.pack(fill=tk.X, padx=14, pady=(0, 12))

        self.identify_button = styled_button(self, "Identify", self.identify)
        self.identify_button.pack(anchor="w", padx=14, pady=(0, 14))

    def identify(self):
        self.identify_until = time.time() + 3.0
        self.identify_callback(self.role_index)

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
        for child in (self.header, self.role_label, self.badge, self.name_label, self.device_label, self.role_row, self.audio_text):
            child.configure(bg=bg)
        for child in self.role_row.winfo_children():
            if isinstance(child, tk.Label):
                child.configure(bg=bg)

        role_text = source.name if source else f"Camera {self.role_index}"
        self.role_label.configure(text=role_text)
        self._updating_role = True
        self.role_var.set(role_text if role_text in ("Camera 1", "Camera 2", "Camera 3") else f"Camera {self.role_index}")
        self._updating_role = False
        self.badge.configure(text="Active speaker" if is_active else "")
        self.name_label.configure(text=f"Name: {display_name or role_text}")
        self.device_label.configure(text=f"Device: {device_name or 'Unavailable'}")

        level = int((source.audio_level if source else 0.0) * 100)
        self.audio_text.configure(text=f"Audio level: {level}%")
        self.meter["value"] = level

        if frame is None:
            frame = np.zeros((240, 360, 3), dtype=np.uint8)
            message = status_text or "Camera unavailable"
            cv2.putText(frame, message, (24, 118), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (120, 160, 255), 2, cv2.LINE_AA)
        else:
            frame = self._resize_cover(frame, 520, 292)

        if time.time() < self.identify_until:
            cv2.rectangle(frame, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1), (80, 220, 120), 8)
            cv2.putText(frame, role_text.upper(), (42, frame.shape[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 4, cv2.LINE_AA)

        self.photo = frame_to_photo(frame)
        self.preview.configure(image=self.photo)

    @staticmethod
    def _resize_cover(frame, width: int, height: int):
        frame = ConsoleUI._trim_black_bars(frame)
        return ConsoleUI._resize_cover(frame, target_w=width, target_h=height)


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

        self.auto_button = styled_button(self, "Auto assign by audio", callbacks["auto_assign"])
        self.auto_button.pack(fill=tk.X, padx=16, pady=5)

        self.debug_button = styled_button(self, "Show logs", callbacks["logs"])
        self.debug_button.pack(fill=tk.X, padx=16, pady=(5, 16))

    def set_recording(self, recording: bool):
        self.start_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if recording else tk.DISABLED)
        self.setup_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.auto_button.configure(state=tk.DISABLED if recording else tk.NORMAL)


class SetupCameraCard(tk.Frame):
    def __init__(self, master, device, roles, on_identify):
        super().__init__(master, bg=CARD_BG, highlightthickness=1, highlightbackground=LINE)
        self.device = device
        self.photo = None
        self.identify_until = 0
        self.on_identify = on_identify

        self.title = tk.Label(self, text=device["device_label"], bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", 13, "bold"))
        self.title.pack(anchor="w", padx=14, pady=(12, 6))

        self.preview = tk.Label(self, bg="#17181b", height=170)
        self.preview.pack(fill=tk.BOTH, padx=14, pady=(0, 8))

        tk.Label(self, text="Name", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", 10)).pack(anchor="w", padx=14)
        self.name_var = tk.StringVar(value=device["name"])
        tk.Entry(self, textvariable=self.name_var, bg="#24262b", fg=TEXT, insertbackground=TEXT, relief=tk.FLAT).pack(fill=tk.X, padx=14, pady=(2, 8))

        tk.Label(self, text=f"Audio: {device['audio_label']}", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", 10), wraplength=250, justify=tk.LEFT).pack(anchor="w", padx=14, pady=(0, 6))

        self.audio_var = tk.StringVar(value="Audio level: 0%")
        tk.Label(self, textvariable=self.audio_var, bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", 10)).pack(anchor="w", padx=14)
        self.meter = ttk.Progressbar(self, maximum=100, mode="determinate")
        self.meter.configure(style="Audio.Horizontal.TProgressbar")
        self.meter.pack(fill=tk.X, padx=14, pady=(4, 10))

        self.role_var = tk.StringVar(value=device["role_label"])
        dark_option_menu(self, self.role_var, roles).pack(fill=tk.X, padx=14, pady=(0, 10))

        styled_button(self, "Identify", self.identify).pack(anchor="w", padx=14, pady=(0, 14))

    def identify(self):
        self.identify_until = time.time() + 3
        self.on_identify(self.device)

    def update_preview(self):
        frame = self.device["get_frame"]()
        if frame is None:
            frame = np.zeros((210, 300, 3), dtype=np.uint8)
            cv2.putText(frame, "Unavailable", (48, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (120, 160, 255), 2, cv2.LINE_AA)
        else:
            frame = ConsoleUI._resize_cover(ConsoleUI._trim_black_bars(frame), target_w=300, target_h=190)

        if time.time() < self.identify_until:
            label = self.role_var.get().upper()
            cv2.rectangle(frame, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1), (80, 220, 120), 7)
            cv2.putText(frame, label, (30, frame.shape[0] // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 3, cv2.LINE_AA)

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
        return CameraAssignment(
            role=role,
            video_id=self.device["video_id"],
            audio_id=self.device["audio_id"],
            name=self.name_var.get().strip() or role_label,
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
        tk.Label(self, text="Assign each device to Camera 1, Camera 2, Camera 3, or do not use it.", bg=APP_BG, fg=MUTED, font=("Helvetica Neue", 12)).pack(anchor="w", padx=22)

        body = tk.Frame(self, bg=APP_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)

        roles = [f"Camera {i}" for i in range(1, max_sources + 1)] + ["Do not use"]
        if not devices:
            empty = tk.Frame(body, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
            empty.pack(fill=tk.BOTH, expand=True)
            tk.Label(empty, text="No cameras found", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", 18, "bold")).pack(pady=(120, 8))
            tk.Label(empty, text="Check camera permissions or connect a device.", bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", 12)).pack()
        else:
            for idx, device in enumerate(devices):
                card = SetupCameraCard(body, device, roles, self._identify)
                card.grid(row=0, column=idx, sticky="nsew", padx=(0, 14))
                body.columnconfigure(idx, weight=1)
                self.cards.append(card)

        footer = tk.Frame(self, bg=APP_BG)
        footer.pack(fill=tk.X, padx=22, pady=(0, 18))
        styled_button(footer, "Auto assign by audio level", self.auto_assign).pack(side=tk.LEFT)
        styled_button(footer, "Cancel", self.cancel).pack(side=tk.RIGHT, padx=(8, 0))
        styled_button(footer, "Save", self.save, bg="#2f6f4e", active="#3b875f").pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(60, self._update_loop)
        self.grab_set()

    def _identify(self, device):
        device["identify_until"] = time.time() + 3

    def _update_loop(self):
        if not self.winfo_exists():
            return
        for card in self.cards:
            if card.winfo_exists():
                card.update_preview()
        self.after(80, self._update_loop)

    def auto_assign(self):
        ordered = sorted(self.cards, key=lambda card: card.device["get_audio_level"](), reverse=True)
        for card in self.cards:
            card.role_var.set("Do not use")
        for idx, card in enumerate(ordered[:3], start=1):
            card.role_var.set(f"Camera {idx}")

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
            self.camera_grid.columnconfigure(idx, weight=1)
            card = CameraCard(self.camera_grid, idx + 1, self._identify_role, self.change_card_role)
            card.grid(row=0, column=idx, sticky="nsew", padx=(0 if idx == 0 else 10, 0))
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
                "auto_assign": self.auto_assign_by_audio,
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
        discovered = set(self.system.ingest.discovered_video_devices)
        assignments = [item for item in self.settings.assignments() if item.video_id in discovered]

        if not assignments and not self.settings.data.get("camera_roles_configured"):
            assignments = self._default_assignments()
            self.settings.save_assignments(assignments)

        source_names = [f"Camera {item.role}" for item in assignments]
        video_ids = [item.video_id for item in assignments]
        audio_ids = [item.audio_id for item in assignments]
        self.display_names = {idx + 1: item.name for idx, item in enumerate(assignments)}
        self.device_names = {idx + 1: self._camera_device_name(item.video_id) for idx, item in enumerate(assignments)}

        if source_names:
            self.system.setup_scene(source_names, reset=False, video_device_ids=video_ids, audio_device_ids=audio_ids)

    def _default_assignments(self):
        video_ids = self.system.ingest.discovered_video_devices[: config_service.max_sources]
        audio_ids = list(self.system.ingest.auto_audio_device_queue)
        assignments = []
        for idx, video_id in enumerate(video_ids, start=1):
            audio_id = audio_ids[idx - 1] if idx - 1 < len(audio_ids) else None
            assignments.append(CameraAssignment(role=idx, video_id=video_id, audio_id=audio_id, name=f"Camera {idx}"))
        return assignments

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
                frame = self._selected_output_frame()
                self.system.render.write_frame(frame)
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
        if not sources:
            self.empty_panel.place(relx=0.5, rely=0.45, anchor="center")
        else:
            self.empty_panel.place_forget()

        active_id = self.system.switching.current_source_id
        for idx, card in enumerate(self.camera_cards):
            if idx < len(sources):
                source = sources[idx]
                frame = self.system.ingest.get_frame(idx)
                device_name = self.device_names.get(source.id, f"Camera device {idx + 1}")
                display_name = self.display_names.get(source.id, source.name)
                status = "" if source.status.value == "active" else "Camera unavailable"
                card.update_card(source, frame, display_name, device_name, source.id == active_id, status)
                card.grid()
            else:
                card.grid_remove()

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
        audio_queue = list(self.system.ingest.auto_audio_device_queue)
        saved = {item.video_id: item for item in self.settings.assignments()}
        devices = []

        for idx, video_id in enumerate(self.system.ingest.discovered_video_devices[: config_service.max_sources]):
            saved_assignment = saved.get(video_id)
            audio_id = saved_assignment.audio_id if saved_assignment else (audio_queue[idx] if idx < len(audio_queue) else None)
            cap = self._open_temp_camera(video_id)
            mic = MicrophoneCapture(f"setup video {video_id}", audio_id)
            mic.start()
            role = saved_assignment.role if saved_assignment else idx + 1
            name = saved_assignment.name if saved_assignment else f"Camera {role}"
            devices.append(
                {
                    "video_id": video_id,
                    "audio_id": audio_id,
                    "name": name,
                    "role_label": f"Camera {role}" if role else "Do not use",
                    "device_label": self._camera_device_name(video_id),
                    "audio_label": audio_names.get(audio_id, "Audio unavailable") if audio_id is not None else "Audio unavailable",
                    "cap": cap,
                    "mic": mic,
                    "get_frame": lambda cap=cap: read_cap_frame(cap),
                    "get_audio_level": lambda mic=mic: mic.get_level() if mic else 0.0,
                }
            )
        return devices

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

    def auto_assign_by_audio(self):
        if self.system.is_running:
            return
        sources = self.system.ingest.get_sources()
        captures = self.system.ingest.captures
        ranked = sorted(
            zip(sources, captures),
            key=lambda pair: pair[0].audio_level,
            reverse=True,
        )
        assignments = []
        for role, (source, capture) in enumerate(ranked[: config_service.max_sources], start=1):
            assignments.append(
                CameraAssignment(
                    role=role,
                    video_id=capture.device_id,
                    audio_id=capture.audio_capture.device_id,
                    name=self.display_names.get(source.id, f"Camera {role}"),
                )
            )
        if assignments:
            self.settings.save_assignments(assignments)
            self._configure_scene_from_settings()

    def change_card_role(self, source, role_label: str):
        if self.system.is_running:
            return
        captures = self.system.ingest.captures
        sources = self.system.ingest.get_sources()
        assignments = []
        for src, capture in zip(sources, captures):
            if src.id == source.id:
                if role_label == "Do not use":
                    continue
                role = int(role_label.split()[-1])
            else:
                try:
                    role = int(src.name.split()[-1])
                except Exception:
                    role = src.id
                if role_label != "Do not use" and role == int(role_label.split()[-1]):
                    continue
            assignments.append(
                CameraAssignment(
                    role=role,
                    video_id=capture.device_id,
                    audio_id=capture.audio_capture.device_id,
                    name=self.display_names.get(src.id, src.name),
                )
            )
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

    def _identify_role(self, role_index: int):
        self._append_log(f"Identify Camera {role_index}")

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
