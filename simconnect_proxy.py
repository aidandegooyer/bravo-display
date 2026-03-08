"""
simconnect_proxy.py — SimConnect → WebSocket bridge

Reads live data from MSFS 2024 via SimConnect and broadcasts it over
WebSocket in the same JSON format as mock_server.py.

Run this on the Windows PC running MSFS. The Pi (or any display.py client)
connects to ws://<this-pc-ip>:8765 instead of localhost.

Requirements (Windows only):
    pip install SimConnect
    -- or inside this project's venv --
    uv add SimConnect --optional simconnect

Usage:
    python simconnect_proxy.py [--host 0.0.0.0] [--port 8765]
"""

import argparse
import asyncio
import json
import signal
import threading
import time

try:
    from SimConnect import AircraftRequests, SimConnect
    SIMCONNECT_AVAILABLE = True
except ImportError:
    SIMCONNECT_AVAILABLE = False

from websockets.asyncio.server import serve

from config import SIMVARS, WS_PORT

# ---------------------------------------------------------------------------
# SimConnect → our SimVar mapping
#
# Format:  "OUR_KEY": ("SimConnect Variable Name", transform_fn)
#
# transform_fn receives the raw value from SimConnect (may be None on error)
# and should return a bool or numeric value matching config.py expectations.
#
# Unmapped keys (e.g. MASTER_WARNING) stay at their default (False / 0).
# You can add aircraft-specific L:var reads via a WASM bridge separately.
# ---------------------------------------------------------------------------

def _b(v):
    """Raw value → bool."""
    return bool(int(v)) if v is not None else False

