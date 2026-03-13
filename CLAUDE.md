# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Raspberry Pi sim cockpit display for MSFS 2024. A Raspberry Pi Zero 2W drives two Waveshare 1.9" SPI displays (ST7789V2, 320×170 in landscape), showing aircraft state received over WebSocket from a Windows PC running MSFS 2024.

## Commands

```bash
# Install dependencies
uv sync

# Desktop testing (run in two terminals)
uv run python mock_server.py   # Terminal 1: fake data server on ws://WS_HOST:8765
uv run python display.py       # Terminal 2: tkinter preview window

# Live MSFS data (Windows only, requires SimConnect)
uv run --extra pysimconnect python simconnect_proxy.py [--host 0.0.0.0] [--port 8765] [--debug]
```

## Architecture

The system has two runtime modes sharing the same WS protocol:

```
[mock_server.py]  OR  [simconnect_proxy.py]
       │ ws://WS_HOST:8765  (JSON broadcast every 100ms)
       ▼
[display.py] — tkinter preview (desktop) or render_panel() → ST7789V2 (Pi)
```

### Data flow

1. **Data sources** broadcast a flat JSON dict (`{SIMVAR_KEY: value, ...}`) over WebSocket every 100ms.
2. **`display.py`** runs a WS client in a background asyncio thread, posts data to tkinter via `root.after(0, _redraw)`, and calls `render_panel()` for each panel.
3. **`render_panel(data, panel_side, flash)`** returns a `PIL.Image` (320×170) — pure Pillow, no tkinter. This is the Pi-portable rendering function: on Pi, pass the returned image directly to `display.ShowImage(img)`.

### Key design points

- **`config.py`** is the single source of truth for all SimVars. Adding a new indicator requires only adding an entry to `SIMVARS` (display config) and `SIMVAR_MAP` in `simconnect_proxy.py` (SimConnect mapping).
- **`SIMVARS` dict** drives all rendering. Each entry has `panel` ("left"/"right") and optional keys: `light_row`, `type="gauge"`, `severity` ("warning"/"caution"), `top_row`, `color`.
- **Left panel layout**: compact light strip (top) → boolean systems grid (middle) → position gauges (bottom).
- **Right panel layout**: full-width top-row annunciators → secondary annunciator grid.
- **Flash** toggled on every WS message; warning-severity tiles alternate between bright red and dim red.
- **`WS_HOST` in `config.py`** must match the server machine's IP. Set to `"localhost"` for local testing with `mock_server.py`.

### SimVar types in `SIMVARS`

| Key fields | Tile type |
|---|---|
| (default) | Boolean tile — ON/OFF with optional `color` ("green"/"blue"/"amber") |
| `"light_row": True` | Compact light strip tile (label only, no state text) |
| `"type": "gauge"` | Position gauge bar with `max`, `ticks`, `color_a`, `color_b`, `unit` |
| `"severity": "warning"/"caution"` | Right-panel annunciator — warning=red flash, caution=amber |
| `"top_row": True` | Placed in the 3-wide header row of the right panel |

### SimConnect proxy details (`simconnect_proxy.py`)

- `SIMVAR_MAP`: maps our internal key → `(SimConnect var name or list, units, transform_fn)`.
- A list of var names (e.g. MASTER_WARNING) means "poll all, use `any()`" — supports multiple aircraft variants.
- SimConnect polling runs in a thread at 100ms; WS broadcast is a separate asyncio task also at 100ms.
- `MASTER_WARNING`/`MASTER_CAUTION` use L:vars (aircraft-specific local variables) — update the list for your aircraft.

### Fonts

`display.py` searches for `DejaVuSans-Bold.ttf` in standard Linux/macOS paths, then falls back to Pillow's default font. On Pi, install `fonts-dejavu` for best results.
