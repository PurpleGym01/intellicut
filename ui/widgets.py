import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np

from ui.theme import (
    DISABLED,
    F_BODY,
    F_SMALL,
    F_STATUS,
    GREEN,
    LINE,
    TEXT,
)


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


def trim_black_bars(frame):
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


def resize_cover(frame, target_w=1280, target_h=720):
    if frame is None or frame.size == 0:
        return frame
    h, w = frame.shape[:2]
    scale = max(target_w / w, target_h / h)
    resized = cv2.resize(frame, (int(w * scale), int(h * scale)))
    rh, rw = resized.shape[:2]
    x0 = max((rw - target_w) // 2, 0)
    y0 = max((rh - target_h) // 2, 0)
    return resized[y0:y0 + target_h, x0:x0 + target_w]


def open_path(path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


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
            font=("Helvetica Neue", F_SMALL, "bold"),
            padx=10,
            pady=7,
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
        font=("Helvetica Neue", F_SMALL, "bold"),
        indicatoron=False,
    )
    menu["menu"].configure(
        bg="#24262b",
        fg=TEXT,
        activebackground="#3a3d44",
        activeforeground=TEXT,
        borderwidth=0,
        font=("Helvetica Neue", F_SMALL),
    )
    return menu


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
        super().__init__(master, text="Stopped", font=("Helvetica Neue", F_STATUS, "bold"), padx=14, pady=6)
        self.set("Stopped")

    def set(self, value: str):
        bg, fg = self.COLORS.get(value, self.COLORS["Stopped"])
        self.configure(text=value, bg=bg, fg=fg)
