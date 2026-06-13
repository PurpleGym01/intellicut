import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np

from config.settings import config_service
from ui.theme import (
    CARD_ACTIVE,
    CARD_BG,
    F_BODY,
    F_CARD,
    F_SMALL,
    GREEN,
    LINE,
    MUTED,
    TEXT,
)
from ui.widgets import dark_option_menu, frame_to_photo, styled_button


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

        self.role_label = tk.Label(self.header, text=f"Camera {role_index}", bg=CARD_BG, fg=TEXT, font=("Helvetica Neue", F_CARD, "bold"))
        self.role_label.pack(side=tk.LEFT)

        self.badge = tk.Label(self.header, text="", bg=CARD_BG, fg=GREEN, font=("Helvetica Neue", F_SMALL, "bold"))
        self.badge.pack(side=tk.LEFT, padx=(8, 0))

        self.remove_button = styled_button(self.header, "Remove", self._remove, bg="#4a2c2c", active="#633838")
        self.remove_button.pack(side=tk.RIGHT)

        self.preview_shell = tk.Frame(self, bg="#161719", width=self.preview_size[0], height=self.preview_size[1])
        self.preview_shell.pack(anchor="center", padx=12)
        self.preview_shell.pack_propagate(False)
        self.preview = tk.Label(self.preview_shell, bg="#161719", bd=0, highlightthickness=0)
        self.preview.place(relx=0.5, rely=0.5, anchor="center")

        self.name_label = tk.Label(self, text="Name: -", bg=CARD_BG, fg=TEXT, anchor="w", font=("Helvetica Neue", F_BODY))
        self.name_label.pack(fill=tk.X, padx=12, pady=(6, 0))

        role_row = tk.Frame(self, bg=CARD_BG)
        role_row.pack(fill=tk.X, padx=12, pady=(4, 0))
        self.role_row = role_row
        tk.Label(role_row, text="Role", bg=CARD_BG, fg=MUTED, font=("Helvetica Neue", F_SMALL)).pack(side=tk.LEFT)
        self.role_var = tk.StringVar(value=f"Camera {role_index}")
        self.role_dropdown = dark_option_menu(
            role_row,
            self.role_var,
            [f"Camera {i}" for i in range(1, config_service.max_sources + 1)] + ["Do not use"],
            self._on_role_changed,
        )
        self.role_dropdown.pack(side=tk.RIGHT)

        self.audio_text = tk.Label(self, text="Audio level: 0%", bg=CARD_BG, fg=MUTED, anchor="w", font=("Helvetica Neue", F_SMALL))
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
