"""
pi_display.py — Pi driver for a single ST7789V2 display (SPI0)
Replaces the tkinter preview. Renders left or right panel via
the existing render_panel() and pushes frames to the physical screen.
"""

import asyncio
import json
import threading
import time

import st7789
import RPi.GPIO as GPIO
from PIL import Image

from config import (
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    SIMVARS,
    WS_HOST,
    WS_PORT,
)
from display import render_panel

# ---------------------------------------------------------------------------
# Which panel this Pi drives — change to "right" for the other display
# ---------------------------------------------------------------------------
PANEL_SIDE = "left"

# ---------------------------------------------------------------------------
# SPI0 pin config (BCM numbering)
# ---------------------------------------------------------------------------
DC_PIN = 25
RST_PIN = 27
BL_PIN = 13


# ---------------------------------------------------------------------------
# Display init
# ---------------------------------------------------------------------------
def init_display() -> st7789.ST7789:
    disp = st7789.ST7789(
        port=0,
        cs=0,
        dc=DC_PIN,
        rst=RST_PIN,
        backlight=BL_PIN,
        width=DISPLAY_WIDTH,
        height=DISPLAY_HEIGHT,
        rotation=90,
        spi_speed_hz=40_000_000,
    )
    return disp


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
class PiDisplay:
    def __init__(self) -> None:
        self._sim_data: dict = {key: False for key in SIMVARS}
        self._flash = False
        self._lock = threading.Lock()
        self._dirty = threading.Event()

        self.disp = init_display()
        print("[DISP] Display initialized")

        blank = Image.new("RGB", (DISPLAY_WIDTH, DISPLAY_HEIGHT), "#111111")
        self.disp.display(blank)

    def _push_frame(self) -> None:
        with self._lock:
            data = dict(self._sim_data)
            flash = self._flash
        img = render_panel(data, PANEL_SIDE, flash)
        self.disp.display(img)

    def _flash_ticker(self) -> None:
        while True:
            time.sleep(0.5)
            with self._lock:
                self._flash = not self._flash
            self._dirty.set()

    def _render_loop(self) -> None:
        while True:
            self._dirty.wait()
            self._dirty.clear()
            self._push_frame()

    def _run_ws_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._ws_client())

    async def _ws_client(self) -> None:
        from websockets.asyncio.client import connect

        uri = f"ws://{WS_HOST}:{WS_PORT}"
        while True:
            try:
                async with connect(uri) as ws:
                    print(f"[WS] Connected to {uri}")
                    async for raw in ws:
                        data = json.loads(raw)
                        with self._lock:
                            self._sim_data.update(data)
                        self._dirty.set()
            except Exception as exc:
                print(f"[WS] {exc!r} — reconnecting in 2s")
                await asyncio.sleep(2)

    def run(self) -> None:
        threading.Thread(target=self._flash_ticker, daemon=True).start()
        threading.Thread(target=self._render_loop, daemon=True).start()
        threading.Thread(target=self._run_ws_thread, daemon=True).start()

        print(f"[DISP] Running panel='{PANEL_SIDE}', ws={WS_HOST}:{WS_PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("[DISP] Shutting down")
            GPIO.cleanup()


if __name__ == "__main__":
    PiDisplay().run()
