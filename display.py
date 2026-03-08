"""
display.py — Bravo Display sim cockpit desktop preview

Architecture:
  - render_panel(data, panel_side, flash) → PIL.Image (320×170)
    Pure Pillow — no tkinter. Identical call on Pi: pass image to ST7789V2 driver.
  - DisplayApp (tkinter) glues two panels into a 644×170 window.
  - WebSocket client runs in a background thread (asyncio event loop).
    Data updates posted to tkinter main thread via root.after(0, ...).
"""

import asyncio
import json
import threading
import tkinter as tk

from PIL import Image, ImageDraw, ImageFont, ImageTk
from websockets.asyncio.client import connect

from config import (
    DISPLAY_HEIGHT,
    DISPLAY_WIDTH,
    FONT_LABEL_SIZE,
    FONT_STATE_SIZE,
    PANEL_GAP,
    SIMVARS,
    TILE_PADDING,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    WS_HOST,
    WS_PORT,
)

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
BG = "#111111"
TILE_ON = "#00AA44"        # green  — normal ON / running
TILE_OFF = "#2A2A2A"       # dark grey — inactive
TILE_BLUE = "#1166DD"      # blue  — protective systems (pitot heat, anti-ice)
TILE_CYAN = "#007A8A"      # cyan  — armed states
TILE_AMBER = "#FFAA00"     # amber — caution / parking brake
TILE_PURPLE = "#8844BB"    # purple — (reserved / future use)
TILE_WARN_BRIGHT = "#FF2222"
TILE_WARN_DIM = "#5A0A0A"
TILE_CAUTION = TILE_AMBER  # right-panel caution uses same amber
TEXT_BRIGHT = "#FFFFFF"
TEXT_DIM = "#666666"
GAP_COLOR = "#222222"

_COLOR_MAP = {
    "green": (TILE_ON,     TEXT_BRIGHT),
    "blue":  (TILE_BLUE,   TEXT_BRIGHT),
    "cyan":  (TILE_CYAN,   TEXT_BRIGHT),
    "amber": (TILE_AMBER,  "#111111"),
    "purple":(TILE_PURPLE, TEXT_BRIGHT),
}

# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------
_FONT_CACHE: dict = {}

_DEJAVU_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for path in _DEJAVU_PATHS:
        try:
            font = ImageFont.truetype(path, size)
            break
        except (IOError, OSError):
            pass
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


# ---------------------------------------------------------------------------
# Tile drawing helpers
# ---------------------------------------------------------------------------

