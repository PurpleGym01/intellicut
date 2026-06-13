import tkinter as tk
from pathlib import Path

from ui.theme import F_BODY, F_SMALL, MUTED, PANEL_BG, LINE, TEXT
from ui.widgets import styled_button


class OutputPanel(tk.Frame):
    def __init__(self, master, choose_callback, open_file_callback, open_folder_callback):
        super().__init__(master, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
        self.choose_callback = choose_callback
        self.open_file_callback = open_file_callback
        self.open_folder_callback = open_folder_callback

        tk.Label(self, text="Output", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", F_BODY, "bold")).pack(anchor="w", padx=12, pady=(10, 6))

        self.folder_var = tk.StringVar(value="-")
        self.file_var = tk.StringVar(value="-")
        self.status_var = tk.StringVar(value="Not recorded yet")

        self._row("Folder", self.folder_var)
        self._row("Latest file", self.file_var)
        self._row("Status", self.status_var)

        buttons = tk.Frame(self, bg=PANEL_BG)
        buttons.pack(fill=tk.X, padx=12, pady=(8, 10))
        self.choose_button = styled_button(buttons, "Choose folder", choose_callback)
        self.choose_button.pack(fill=tk.X, pady=(0, 4))
        self.open_file_button = styled_button(buttons, "Open file", open_file_callback)
        self.open_file_button.pack(fill=tk.X, pady=(0, 4))
        self.open_folder_button = styled_button(buttons, "Open folder", open_folder_callback)
        self.open_folder_button.pack(fill=tk.X)

    def _row(self, label: str, variable: tk.StringVar):
        tk.Label(self, text=label, bg=PANEL_BG, fg=MUTED, font=("Helvetica Neue", F_SMALL)).pack(anchor="w", padx=12, pady=(6, 0))
        tk.Label(self, textvariable=variable, bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", F_SMALL), wraplength=210, justify=tk.LEFT).pack(anchor="w", padx=12, pady=(1, 0))

    def update_output(self, folder: Path, latest_file: str | None, status: str):
        self.folder_var.set(str(folder))
        self.file_var.set(Path(latest_file).name if latest_file else "-")
        self.status_var.set(status)
        state = tk.NORMAL if latest_file and Path(latest_file).exists() else tk.DISABLED
        self.open_file_button.configure(state=state)


class ControlPanel(tk.Frame):
    def __init__(self, master, callbacks):
        super().__init__(master, bg=PANEL_BG, highlightthickness=1, highlightbackground=LINE)
        tk.Label(self, text="Controls", bg=PANEL_BG, fg=TEXT, font=("Helvetica Neue", F_BODY, "bold")).pack(anchor="w", padx=12, pady=(10, 8))

        self.start_button = styled_button(self, "Start", callbacks["start"], bg="#2f6f4e", active="#3b875f")
        self.start_button.pack(fill=tk.X, padx=12, pady=4)

        self.stop_button = styled_button(self, "Stop", callbacks["stop"], bg="#7f3434", active="#9a4141")
        self.stop_button.pack(fill=tk.X, padx=12, pady=4)

        self.setup_button = styled_button(self, "Setup cameras", callbacks["setup"])
        self.setup_button.pack(fill=tk.X, padx=12, pady=4)

        self.add_camera_button = styled_button(self, "Add camera", callbacks["add_camera"])
        self.add_camera_button.pack(fill=tk.X, padx=12, pady=4)

        self.auto_button = styled_button(self, "Reset default devices", callbacks["auto_assign"])
        self.auto_button.pack(fill=tk.X, padx=12, pady=4)

        self.debug_button = styled_button(self, "Show logs", callbacks["logs"])
        self.debug_button.pack(fill=tk.X, padx=12, pady=(4, 10))

    def set_recording(self, recording: bool):
        self.start_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if recording else tk.DISABLED)
        self.setup_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.add_camera_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
        self.auto_button.configure(state=tk.DISABLED if recording else tk.NORMAL)
