import asyncio
import json
import math
import random
import signal
import time

from websockets.asyncio.server import serve

from config import WS_HOST, WS_PORT, SIMVARS

_gauge_keys = {k for k, v in SIMVARS.items() if v.get("type") == "gauge"}
sim_state = {k: (0 if k in _gauge_keys else False) for k in SIMVARS}

# PFD defaults — updated each broadcast by _pfd_animate()
_PFD_DEFAULTS: dict = {
    "airspeed":          150.0,
    "ground_speed":      181.0,
    "altitude":          3000.0,
    "radio_alt":         2400.0,
    "vertical_speed":    -800.0,
    "baro_setting":      "29.92",
    "pitch":             -2.5,
    "bank":              5.0,
    "heading":           320.0,
    "fd_pitch":          -2.5,
    "fd_bank":           5.0,
    "autopilot_active":  True,
    "at_active":         True,
    "lnav_active":       True,
    "vnav_active":       True,
    "app_active":        False,
    "loc_active":        False,
    "selected_speed":    148.0,
    "selected_altitude": 2000.0,
    "selected_heading":  315.0,
    "flaps":             5,
}
sim_state.update(_PFD_DEFAULTS)

# EICAS defaults
_EICAS_DEFAULTS: dict = {
    # GA / piston
    "rpm":               2300.0,
    "manifold_pressure": 24.5,
    "fuel_flow":         11.2,
    "oil_temp":          195.0,
    "oil_pressure":      72.0,
    "egt":               1320.0,
    # Jet — both engines
    "n1_1":        84.2,  "n1_2":        84.0,
    "n2_1":        92.1,  "n2_2":        92.0,
    "egt_1":       620.0, "egt_2":       615.0,
    "ff_1":        2640.0,"ff_2":        2620.0,
    "oil_temp_1":  85.0,  "oil_temp_2":  83.0,
    "oil_press_1": 62.0,  "oil_press_2": 61.0,
    "vib_1":       0.8,   "vib_2":       0.7,
    # Fuel
    "fuel_left":   8400.0,
    "fuel_right":  8350.0,
    "fuel_total":  16750.0,
}
sim_state.update(_EICAS_DEFAULTS)


def _eicas_animate() -> None:
    """Update EICAS vars with sinusoidal motion to simulate live engine data."""
    t = time.monotonic()
    sim_state["rpm"]               = 2300 + 30  * math.sin(t * 0.3)
    sim_state["manifold_pressure"] = 24.5 + 0.3 * math.sin(t * 0.2)
    sim_state["fuel_flow"]         = 11.2 + 0.2 * math.sin(t * 0.4)
    sim_state["oil_temp"]          = 195  + 2   * math.sin(t * 0.1)
    sim_state["oil_pressure"]      = 72   + 1   * math.sin(t * 0.15)
    sim_state["egt"]               = 1320 + 20  * math.sin(t * 0.25)
    sim_state["n1_1"]   = 84.2 + 0.4 * math.sin(t * 0.30)
    sim_state["n1_2"]   = 84.0 + 0.4 * math.sin(t * 0.28)
    sim_state["n2_1"]   = 92.1 + 0.3 * math.sin(t * 0.22)
    sim_state["n2_2"]   = 92.0 + 0.3 * math.sin(t * 0.24)
    sim_state["egt_1"]  = 620  + 8   * math.sin(t * 0.18)
    sim_state["egt_2"]  = 615  + 8   * math.sin(t * 0.20)
    sim_state["ff_1"]   = 2640 + 30  * math.sin(t * 0.35)
    sim_state["ff_2"]   = 2620 + 30  * math.sin(t * 0.33)
    sim_state["oil_temp_1"]  = 85 + math.sin(t * 0.12)
    sim_state["oil_temp_2"]  = 83 + math.sin(t * 0.11)
    sim_state["oil_press_1"] = 62 + 0.5 * math.sin(t * 0.17)
    sim_state["oil_press_2"] = 61 + 0.5 * math.sin(t * 0.19)
    sim_state["vib_1"]  = 0.8 + 0.1 * math.sin(t * 0.7)
    sim_state["vib_2"]  = 0.7 + 0.1 * math.sin(t * 0.65)
    # Fuel burns down slowly
    sim_state["fuel_left"]  = max(0.0, 8400 - t * 0.05)
    sim_state["fuel_right"] = max(0.0, 8350 - t * 0.05)
    sim_state["fuel_total"] = sim_state["fuel_left"] + sim_state["fuel_right"]


