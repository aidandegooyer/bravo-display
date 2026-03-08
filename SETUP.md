# Bravo Display — Setup & Usage Guide

Two Waveshare 1.9" SPI displays driven by a Raspberry Pi Zero 2W, showing
sim cockpit state for MSFS 2024 via a Honeycomb Bravo Throttle Quadrant.

```
┌─────────────────┐    WebSocket     ┌──────────────────────┐
│  Windows PC     │  ws://<ip>:8765  │  Raspberry Pi Zero   │
│  MSFS 2024      │ ◄──────────────► │  display.py          │
│  simconnect_    │                  │  Two ST7789V2 panels  │
│  proxy.py       │                  └──────────────────────┘
└─────────────────┘

         OR (development/testing)

┌─────────────────────────────────────────┐
│  Any machine (macOS / Linux / Windows)  │
│  mock_server.py  ◄──►  display.py       │
│  (simulates random sim data)            │
└─────────────────────────────────────────┘
```

---

## Project layout

```
bravo-display/
├── config.py              # SimVar definitions, display geometry, WS settings
├── mock_server.py         # Fake data server for testing (no MSFS needed)
├── simconnect_proxy.py    # Live data from MSFS via SimConnect (Windows only)
├── display.py             # Tkinter preview + pure-Pillow renderer
├── main.py                # (Pi entry point — calls render_panel directly)
└── pyproject.toml
```

---

## 1. Development / Desktop Testing (no MSFS)

Works on macOS, Linux, or Windows.

### Prerequisites

- Python 3.14+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- tkinter (usually bundled; on Linux: `sudo apt install python3-tk`)

### Install

```bash
git clone <repo>
cd bravo-display
uv sync
```

### Run

**Terminal 1 — fake data server:**
```bash
uv run python mock_server.py
```

**Terminal 2 — display preview:**
```bash
uv run python display.py
```

A 644×170 window opens showing both panels. The mock server:
- Broadcasts state every 500 ms
- Randomly toggles boolean switches every 3–6 s
- Steps flaps/spoiler positions every 2–4 s

---

## 2. Live MSFS Data (Windows)

### Prerequisites

