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
import logging
import math
import signal
import threading

# Suppress noisy websockets handshake errors (TCP probes / browser keepalives
# that open a connection but close before completing the HTTP upgrade).
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

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


SIMVAR_MAP: dict[str, tuple[str | list[str], str | None, callable]] = {
    # --- Lights ---
    "LIGHT_LANDING_LEFT": ("LIGHT LANDING", "Bool", _b),
    "LIGHT_LANDING_RIGHT": ("LIGHT LANDING", "Bool", _b),
    "LIGHT_STROBE": ("LIGHT STROBE", "Bool", _b),
    "LIGHT_NAV": ("LIGHT NAV", "Bool", _b),
    "LIGHT_BEACON": ("LIGHT BEACON", "Bool", _b),
    "LIGHT_TAXI": ("LIGHT TAXI", "Bool", _b),
    # --- Engines ---
    "ENGINE_1_RUNNING": ("GENERAL ENG COMBUSTION:1", "Bool", _b),
    "ENGINE_2_RUNNING": ("GENERAL ENG COMBUSTION:2", "Bool", _b),
    # --- Systems ---
    "GEAR_DEPLOYED": (
        "GEAR TOTAL PCT EXTENDED",
        "Percent Over 100",
        lambda v: float(v) > 0.95,
    ),
    "ANTI_ICE_ON": ("STRUCTURAL DEICE SWITCH", "Bool", _b),
    "PITOT_HEAT": ("PITOT HEAT", "Bool", _b),
    "PARKING_BRAKE": ("BRAKE PARKING INDICATOR", "Bool", _b),
    # --- Annunciators ---
    # List multiple L:var names to support different aircraft — any() of results is used.
    "MASTER_WARNING": (
        [
            "L:INI_ATLEASTONEMASTERWARNING",  # iniBuilds A321 (default MSFS 2024)
            "L:A32NX_MASTER_WARNING",  # FlyByWire A32NX
        ],
        "Bool",
        _b,
    ),
    "MASTER_CAUTION": (
        [
            "L:INI_ATLEASTONEMASTERCAUTION",  # iniBuilds A321 (default MSFS 2024)
            "L:A32NX_MASTER_CAUTION",  # FlyByWire A32NX
        ],
        "Bool",
        _b,
    ),
    "HYD_PRESSURE_WARNING": (
        "HYDRAULIC PRESSURE:1",
        "Pounds per square inch",
        lambda v: float(v) < 1500,
    ),
    "STALL_WARNING": ("STALL WARNING", "Bool", _b),
    "OVERSPEED": ("OVERSPEED WARNING", "Bool", _b),
    # Oil: warn below 10 PSI (adjust threshold to aircraft)
    "OIL_PRESSURE_LOW": (
        "ENG OIL PRESSURE:1",
        "Pounds per square inch",
        lambda v: float(v) < 10,
    ),
    # Fuel: warn when tank < 10 US gallons (adjust threshold to aircraft)
    "LOW_FUEL_L": ("FUEL TANK LEFT MAIN QUANTITY", "Gallons", lambda v: float(v) < 10),
    "LOW_FUEL_R": ("FUEL TANK RIGHT MAIN QUANTITY", "Gallons", lambda v: float(v) < 10),
    # Door: EXIT OPEN:0 is the main cabin door on most aircraft
    "DOOR_OPEN": ("EXIT OPEN:0", "Percent Over 100", lambda v: float(v) > 0.1),
    # --- Gauges ---
    "FLAPS_POSITION": ("FLAPS HANDLE INDEX", "Number", lambda v: int(v)),
    # Spoilers: 0–100 % (some aircraft use a 0.0–1.0 range — check and adjust)
    "SPOILER_POSITION": (
        "SPOILERS HANDLE POSITION",
        "Percent",
        lambda v: round(float(v), 1),
    ),
    # -------------------------------------------------------------------------
    # PFD — Primary Flight Display vars
    # -------------------------------------------------------------------------
    # --- Air data ---
    "airspeed": ("AIRSPEED INDICATED", "Knots", lambda v: round(float(v), 1)),
    "ground_speed": ("GROUND VELOCITY", "Knots", lambda v: round(float(v), 1)),
    "altitude": ("INDICATED ALTITUDE", "Feet", lambda v: round(float(v), 1)),
    "radio_alt": ("PLANE ALT ABOVE GROUND", "Feet", lambda v: round(float(v), 1)),
    "vertical_speed": (
        "VERTICAL SPEED",
        "Feet per minute",
        lambda v: round(float(v), 0),
    ),
    "baro_setting": ("KOHLSMAN SETTING HG", "inHg", lambda v: f"{float(v):.2f}"),
    # --- Attitude ---
    # Positive pitch = nose up; positive bank = right wing down (left roll)
    "pitch": ("PLANE PITCH DEGREES", "Degrees", lambda v: round(float(v), 2)),
    "bank": ("PLANE BANK DEGREES", "Degrees", lambda v: round(float(v), 2)),
    "heading": (
        "PLANE HEADING DEGREES MAGNETIC",
        "Degrees",
        lambda v: round(float(v), 1),
    ),
    # --- Flight director ---
    "fd_pitch": (
        "AUTOPILOT FLIGHT DIRECTOR PITCH",
        "Degrees",
        lambda v: round(float(v), 2),
    ),
    "fd_bank": (
        "AUTOPILOT FLIGHT DIRECTOR BANK",
        "Degrees",
        lambda v: round(float(v), 2),
    ),
    # --- Autopilot modes ---
    "autopilot_active": ("AUTOPILOT MASTER", "Bool", _b),
    "at_active": ("AUTOPILOT THROTTLE ARM", "Bool", _b),
    "lnav_active": ("AUTOPILOT NAV1 LOCK", "Bool", _b),
    "vnav_active": ("AUTOPILOT ALTITUDE LOCK", "Bool", _b),
    "app_active": ("AUTOPILOT APPROACH HOLD", "Bool", _b),
    "loc_active": ("AUTOPILOT NAV1 LOCK", "Bool", _b),
    # --- Autopilot selected values ---
    "selected_speed": (
        "AUTOPILOT AIRSPEED HOLD VAR",
        "Knots",
        lambda v: round(float(v), 0),
    ),
    "selected_altitude": (
        "AUTOPILOT ALTITUDE LOCK VAR",
        "Feet",
        lambda v: round(float(v), 0),
    ),
    "selected_heading": (
        "AUTOPILOT HEADING LOCK DIR",
        "Degrees",
        lambda v: round(float(v), 1),
    ),
    # --- Flaps (shared with annunciator panel) ---
    "flaps": ("FLAPS HANDLE INDEX", "Number", lambda v: int(v)),
    # -------------------------------------------------------------------------
    # EICAS / EIS — engine instrument vars
    # -------------------------------------------------------------------------
    # GA / piston
    "rpm":               ("GENERAL ENG RPM:1",            "RPM",              lambda v: round(float(v), 0)),
    "manifold_pressure": ("ENG MANIFOLD PRESSURE:1",      "inHg",             lambda v: round(float(v), 1)),
    "fuel_flow":         ("ENG FUEL FLOW GPH:1",          "Gallons per hour", lambda v: round(float(v), 1)),
    "oil_temp":          ("ENG OIL TEMPERATURE:1",        "Fahrenheit",       lambda v: round(float(v), 0)),
    "oil_pressure":      ("ENG OIL PRESSURE:1",           "Pounds per square inch", lambda v: round(float(v), 0)),
    "egt":               ("ENG EXHAUST GAS TEMPERATURE:1","Fahrenheit",       lambda v: round(float(v), 0)),
    # Jet — engine 1
    "n1_1":        ("ENG N1 RPM:1",              "Percent",            lambda v: round(float(v), 1)),
    "n2_1":        ("ENG N2 RPM:1",              "Percent",            lambda v: round(float(v), 1)),
    "egt_1":       ("ENG EXHAUST GAS TEMPERATURE:1", "Celsius",        lambda v: round(float(v), 0)),
    "ff_1":        ("ENG FUEL FLOW PPH:1",        "Pounds per hour",   lambda v: round(float(v), 0)),
    "oil_temp_1":  ("ENG OIL TEMPERATURE:1",      "Celsius",           lambda v: round(float(v), 0)),
    "oil_press_1": ("ENG OIL PRESSURE:1",         "Pounds per square inch", lambda v: round(float(v), 0)),
    "vib_1":       ("ENG VIBRATION:1",            "Number",            lambda v: round(float(v), 1)),
    # Jet — engine 2
    "n1_2":        ("ENG N1 RPM:2",              "Percent",            lambda v: round(float(v), 1)),
    "n2_2":        ("ENG N2 RPM:2",              "Percent",            lambda v: round(float(v), 1)),
    "egt_2":       ("ENG EXHAUST GAS TEMPERATURE:2", "Celsius",        lambda v: round(float(v), 0)),
    "ff_2":        ("ENG FUEL FLOW PPH:2",        "Pounds per hour",   lambda v: round(float(v), 0)),
    "oil_temp_2":  ("ENG OIL TEMPERATURE:2",      "Celsius",           lambda v: round(float(v), 0)),
    "oil_press_2": ("ENG OIL PRESSURE:2",         "Pounds per square inch", lambda v: round(float(v), 0)),
    "vib_2":       ("ENG VIBRATION:2",            "Number",            lambda v: round(float(v), 1)),
    # Fuel (weight in kg — adjust to Pounds/Gallons if your aircraft differs)
    "fuel_left":   ("FUEL TANK LEFT MAIN QUANTITY",  "Kilograms", lambda v: round(float(v), 0)),
    "fuel_right":  ("FUEL TANK RIGHT MAIN QUANTITY", "Kilograms", lambda v: round(float(v), 0)),
    "fuel_total":  ("FUEL TOTAL QUANTITY WEIGHT",    "Kilograms", lambda v: round(float(v), 0)),
}

