import asyncio
import json
import random
import signal

from websockets.asyncio.server import serve

from config import WS_HOST, WS_PORT, SIMVARS

_gauge_keys = {k for k, v in SIMVARS.items() if v.get("type") == "gauge"}
sim_state = {k: (0 if k in _gauge_keys else False) for k in SIMVARS}
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
        await asyncio.sleep(0.5)
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
    loop.add_signal_handler(signal.SIGINT, shutdown)
    loop.add_signal_handler(signal.SIGTERM, shutdown)

    async with serve(handler, WS_HOST, WS_PORT):
        print(f"Mock server listening on ws://{WS_HOST}:{WS_PORT}")
        asyncio.create_task(broadcast_loop())
        asyncio.create_task(random_toggle_loop())
        asyncio.create_task(gauge_step_loop())
        await stop

    print("Server stopped.")


if __name__ == "__main__":
    asyncio.run(main())