- Windows 10/11 with MSFS 2024 installed and running
- Python 3.9+ (the SimConnect library doesn't require 3.14)
- The `SimConnect` Python package (wraps `SimConnect.dll`)

### Install SimConnect

```cmd
pip install SimConnect
```

> `SimConnect.dll` ships with MSFS. The Python library locates it
> automatically via the Windows registry.

### Run the proxy

```cmd
cd bravo-display
python simconnect_proxy.py
```

By default it listens on `0.0.0.0:8765` (all network interfaces).

```
[SC] Connecting to SimConnect…
[SC] Connected. Polling…
[WS] Listening on ws://0.0.0.0:8765
     Point display.py WS_HOST to this machine's IP address.
```

**Optional flags:**
```
--host 192.168.1.50   bind to a specific interface
--port 9000           use a different port
```

### Connect display.py to the proxy

Edit `config.py` on the Pi / your desktop:

```python
WS_HOST = "192.168.1.50"   # ← IP of your Windows PC
WS_PORT = 8765
```

Then run `display.py` (or the Pi's `main.py`) as normal.

### Windows Firewall

You may need to allow inbound TCP on port 8765:

```powershell
New-NetFirewallRule -DisplayName "Bravo Display WS" `
  -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

---

## 3. Raspberry Pi Zero 2W Deployment

### OS / dependencies

```bash
sudo apt update
sudo apt install python3-pip python3-pillow python3-websockets
pip3 install websockets pillow
```

### SPI displays

Enable SPI in `raspi-config → Interface Options → SPI`.

Install the Waveshare ST7789V2 driver (see Waveshare wiki for your model)
and wire both displays to separate CE pins (CE0 / CE1).

### `main.py` structure

`render_panel()` in `display.py` is pure Pillow — it returns a `PIL.Image`
with no tkinter dependency. On the Pi, call it directly:

```python
from display import render_panel

# left_display / right_display are your Waveshare driver instances
img_left  = render_panel(data, "left",  flash)
img_right = render_panel(data, "right", flash)

left_display.ShowImage(img_left)
right_display.ShowImage(img_right)
```

---

## 4. SimConnect Variable Reference

`simconnect_proxy.py` contains a `SIMVAR_MAP` dict. Each entry maps an
internal key to a SimConnect variable name and a transform function.

| Config key | SimConnect variable | Notes |
|---|---|---|
| `LIGHT_LANDING_LEFT` | `LIGHT LANDING` | No separate L/R in standard SimConnect |
| `LIGHT_LANDING_RIGHT` | `LIGHT LANDING` | Same var as above |
| `LIGHT_STROBE` | `LIGHT STROBE` | |
| `LIGHT_NAV` | `LIGHT NAV` | |
| `LIGHT_BEACON` | `LIGHT BEACON` | |
| `LIGHT_TAXI` | `LIGHT TAXI` | |
| `ENGINE_1_RUNNING` | `GENERAL ENG COMBUSTION:1` | |
| `ENGINE_2_RUNNING` | `GENERAL ENG COMBUSTION:2` | |
| `GEAR_DEPLOYED` | `GEAR TOTAL PCT EXTENDED` | True when > 95% |
| `ANTI_ICE_ON` | `STRUCTURAL DEICE SWITCH` | May differ by aircraft |
| `PITOT_HEAT` | `PITOT HEAT` | |
| `PARKING_BRAKE` | `BRAKE PARKING INDICATOR` | |
| `HYD_PRESSURE_WARNING` | `HYDRAULIC PRESSURE:1` | True when < 1500 PSI |
| `STALL_WARNING` | `STALL WARNING` | |
| `OVERSPEED` | `OVERSPEED WARNING` | |
| `OIL_PRESSURE_LOW` | `ENG OIL PRESSURE:1` | True when < 10 PSI |
| `LOW_FUEL_L` | `FUEL TANK LEFT MAIN QUANTITY` | True when < 10 US gal |
| `LOW_FUEL_R` | `FUEL TANK RIGHT MAIN QUANTITY` | True when < 10 US gal |
| `DOOR_OPEN` | `EXIT OPEN:0` | Index 0 = main door |
| `FLAPS_POSITION` | `FLAPS HANDLE INDEX` | Integer 0–5 |
| `SPOILER_POSITION` | `SPOILERS HANDLE POSITION` | Float 0–100 % |

### Variables without a universal SimConnect mapping

`MASTER_WARNING` and `MASTER_CAUTION` are not standard SimConnect variables —
they are aircraft-specific. Most study-level airliners expose them as
**L:vars** (local variables) via a WASM gauge. Once you know your aircraft's
variable names, uncomment and update these lines in `SIMVAR_MAP`:

```python
"MASTER_WARNING": ("L:Generic_Master_Warning_Active", _b),
"MASTER_CAUTION": ("L:Generic_Master_Caution_Active", _b),
```

Use tools like [MSFS Variable Viewer](https://flightsim.to/file/26938/msfs-variable-viewer)
or SimConnect's own variable browser to find the right names for your aircraft.

---

## 5. Adding or changing indicators

All display content is driven by `config.py`. To add a new indicator:

1. Add an entry to `SIMVARS` in `config.py`:
   ```python
   "APU_RUNNING": {"label": "APU", "panel": "left"},
   ```

2. Add a mapping in `simconnect_proxy.py`'s `SIMVAR_MAP`:
   ```python
   "APU_RUNNING": ("APU GENERATOR SWITCH:1", _b),
   ```

3. Restart both scripts — no other changes needed.

### Gauge type

For a position indicator (like flaps/spoilers):
```python
"ELEVATOR_TRIM": {
    "label": "TRIM", "panel": "left", "type": "gauge",
    "max": 100, "ticks": [0, 25, 50, 75, 100], "unit": "%",
    "color_a": "#00AAFF", "color_b": "#FF4400",
},
```
```python
# simconnect_proxy.py
"ELEVATOR_TRIM": ("ELEVATOR TRIM PCT", lambda v: round(_f(v) * 100, 1)),
```

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `display.py` shows "reconnecting" | Start `mock_server.py` or `simconnect_proxy.py` first |
| SimConnect error: DLL not found | MSFS must be installed; launch MSFS before the proxy |
| SimConnect error: not connected | MSFS must be **running** (at the main menu or in a flight) |
| Variable returns `None` | Variable name may differ for your aircraft — check `SIMVAR_MAP` |
| Tiles not updating | Check WS_HOST in config.py matches the server machine's IP |
| Flaps stuck at 0 | Some aircraft use `FLAPS HANDLE PERCENT` instead of `FLAPS HANDLE INDEX` |
| Spoilers stuck at 0 | Try `SPOILER HANDLE POSITION` (no S) or `SPOILERS LEFT POSITION` |
