# Maps SimVar names to display labels and panel assignments.
# panel: "left" or "right"
# fault_var: optional SimVar that, if True, forces state to "FAULT"

SIMVARS = {
    # --- Left panel: compact light strip (top row) ---
    "LIGHT_LANDING_LEFT":  {"label": "LDG L",  "panel": "left", "light_row": True},
    "LIGHT_LANDING_RIGHT": {"label": "LDG R",  "panel": "left", "light_row": True},
    "LIGHT_STROBE":        {"label": "STRB",   "panel": "left", "light_row": True},
    "LIGHT_NAV":           {"label": "NAV",    "panel": "left", "light_row": True},
    "LIGHT_BEACON":        {"label": "BCN",    "panel": "left", "light_row": True},
    "LIGHT_TAXI":          {"label": "TAXI",   "panel": "left", "light_row": True},

    # --- Left panel: systems grid (boolean tiles) ---
    "ENGINE_1_RUNNING": {"label": "ENG 1",       "panel": "left"},
    "ENGINE_2_RUNNING": {"label": "ENG 2",       "panel": "left"},
    "GEAR_DEPLOYED":    {"label": "GEAR",        "panel": "left"},
    "ANTI_ICE_ON":      {"label": "ANTI\nICE",   "panel": "left", "color": "blue"},
    "PITOT_HEAT":       {"label": "PITOT\nHEAT", "panel": "left", "color": "blue"},
    "PARKING_BRAKE":    {"label": "PARK\nBRK",   "panel": "left", "color": "amber"},

    # --- Left panel: position gauges ---
    "FLAPS_POSITION": {
        "label": "FLAPS", "panel": "left", "type": "gauge",
        "max": 5, "ticks": [0, 1, 2, 3, 4, 5],
        "color_a": "#00DD55", "color_b": "#FF6600",
    },
    "SPOILER_POSITION": {
        "label": "SPLR", "panel": "left", "type": "gauge",
        "max": 100, "ticks": [0, 25, 50, 75, 100], "unit": "%",
        "color_a": "#00CCBB", "color_b": "#CC44FF",
    },

    # --- Right panel: top-row priority annunciators (rendered as a 3-wide header row) ---
    "MASTER_WARNING": {
        "label": "MASTER\nWARN",
        "panel": "right",
        "severity": "warning",   # red flashing when active
        "top_row": True,
    },
    "MASTER_CAUTION": {
        "label": "MASTER\nCAUT",
        "panel": "right",
        "severity": "caution",   # amber when active
        "top_row": True,
    },
    "HYD_PRESSURE_WARNING": {
        "label": "HYD\nPRESS",
        "panel": "right",
        "severity": "warning",
        "top_row": True,
    },

    # --- Right panel: secondary annunciator grid ---
    "STALL_WARNING": {
        "label": "STALL",
        "panel": "right",
        "severity": "warning",
    },
    "OVERSPEED": {
        "label": "OVER\nSPEED",
        "panel": "right",
        "severity": "warning",
    },
    "OIL_PRESSURE_LOW": {
        "label": "OIL\nPRESS",
        "panel": "right",
        "severity": "caution",
    },
    "LOW_FUEL_L": {
        "label": "FUEL\nLOW L",
        "panel": "right",
        "severity": "caution",
    },
    "LOW_FUEL_R": {
        "label": "FUEL\nLOW R",
        "panel": "right",
        "severity": "caution",
    },
    "DOOR_OPEN": {
        "label": "DOOR\nOPEN",
        "panel": "right",
        "severity": "caution",
    },
}

# Display geometry (matches Waveshare 1.9" ST7789V2: 170x320 in landscape = 320x170)
DISPLAY_WIDTH = 320   # physical display height used as width in landscape
DISPLAY_HEIGHT = 170  # physical display width used as height in landscape

# Tkinter preview: two panels side by side with a gap
PANEL_GAP = 4         # pixels between the two simulated displays
WINDOW_WIDTH = DISPLAY_WIDTH * 2 + PANEL_GAP
WINDOW_HEIGHT = DISPLAY_HEIGHT

# WebSocket server
WS_HOST = "localhost"
WS_PORT = 8765

# Tile layout
TILE_PADDING = 6      # px inside each tile
FONT_LABEL_SIZE = 14
FONT_STATE_SIZE = 11
