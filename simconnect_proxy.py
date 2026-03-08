"""
simconnect_proxy.py — SimConnect → WebSocket bridge

Reads live data from MSFS 2024 via SimConnect and broadcasts it over
WebSocket in the same JSON format as mock_server.py.

Run this on the Windows PC running MSFS. The Pi (or any display.py client)
connects to ws://<this-pc-ip>:8765 instead of localhost.

Requirements:
    uv run --extra pysimconnect python simconnect_proxy.py

Usage:
    python simconnect_proxy.py [--host 0.0.0.0] [--port 8765] [--dll PATH]
"""

import argparse
import asyncio
import json
import signal
import threading

from websockets.asyncio.server import serve

from config import SIMVARS, WS_PORT, SC_DLL_PATH

# ---------------------------------------------------------------------------
# SimConnect → our SimVar mapping
#
# Format: "OUR_KEY": ("SimConnect Variable Name", "units", transform_fn)
#
# transform_fn receives the value already converted to the requested units
# (always float64 for numeric/bool types) and returns a bool or number.
#
# Unmapped keys (e.g. MASTER_WARNING) stay at their default (False / 0).
# ---------------------------------------------------------------------------

def _b(v):
    """float → bool"""
    return bool(v)


SIMVAR_MAP: dict[str, tuple[str, str | None, callable]] = {
    # --- Lights ---
    "LIGHT_LANDING_LEFT":    ("LIGHT LANDING",               "Bool",                   _b),
    "LIGHT_LANDING_RIGHT":   ("LIGHT LANDING",               "Bool",                   _b),
    "LIGHT_STROBE":          ("LIGHT STROBE",                "Bool",                   _b),
    "LIGHT_NAV":             ("LIGHT NAV",                   "Bool",                   _b),
    "LIGHT_BEACON":          ("LIGHT BEACON",                "Bool",                   _b),
    "LIGHT_TAXI":            ("LIGHT TAXI",                  "Bool",                   _b),

    # --- Engines ---
    "ENGINE_1_RUNNING":      ("GENERAL ENG COMBUSTION:1",    "Bool",                   _b),
    "ENGINE_2_RUNNING":      ("GENERAL ENG COMBUSTION:2",    "Bool",                   _b),

    # --- Systems ---
    "GEAR_DEPLOYED":         ("GEAR TOTAL PCT EXTENDED",     "Percent Over 100",       lambda v: float(v) > 0.95),
    "ANTI_ICE_ON":           ("STRUCTURAL DEICE SWITCH",     "Bool",                   _b),
    "PITOT_HEAT":            ("PITOT HEAT",                  "Bool",                   _b),
    "PARKING_BRAKE":         ("BRAKE PARKING INDICATOR",     "Bool",                   _b),

    # --- Annunciators ---
    # MASTER_WARNING / MASTER_CAUTION have no universal SimConnect variable;
    # most airliners expose them as L:vars via WASM. Add them here once you
    # know your aircraft's specific variable names.
    # "MASTER_WARNING":      ("L:Generic_Master_Warning_Active", "Bool",               _b),
    # "MASTER_CAUTION":      ("L:Generic_Master_Caution_Active", "Bool",               _b),

    "HYD_PRESSURE_WARNING":  ("HYDRAULIC PRESSURE:1",        "Pounds per square inch", lambda v: float(v) < 1500),
    "STALL_WARNING":         ("STALL WARNING",               "Bool",                   _b),
    "OVERSPEED":             ("OVERSPEED WARNING",           "Bool",                   _b),
    # Oil: warn below 10 PSI (adjust threshold to aircraft)
    "OIL_PRESSURE_LOW":      ("ENG OIL PRESSURE:1",         "Pounds per square inch", lambda v: float(v) < 10),
    # Fuel: warn when tank < 10 US gallons (adjust threshold to aircraft)
    "LOW_FUEL_L":            ("FUEL TANK LEFT MAIN QUANTITY",  "Gallons",              lambda v: float(v) < 10),
    "LOW_FUEL_R":            ("FUEL TANK RIGHT MAIN QUANTITY", "Gallons",              lambda v: float(v) < 10),
    # Door: EXIT OPEN:0 is the main cabin door on most aircraft
    "DOOR_OPEN":             ("EXIT OPEN:0",                 "Percent Over 100",       lambda v: float(v) > 0.1),

    # --- Gauges ---
    "FLAPS_POSITION":        ("FLAPS HANDLE INDEX",          "Number",                 lambda v: int(v)),
    # Spoilers: 0–100 % (some aircraft use a 0.0–1.0 range — check and adjust)
    "SPOILER_POSITION":      ("SPOILERS HANDLE POSITION",    "Percent",                lambda v: round(float(v), 1)),
}

# Build deduplicated subscription spec (some internal keys share the same SC var)
_SC_SPEC: list[dict] = []
_sc_vars_seen: set[str] = set()
for _our_key, (_sc_var, _units, _) in SIMVAR_MAP.items():
    if _sc_var not in _sc_vars_seen:
        _sc_vars_seen.add(_sc_var)
        _SC_SPEC.append({"name": _sc_var, "units": _units})


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

def _poll_simconnect(stop_event: threading.Event, dll_path: str, debug: bool = False) -> None:
    from simconnect import SimConnect
    from simconnect.datadef import DataDefinition
    from simconnect.scdefs import PERIOD_SECOND

    while not stop_event.is_set():
        try:
            print("[SC] Connecting to SimConnect…")
            # Clear cached data definitions so they get re-registered on reconnect
            DataDefinition._instances.clear()

            with SimConnect(dll_path=dll_path) as sc:
                dd = sc.subscribe_simdata(_SC_SPEC, period=PERIOD_SECOND)
                print("[SC] Connected. Polling…")

                while not stop_event.is_set():
                    sc.receive(timeout_seconds=0.25)

                    if not dd.simdata:
                        continue

                    updates: dict = {}
                    for our_key, (sc_var, _, transform) in SIMVAR_MAP.items():
                        val = dd.simdata.get(sc_var)
                        if val is not None:
                            try:
                                updates[our_key] = transform(val)
                            except Exception:
                                pass

                    with _state_lock:
                        _sim_state.update(updates)

                    if debug and updates:
                        print(f"[SC] {updates}")

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


async def _main(host: str, port: int, dll_path: str, debug: bool = False) -> None:
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
    sc_thread = threading.Thread(
        target=_poll_simconnect, args=(sc_stop, dll_path, debug), daemon=True
    )
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
    parser.add_argument("--dll", default=SC_DLL_PATH,
                        help=f"Path to SimConnect.dll (default: {SC_DLL_PATH})")
    parser.add_argument("--debug", action="store_true",
                        help="Print every SimConnect update to stdout")
    args = parser.parse_args()

    asyncio.run(_main(args.host, args.port, args.dll, args.debug))