# Build deduplicated subscription spec (some keys share vars, or have a list of vars)
_SC_SPEC: list[dict] = []
_sc_vars_seen: set[str] = set()
for _our_key, (_sc_var, _units, _) in SIMVAR_MAP.items():
    for _v in _sc_var if isinstance(_sc_var, list) else [_sc_var]:
        if _v not in _sc_vars_seen:
            _sc_vars_seen.add(_v)
            _SC_SPEC.append({"name": _v, "units": _units})


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_gauge_keys = {k for k, v in SIMVARS.items() if v.get("type") == "gauge"}
_sim_state: dict = {k: (0 if k in _gauge_keys else False) for k in SIMVARS}

# EICAS defaults — held until SimConnect provides real values
_sim_state.update({
    "rpm": 0.0, "manifold_pressure": 0.0, "fuel_flow": 0.0,
    "oil_temp": 0.0, "oil_pressure": 0.0, "egt": 0.0,
    "n1_1": 0.0, "n1_2": 0.0, "n2_1": 0.0, "n2_2": 0.0,
    "egt_1": 0.0, "egt_2": 0.0, "ff_1": 0.0, "ff_2": 0.0,
    "oil_temp_1": 0.0, "oil_temp_2": 0.0,
    "oil_press_1": 0.0, "oil_press_2": 0.0,
    "vib_1": 0.0, "vib_2": 0.0,
    "fuel_left": 0.0, "fuel_right": 0.0, "fuel_total": 0.0,
})

