import sys
import tkinter as tk
from tkinter import ttk

from ui.dashboard import DashboardWindow
from ui.theme import GREEN, init_fonts


def main():
    # Enable DPI awareness on Windows for crisp rendering on HiDPI displays
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    root = tk.Tk()
    init_fonts(root)

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