def _draw_tile(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    fill: str,
    label: str,
    state: str,
    text_color: str,
    compact: bool = False,
) -> None:
    draw.rounded_rectangle([x, y, x + w, y + h], radius=5, fill=fill)

    if compact:
        # Light-strip tiles: just label centered, smaller font
        font = _load_font(FONT_STATE_SIZE)
        bbox = draw.textbbox((0, 0), label, font=font)
        lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((x + (w - lw) // 2, y + (h - lh) // 2), label, font=font, fill=text_color)
        return

    font_lbl = _load_font(FONT_LABEL_SIZE)
    font_st = _load_font(FONT_STATE_SIZE)

    bbox = draw.textbbox((0, 0), label, font=font_lbl)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    bbox2 = draw.textbbox((0, 0), state, font=font_st)
    sw, sh = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]

    inner_gap = 3
    total_h = lh + inner_gap + sh
    ly = y + (h - total_h) // 2
    draw.text((x + (w - lw) // 2, ly), label, font=font_lbl, fill=text_color)
    draw.text((x + (w - sw) // 2, ly + lh + inner_gap), state, font=font_st, fill=text_color)


# ---------------------------------------------------------------------------
# Gauge drawing
# ---------------------------------------------------------------------------

def _lerp_color(a: str, b: str, t: float) -> str:
    t = max(0.0, min(1.0, t))
    ra, ga, ba = int(a[1:3], 16), int(a[3:5], 16), int(a[5:7], 16)
    rb, gb, bb = int(b[1:3], 16), int(b[3:5], 16), int(b[5:7], 16)
    return f"#{int(ra+(rb-ra)*t):02X}{int(ga+(gb-ga)*t):02X}{int(ba+(bb-ba)*t):02X}"


def _draw_gauge(
    draw: ImageDraw.ImageDraw,
    x: int, y: int, w: int, h: int,
    label: str, value: float, max_val: float,
    ticks: list, unit: str,
    color_a: str, color_b: str,
) -> None:
    # Background — subtle dark tint to distinguish from bool tiles
    draw.rounded_rectangle([x, y, x + w, y + h], radius=5, fill="#0A0A18")

    font = _load_font(FONT_STATE_SIZE)

    # Label (top-left) and value (top-right)
    val_str = f"{int(value)}{unit}"
    draw.text((x + 5, y + 3), label, font=font, fill=TEXT_BRIGHT)
    vbox = draw.textbbox((0, 0), val_str, font=font)
    draw.text((x + w - (vbox[2] - vbox[0]) - 5, y + 3), val_str, font=font,
              fill=_lerp_color(color_a, color_b, value / max_val if max_val else 0))

    # Track geometry
    pad_x = 12
    track_lx = x + pad_x
    track_rx = x + w - pad_x
    track_len = track_rx - track_lx
    track_cy = y + h - 14
    track_th = 7

    # Track background
    draw.rounded_rectangle(
        [track_lx, track_cy - track_th // 2, track_rx, track_cy + track_th // 2],
        radius=3, fill="#2A2A3A",
    )

    # Colored fill
    frac = value / max_val if max_val else 0
    fill_rx = track_lx + int(track_len * frac)
    if frac > 0:
        fill_color = _lerp_color(color_a, color_b, frac)
        draw.rounded_rectangle(
            [track_lx, track_cy - track_th // 2, fill_rx, track_cy + track_th // 2],
            radius=3, fill=fill_color,
        )

    # Tick marks below track
    for tick in ticks:
        tx = track_lx + int(track_len * tick / max_val) if max_val else track_lx
        draw.line([tx, track_cy + track_th // 2 + 1, tx, track_cy + track_th // 2 + 4],
                  fill="#555566", width=1)

    # Triangle pointer above track (pointing down toward track)
    ptr_x = track_lx + int(track_len * frac)
    ptr_top = track_cy - track_th // 2 - 7
    ptr_tip = track_cy - track_th // 2 - 1
    ptr_color = _lerp_color(color_a, color_b, frac) if frac > 0 else "#444444"
    draw.polygon(
        [(ptr_x, ptr_tip), (ptr_x - 4, ptr_top), (ptr_x + 4, ptr_top)],
        fill=ptr_color,
    )


# ---------------------------------------------------------------------------
# render_panel — pure Pillow, Pi-portable
# ---------------------------------------------------------------------------

def render_panel(data: dict, panel_side: str, flash: bool = False) -> Image.Image:
    """Return a 320×170 PIL.Image for one display panel."""
    img = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    panel_vars = {k: v for k, v in SIMVARS.items() if v["panel"] == panel_side}

    if panel_side == "left":
        _render_left(draw, data, panel_vars)
    else:
        _render_right(draw, data, panel_vars, flash)

    return img


def _left_tile_colors(value: bool, cfg: dict) -> tuple[str, str]:
    if not value:
        return TILE_OFF, TEXT_DIM
    fill, text = _COLOR_MAP.get(cfg.get("color", "green"), (TILE_ON, TEXT_BRIGHT))
    return fill, text


def _render_left(draw: ImageDraw.ImageDraw, data: dict, panel_vars: dict) -> None:
    margin = 5
    gap = 4

    light_items = [(k, v) for k, v in panel_vars.items() if v.get("light_row")]
    gauge_items  = [(k, v) for k, v in panel_vars.items() if v.get("type") == "gauge"]
    bool_items   = [(k, v) for k, v in panel_vars.items()
                    if not v.get("light_row") and v.get("type") != "gauge"]

    # --- Compact light strip ---
    light_h = 32
    n = len(light_items) or 1
    light_tile_w = (DISPLAY_WIDTH - 2 * margin - (n - 1) * gap) // n
    for i, (key, cfg) in enumerate(light_items):
        x = margin + i * (light_tile_w + gap)
        value = data.get(key, False)
        fill, text_color = _left_tile_colors(value, cfg)
        _draw_tile(draw, x, margin, light_tile_w, light_h,
                   fill, cfg["label"], "", text_color, compact=True)

    # --- Gauge section (bottom, fixed height) ---
    gauge_h = 52
    gauge_top = DISPLAY_HEIGHT - margin - gauge_h
    if gauge_items:
        n_g = len(gauge_items)
        gauge_tile_w = (DISPLAY_WIDTH - 2 * margin - (n_g - 1) * gap) // n_g
        for i, (key, cfg) in enumerate(gauge_items):
            gx = margin + i * (gauge_tile_w + gap)
            _draw_gauge(
                draw, gx, gauge_top, gauge_tile_w, gauge_h,
                cfg["label"], data.get(key, 0), cfg["max"],
                cfg.get("ticks", []), cfg.get("unit", ""),
                cfg["color_a"], cfg["color_b"],
            )

    # --- Boolean systems grid ---
    if not bool_items:
        return
    cols = 3
    rows = (len(bool_items) + cols - 1) // cols
    grid_top = margin + light_h + gap
    grid_bottom = gauge_top - gap
    available_h = grid_bottom - grid_top
    tile_w = (DISPLAY_WIDTH - 2 * margin - (cols - 1) * gap) // cols
    tile_h = (available_h - (rows - 1) * gap) // rows

    for i, (key, cfg) in enumerate(bool_items):
        col = i % cols
        row = i // cols
        x = margin + col * (tile_w + gap)
        y = grid_top + row * (tile_h + gap)
        value = data.get(key, False)
        fill, text_color = _left_tile_colors(value, cfg)
        state = "ON" if value else "OFF"
        _draw_tile(draw, x, y, tile_w, tile_h, fill, cfg["label"], state, text_color)


def _annunciator_colors(value: bool, severity: str, flash: bool) -> tuple[str, str, str]:
    """Return (fill, text_color, state_label) for an annunciator tile."""
    if not value:
        return TILE_OFF, TEXT_DIM, "NORM"
    if severity == "warning":
        return (TILE_WARN_BRIGHT if flash else TILE_WARN_DIM), TEXT_BRIGHT, "WARN"
    if severity == "caution":
        return TILE_CAUTION, "#111111", "CAUT"
    return TILE_ON, TEXT_BRIGHT, "ON"


def _render_right(
    draw: ImageDraw.ImageDraw, data: dict, panel_vars: dict, flash: bool
) -> None:
    margin = 6
    gap = 4

    top_items = [(k, v) for k, v in panel_vars.items() if v.get("top_row")]
    grid_items = [(k, v) for k, v in panel_vars.items() if not v.get("top_row")]

    # --- Top row ---
    top_cols = len(top_items) or 1
    top_h = 50
    top_tile_w = (DISPLAY_WIDTH - 2 * margin - (top_cols - 1) * gap) // top_cols

    for i, (key, cfg) in enumerate(top_items):
        x = margin + i * (top_tile_w + gap)
        y = margin
        fill, text_color, state = _annunciator_colors(
            data.get(key, False), cfg.get("severity", ""), flash
        )
        _draw_tile(draw, x, y, top_tile_w, top_h, fill, cfg["label"], state, text_color)

    # --- Secondary grid ---
    if not grid_items:
        return

    grid_cols = 3
    grid_rows = (len(grid_items) + grid_cols - 1) // grid_cols
    grid_top = margin + top_h + gap
    grid_available_h = DISPLAY_HEIGHT - grid_top - margin
    grid_tile_w = (DISPLAY_WIDTH - 2 * margin - (grid_cols - 1) * gap) // grid_cols
    grid_tile_h = (grid_available_h - (grid_rows - 1) * gap) // grid_rows

    for i, (key, cfg) in enumerate(grid_items):
        col = i % grid_cols
        row = i // grid_cols
        x = margin + col * (grid_tile_w + gap)
        y = grid_top + row * (grid_tile_h + gap)
        fill, text_color, state = _annunciator_colors(
            data.get(key, False), cfg.get("severity", ""), flash
        )
        _draw_tile(draw, x, y, grid_tile_w, grid_tile_h, fill, cfg["label"], state, text_color)


# ---------------------------------------------------------------------------
# Tkinter app
# ---------------------------------------------------------------------------

class DisplayApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("Bravo Display")
        root.configure(bg=GAP_COLOR)
        root.resizable(False, False)

        self.canvas = tk.Canvas(
            root,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            bg=GAP_COLOR,
            highlightthickness=0,
        )
        self.canvas.pack()

        self._lock = threading.Lock()
        self._sim_data: dict = {key: False for key in SIMVARS}
        self._flash = False
        self._photo = None  # keep reference to prevent GC

        # Render initial (all-off) state immediately
        self._redraw()

        # Start WS background thread
        t = threading.Thread(target=self._run_ws_thread, daemon=True)
        t.start()

    # --- WebSocket background thread ---

    def _run_ws_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_client())

    async def _ws_client(self) -> None:
        uri = f"ws://{WS_HOST}:{WS_PORT}"
        while True:
            try:
                async with connect(uri) as ws:
                    print(f"[WS] Connected to {uri}")
                    async for raw in ws:
                        data = json.loads(raw)
                        with self._lock:
                            self._sim_data.update(data)
                            self._flash = not self._flash
                        self.root.after(0, self._redraw)
            except Exception as exc:
                print(f"[WS] {exc!r} — reconnecting in 2 s…")
                await asyncio.sleep(2)

    # --- Tkinter render (main thread) ---

    def _redraw(self) -> None:
        with self._lock:
            data = dict(self._sim_data)
            flash = self._flash

        left = render_panel(data, "left", flash)
        right = render_panel(data, "right", flash)

        combined = Image.new("RGB", (WINDOW_WIDTH, WINDOW_HEIGHT), GAP_COLOR)
        combined.paste(left, (0, 0))
        combined.paste(right, (DISPLAY_WIDTH + PANEL_GAP, 0))

        photo = ImageTk.PhotoImage(combined)
        self.canvas.create_image(0, 0, anchor="nw", image=photo)
        self._photo = photo  # prevent garbage collection


def main() -> None:
    root = tk.Tk()
    DisplayApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