def _f(v, default=0.0):
    """Raw value → float."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

SIMVAR_MAP: dict[str, tuple[str, callable]] = {
    # --- Lights ---
    "LIGHT_LANDING_LEFT":  ("LIGHT LANDING",                      _b),
    "LIGHT_LANDING_RIGHT": ("LIGHT LANDING",                      _b),
    "LIGHT_STROBE":        ("LIGHT STROBE",                       _b),
    "LIGHT_NAV":           ("LIGHT NAV",                          _b),
    "LIGHT_BEACON":        ("LIGHT BEACON",                       _b),
    "LIGHT_TAXI":          ("LIGHT TAXI",                         _b),

    # --- Engines ---
    "ENGINE_1_RUNNING":    ("GENERAL ENG COMBUSTION:1",           _b),
    "ENGINE_2_RUNNING":    ("GENERAL ENG COMBUSTION:2",           _b),

    # --- Systems ---
    # Gear: true when all gear > 95 % extended
    "GEAR_DEPLOYED":       ("GEAR TOTAL PCT EXTENDED",            lambda v: _f(v) > 0.95),
    # Anti-ice: structural de-ice switch (check your aircraft — may differ)
    "ANTI_ICE_ON":         ("STRUCTURAL DEICE SWITCH",            _b),
    "PITOT_HEAT":          ("PITOT HEAT",                         _b),
    "PARKING_BRAKE":       ("BRAKE PARKING INDICATOR",            _b),

    # --- Annunciators ---
    # MASTER_WARNING / MASTER_CAUTION have no universal SimConnect variable.
    # Most airliners expose them as L:vars via WASM. Map them here once you
    # know your aircraft's specific variable names.
    # "MASTER_WARNING":   ("L:Generic_Master_Warning_Active", _b),
    # "MASTER_CAUTION":   ("L:Generic_Master_Caution_Active", _b),

    # Hydraulic: warn if pressure on system 1 drops below 1500 PSI
    "HYD_PRESSURE_WARNING": ("HYDRAULIC PRESSURE:1",             lambda v: _f(v, 9999) < 1500),
    "STALL_WARNING":         ("STALL WARNING",                    _b),
    "OVERSPEED":             ("OVERSPEED WARNING",                _b),
    # Oil: warn below 10 PSI (adjust threshold to aircraft)
    "OIL_PRESSURE_LOW":      ("ENG OIL PRESSURE:1",              lambda v: _f(v, 9999) < 10),
    # Fuel: warn when tank < 10 US gallons (adjust threshold to aircraft)
    "LOW_FUEL_L":            ("FUEL TANK LEFT MAIN QUANTITY",    lambda v: _f(v, 9999) < 10),
    "LOW_FUEL_R":            ("FUEL TANK RIGHT MAIN QUANTITY",   lambda v: _f(v, 9999) < 10),
    # Door: EXIT OPEN:0 is the main cabin door on most aircraft
    "DOOR_OPEN":             ("EXIT OPEN:0",                     lambda v: _f(v) > 0.1),

    # --- Gauges ---
    # Flaps handle index: integer 0–N (N depends on aircraft, typically 0–5)
    "FLAPS_POSITION":        ("FLAPS HANDLE INDEX",              lambda v: int(_f(v))),
    # Spoilers: 0–100 % (some aircraft use 0.0–1.0 — check and adjust)
    "SPOILER_POSITION":      ("SPOILERS HANDLE POSITION",        lambda v: round(_f(v), 1)),
}

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_gauge_keys = {k for k, v in SIMVARS.items() if v.get("type") == "gauge"}
_sim_state: dict = {k: (0 if k in _gauge_keys else False) for k in SIMVARS}
_state_lock = threading.Lock()
_connected_clients: set = set()


# ---------------------------------------------------------------------------
# SimConnect polling thread
# ---------------------------------------------------------------------------

def _poll_simconnect(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        if not SIMCONNECT_AVAILABLE:
            print("[SC] SimConnect not installed. Install with: pip install SimConnect")
            stop_event.wait(5)
            continue

        try:
            print("[SC] Connecting to SimConnect…")
            sm = SimConnect()
            aq = AircraftRequests(sm, _time=200)  # 200 ms internal refresh
            print("[SC] Connected. Polling…")

            while not stop_event.is_set():
                updates: dict = {}
                for our_key, (sc_var, transform) in SIMVAR_MAP.items():
                    if our_key not in _sim_state:
                        continue
                    try:
                        raw = aq.get(sc_var)
                        updates[our_key] = transform(raw)
                    except Exception:
                        pass  # leave previous value intact

                with _state_lock:
                    _sim_state.update(updates)

                stop_event.wait(0.25)  # poll at ~4 Hz; WS broadcasts at 2 Hz

        except Exception as exc:
            print(f"[SC] Error: {exc!r} — retrying in 5 s…")
            stop_event.wait(5)

    print("[SC] Poll thread exiting.")


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

async def _ws_handler(websocket) -> None:
    _connected_clients.add(websocket)
    print(f"[WS] Client connected: {websocket.remote_address}")
    try:
        with _state_lock:
            snapshot = dict(_sim_state)
        await websocket.send(json.dumps(snapshot))
        await websocket.wait_closed()
    finally:
        _connected_clients.discard(websocket)
        print(f"[WS] Client disconnected: {websocket.remote_address}")


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(0.5)
        if not _connected_clients:
            continue
        with _state_lock:
            msg = json.dumps(_sim_state)
        await asyncio.gather(
            *[ws.send(msg) for ws in list(_connected_clients)],
            return_exceptions=True,
        )


async def _main(host: str, port: int) -> None:
    stop_future = asyncio.get_event_loop().create_future()
    loop = asyncio.get_event_loop()

    def _shutdown():
        if not stop_future.done():
            stop_future.set_result(None)

    try:
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGTERM, _shutdown)
    except NotImplementedError:
        # Windows: fall back to signal.signal (Ctrl+C still works)
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(_shutdown))

    # Start SimConnect poll thread
    sc_stop = threading.Event()
    sc_thread = threading.Thread(target=_poll_simconnect, args=(sc_stop,), daemon=True)
    sc_thread.start()

    async with serve(_ws_handler, host, port):
        print(f"[WS] Listening on ws://{host}:{port}")
        print(f"     Point display.py WS_HOST to this machine's IP address.")
        asyncio.create_task(_broadcast_loop())
        await stop_future

    sc_stop.set()
    print("[WS] Server stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SimConnect → WebSocket proxy")
    parser.add_argument("--host", default="0.0.0.0",
                        help="WebSocket listen address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=WS_PORT,
                        help=f"WebSocket port (default: {WS_PORT})")
    args = parser.parse_args()

    asyncio.run(_main(args.host, args.port))
