import logging
import tkinter as tk
from tkinter import scrolledtext

from ui.theme import APP_BG, F_BODY, TEXT


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
            font=("Menlo", F_BODY),
            relief=tk.FLAT,
            wrap=tk.WORD,
        )
        self.text.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)
        for line in log_buffer:
            self.append(line)

    def append(self, line: str):
        self.text.insert(tk.END, line + "\n")
        self.text.see(tk.END)
