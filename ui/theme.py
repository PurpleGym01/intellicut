# Layout constants
CARD_GAP = 12
CARD_SIDE_PAD = 28
CARD_VERTICAL_CHROME = 166
PREVIEW_ASPECT = 16 / 9

# Colors
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

# Font sizes (set once in init_fonts based on display scaling)
F_TITLE = 30
F_DIALOG_TITLE = 24
F_HEADING = 18
F_SECTION = 16
F_CARD = 15
F_SETUP_CARD = 13
F_STATUS = 12
F_BODY = 11
F_SMALL = 10


def init_fonts(root):
    """Scale font sizes based on the current display's DPI."""
    global F_TITLE, F_DIALOG_TITLE, F_HEADING, F_SECTION
    global F_CARD, F_SETUP_CARD, F_STATUS, F_BODY, F_SMALL
    try:
        tk_scale = float(root.tk.call("tk", "scaling"))
        factor = max(0.8, min(1.4, tk_scale / 1.33))
        F_TITLE = max(20, round(30 * factor))
        F_DIALOG_TITLE = max(16, round(24 * factor))
        F_HEADING = max(13, round(18 * factor))
        F_SECTION = max(12, round(16 * factor))
        F_CARD = max(11, round(15 * factor))
        F_SETUP_CARD = max(10, round(13 * factor))
        F_STATUS = max(9, round(12 * factor))
        F_BODY = max(9, round(11 * factor))
        F_SMALL = max(8, round(10 * factor))
    except Exception:
        pass
