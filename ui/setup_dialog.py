import tkinter as tk
from tkinter import messagebox, ttk

import cv2
import numpy as np

from config.settings import config_service
from ui.camera_card import CameraCard
from ui.models import CameraAssignment
from ui.theme import (
    APP_BG,
    CARD_BG,
    F_DIALOG_TITLE,
    F_HEADING,
    F_SETUP_CARD,
    F_SMALL,
    F_STATUS,
    LINE,
    MUTED,
    PANEL_BG,
    PREVIEW_ASPECT,
    TEXT,
)
from ui.widgets import dark_option_menu, frame_to_photo, read_cap_frame, styled_button


class SetupCameraCard(tk.Frame):
    def __init__(self, master, device, roles):
        super().__init__(master, bg=CARD_BG, highlightthickness=1, highlightbackground=LINE)
        self.device = device
        self.photo = None

        self.title = tk.Label(self, text=device["device_label"], bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", F_SETUP_CARD, "bold"))
        self.title.pack(anchor="w", padx=14, pady=(10, 4))

        self.preview = tk.Label(self, bg="#17181b", height=170)
        self.preview.pack(fill=tk.BOTH, padx=14, pady=(0, 6))

        tk.Label(self, text="Name", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", F_SMALL)).pack(anchor="w", padx=14)
        self.name_var = tk.StringVar(value=device["name"])
        tk.Entry(self, textvariable=self.name_var, bg="#24262b", fg=TEXT, insertbackground=TEXT, relief=tk.FLAT).pack(fill=tk.X, padx=14, pady=(2, 6))

        tk.Label(self, text="Microphone", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", F_SMALL)).pack(anchor="w", padx=14)
        self.audio_choice_to_id = dict(device["audio_choice_to_id"])
        self.audio_device_var = tk.StringVar(value=device["audio_label"])
        dark_option_menu(
            self,
            self.audio_device_var,
            device["audio_choice_labels"],
            self._on_audio_changed,
        ).pack(fill=tk.X, padx=14, pady=(2, 6))

        self.audio_var = tk.StringVar(value="Audio level: 0%")
        tk.Label(self, textvariable=self.audio_var, bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", F_SMALL)).pack(anchor="w", padx=14)
        self.meter = ttk.Progressbar(self, maximum=100, mode="determinate")
        self.meter.configure(style="Audio.Horizontal.TProgressbar")
        self.meter.pack(fill=tk.X, padx=14, pady=(3, 6))

        self.role_var = tk.StringVar(value=device["role_label"])
        dark_option_menu(self, self.role_var, roles).pack(fill=tk.X, padx=14, pady=(0, 8))

    def _on_audio_changed(self, label: str):
        audio_id = self.audio_choice_to_id.get(label)
        self.device["set_audio_id"](audio_id)

    def reset_to_default(self):
        self.role_var.set(self.device["default_role_label"])
        self.name_var.set(self.device["default_name"])
        self.device["set_audio_id"](self.device["default_audio_id"])
        self.audio_device_var.set(self.device["audio_label"])

    def update_preview(self, target_w: int = 300, target_h: int = 190):
        frame = self.device["get_frame"]()

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
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        dialog_w = min(1160, screen_w - 80)
        dialog_h = min(720, screen_h - 80)
        self.geometry(f"{dialog_w}x{dialog_h}")
        self.minsize(640, 480)
        self.configure(bg=APP_BG)
        self.result = None
        self.cards: list[SetupCameraCard] = []
        self.devices = devices
        self.canvas = None
        self.scroll_frame = None
        self.canvas_window_id = None
        self.setup_cols = 1

        tk.Label(self, text="Setup cameras", bg=APP_BG, fg=TEXT, font=("Helvetica Neue", F_DIALOG_TITLE, "bold")).pack(anchor="w", padx=22, pady=(18, 4))
        tk.Label(self, text=f"Assign devices to camera slots. Up to {config_service.max_sources} cameras are supported.", bg=APP_BG, fg=MUTED, font=("Helvetica Neue", F_STATUS)).pack(anchor="w", padx=22)

        body = tk.Frame(self, bg=APP_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)

        roles = [f"Camera {i}" for i in range(1, max_sources + 1)] + ["Do not use"]
        if not devices:
            empty = tk.Frame(body, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
            empty.pack(fill=tk.BOTH, expand=True)
            tk.Label(empty, text="No cameras found", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", F_HEADING, "bold")).pack(pady=(120, 8))
            tk.Label(empty, text="Check camera permissions or connect a device.", bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", F_STATUS)).pack()
        else:
            self.canvas = tk.Canvas(body, bg=APP_BG, highlightthickness=0)
            scrollbar = tk.Scrollbar(body, orient=tk.VERTICAL, command=self.canvas.yview)
            self.scroll_frame = tk.Frame(self.canvas, bg=APP_BG)

            self.scroll_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
            self.canvas_window_id = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
            self.canvas.configure(yscrollcommand=scrollbar.set)
            self.canvas.bind("<Configure>", self._on_canvas_resize)

            self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

            self.setup_cols = min(3, max(1, len(devices)))
            for idx, device in enumerate(devices):
                card = SetupCameraCard(self.scroll_frame, device, roles)
                row = idx // self.setup_cols
                col = idx % self.setup_cols
                card.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 12, 0), pady=(0 if row == 0 else 12, 0))
                self.cards.append(card)
            for col in range(self.setup_cols):
                self.scroll_frame.columnconfigure(col, weight=1, uniform="setup_cols")
            for row in range((len(devices) + self.setup_cols - 1) // self.setup_cols):
                self.scroll_frame.rowconfigure(row, weight=1, uniform="setup_rows")

        footer = tk.Frame(self, bg=APP_BG)
        footer.pack(fill=tk.X, padx=22, pady=(0, 18))
        styled_button(footer, "Reset default devices", self.reset_default_devices).pack(side=tk.LEFT)
        styled_button(footer, "Cancel", self.cancel).pack(side=tk.RIGHT, padx=(8, 0))
        styled_button(footer, "Save", self.save, bg="#2f6f4e", active="#3b875f").pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.after(60, self._update_loop)
        self.grab_set()

    def _on_canvas_resize(self, event):
        if self.canvas_window_id is not None:
            self.canvas.itemconfig(self.canvas_window_id, width=event.width)

    def _update_loop(self):
        if not self.winfo_exists():
            return
        canvas_w = self.canvas.winfo_width() if self.canvas else 0
        if canvas_w < 100:
            canvas_w = 800
        cols = max(1, self.setup_cols)
        card_w = canvas_w // cols
        # Cap preview width so it doesn't stretch to full card width,
        # then derive height from 16:9 ratio
        preview_w = min(max(240, card_w - 28), 360)
        preview_h = int(preview_w / PREVIEW_ASPECT)
        for card in self.cards:
            if card.winfo_exists():
                card.update_preview(preview_w, preview_h)
        self.after(80, self._update_loop)

    def reset_default_devices(self):
        for card in self.cards:
            card.reset_to_default()

    def save(self):
        assignments = []
        used_roles = set()
        used_audio_ids = {}
        for card in self.cards:
            assignment = card.assignment()
            if assignment is None:
                continue
            if assignment.role in used_roles:
                messagebox.showerror("Duplicate role", f"Camera {assignment.role} is assigned more than once.")
                return
            if assignment.audio_id is not None:
                if assignment.audio_id in used_audio_ids:
                    first_role = used_audio_ids[assignment.audio_id]
                    microphone = assignment.audio_name or f"audio device {assignment.audio_id}"
                    messagebox.showerror(
                        "Duplicate microphone",
                        f"{microphone} is assigned to Camera {first_role} and Camera {assignment.role}. "
                        "Select a different microphone or choose No audio.",
                    )
                    return
                used_audio_ids[assignment.audio_id] = assignment.role
            used_roles.add(assignment.role)
            assignments.append(assignment)
        self.result = sorted(assignments, key=lambda item: item.role)
        self.destroy()

    def cancel(self):
        self.result = None
        self.destroy()