_state_lock = threading.Lock()
_connected_clients: set = set()
SC_POLL_INTERVAL_S = 0.1


# ---------------------------------------------------------------------------
# SimConnect polling thread
# ---------------------------------------------------------------------------


def _poll_simconnect(
    stop_event: threading.Event, dll_path: str, debug: bool = False
) -> None:
    from simconnect import SimConnect
    from simconnect.datadef import DataDefinition
    from simconnect.scdefs import PERIOD_SIM_FRAME

    while not stop_event.is_set():
        try:
            print("[SC] Connecting to SimConnect…")
            # Clear cached data definitions so they get re-registered on reconnect
            DataDefinition._instances.clear()

            with SimConnect(dll_path=dll_path) as sc:
                # Request data every sim frame; WS broadcast loop throttles outbound rate.
                dd = sc.subscribe_simdata(_SC_SPEC, period=PERIOD_SIM_FRAME)
                print("[SC] Connected. Polling…")

                while not stop_event.is_set():
                    sc.receive(timeout_seconds=SC_POLL_INTERVAL_S)

                    if not dd.simdata:
                        continue

                    updates: dict = {}
                    for our_key, (sc_var, _, transform) in SIMVAR_MAP.items():
                        sc_vars = sc_var if isinstance(sc_var, list) else [sc_var]
                        try:
                            vals = [
                                transform(dd.simdata[v])
                                for v in sc_vars
                                if dd.simdata.get(v) is not None
                            ]
                            # Drop nan/inf — SimConnect returns these for
                            # variables not supported by the current aircraft.
                            vals = [
                                v for v in vals
                                if not (isinstance(v, float) and not math.isfinite(v))
                            ]
                        except Exception:
                            vals = []
                        if not vals:
                            continue
                        updates[our_key] = any(vals) if len(sc_vars) > 1 else vals[0]

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


def _safe_json(state: dict) -> str:
    """Serialize state, replacing nan/inf with 0 (SimConnect occasionally returns these)."""
    clean = {
        k: (0 if isinstance(v, float) and not math.isfinite(v) else v)
        for k, v in state.items()
    }
    return json.dumps(clean)


async def _ws_handler(websocket) -> None:
    _connected_clients.add(websocket)
    print(f"[WS] Client connected: {websocket.remote_address}")
    try:
        with _state_lock:
            snapshot = dict(_sim_state)
        await websocket.send(_safe_json(snapshot))
        await websocket.wait_closed()
    finally:
        _connected_clients.discard(websocket)
        print(f"[WS] Client disconnected: {websocket.remote_address}")


async def _broadcast_loop() -> None:
    while True:
        await asyncio.sleep(0.1)
        if not _connected_clients:
            continue
        try:
            with _state_lock:
                msg = _safe_json(_sim_state)
            await asyncio.gather(
                *[ws.send(msg) for ws in list(_connected_clients)],
                return_exceptions=True,
            )
        except Exception as exc:
            print(f"[WS] Broadcast error: {exc!r}")


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
        print(f"     Point display.py WS_HOST to this machine's IP address.")  # noqa: F541
        asyncio.create_task(_broadcast_loop())
        await stop_future

    sc_stop.set()
    print("[WS] Server stopped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SimConnect → WebSocket proxy")
    parser.add_argument(
        "--host", default="0.0.0.0", help="WebSocket listen address (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=WS_PORT, help=f"WebSocket port (default: {WS_PORT})"
    )
    parser.add_argument(
        "--dll",
        default=SC_DLL_PATH,
        help=f"Path to SimConnect.dll (default: {SC_DLL_PATH})",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Print every SimConnect update to stdout"
    )
    args = parser.parse_args()

    asyncio.run(_main(args.host, args.port, args.dll, args.debug))