def _pfd_animate() -> None:
    """Update PFD vars with sinusoidal motion to simulate live flight."""
    t = time.monotonic()
    sim_state["pitch"]        = -2.5 + 3.0  * math.sin(t * 0.3)
    sim_state["bank"]         =  5.0 + 10.0 * math.sin(t * 0.2)
    sim_state["heading"]      = (320 + 5 * math.sin(t * 0.15)) % 360
    sim_state["airspeed"]     = 150 + 10 * math.sin(t * 0.25)
    sim_state["altitude"]     = max(200.0, 3000 - (t % 3600) * 0.22)
    sim_state["radio_alt"]    = max(0.0, sim_state["altitude"] - 600)
    sim_state["fd_pitch"]     = sim_state["pitch"] + 0.5 * math.sin(t * 0.4)
    sim_state["fd_bank"]      = sim_state["bank"]  + 1.0 * math.sin(t * 0.35)
    sim_state["ground_speed"] = 181 + 3 * math.sin(t * 0.18)
connected_clients: set = set()


async def handler(websocket):
    connected_clients.add(websocket)
    print(f"[+] Client connected: {websocket.remote_address}")
    try:
        await websocket.send(json.dumps(sim_state))
        await websocket.wait_closed()
    finally:
        connected_clients.discard(websocket)
        print(f"[-] Client disconnected: {websocket.remote_address}")


async def broadcast_loop():
    while True:
        await asyncio.sleep(0.1)
        _pfd_animate()
        _eicas_animate()
        if not connected_clients:
            continue
        msg = json.dumps(sim_state)
        results = await asyncio.gather(
            *[ws.send(msg) for ws in list(connected_clients)],
            return_exceptions=True,
        )
        active = sum(1 for r in results if r is None)
        on_vars = [k for k, v in sim_state.items() if v]
        print(f"Broadcast → {active} client(s) | ON: {on_vars}")


async def random_toggle_loop():
    bool_keys = [k for k in sim_state if k not in _gauge_keys]
    while True:
        await asyncio.sleep(random.uniform(3, 6))
        to_toggle = random.sample(bool_keys, min(random.randint(1, 3), len(bool_keys)))
        for key in to_toggle:
            sim_state[key] = not sim_state[key]
        print(f"Toggled: {to_toggle}")


async def gauge_step_loop():
    while True:
        await asyncio.sleep(random.uniform(2, 4))
        for key in _gauge_keys:
            cfg = SIMVARS[key]
            max_val = cfg["max"]
            step = max_val / 5
            delta = random.choice([-step, 0, step])
            sim_state[key] = max(0, min(max_val, sim_state[key] + delta))
        print(f"Gauges: { {k: sim_state[k] for k in _gauge_keys} }")


async def main():
    stop = asyncio.get_event_loop().create_future()

    def shutdown():
        if not stop.done():
            stop.set_result(None)

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, shutdown)
        loop.add_signal_handler(signal.SIGTERM, shutdown)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(shutdown))

    async with serve(handler, WS_HOST, WS_PORT):
        print(f"Mock server listening on ws://{WS_HOST}:{WS_PORT}")
        asyncio.create_task(broadcast_loop())
        asyncio.create_task(random_toggle_loop())
        asyncio.create_task(gauge_step_loop())
        await stop

    print("Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
