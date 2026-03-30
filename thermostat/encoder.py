"""EC11 rotary encoder with push button on GPIO23/24/25."""

import os
import threading
import time

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        import lgpio
    except Exception:
        SIMULATE = True

CLK_PIN = int(os.getenv("ENCODER_CLK_PIN", "23"))
DT_PIN = int(os.getenv("ENCODER_DT_PIN", "24"))
SW_PIN = int(os.getenv("ENCODER_SW_PIN", "25"))

POLL_INTERVAL = 0.002  # 2ms polling — fast enough to catch detents


class RotaryEncoder:
    """Tracks rotation and button presses via polling."""

    def __init__(self, on_rotate=None, on_press=None):
        self.simulate = SIMULATE
        self._on_rotate = on_rotate
        self._on_press = on_press
        self._running = False
        self._h = None
        self._thread = None

        if not self.simulate:
            self._h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_input(self._h, CLK_PIN, lgpio.SET_PULL_UP)
            lgpio.gpio_claim_input(self._h, DT_PIN, lgpio.SET_PULL_UP)
            lgpio.gpio_claim_input(self._h, SW_PIN, lgpio.SET_PULL_UP)

            self._running = True
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def _poll_loop(self):
        last_clk = lgpio.gpio_read(self._h, CLK_PIN)
        last_sw = lgpio.gpio_read(self._h, SW_PIN)

        while self._running:
            clk = lgpio.gpio_read(self._h, CLK_PIN)
            sw = lgpio.gpio_read(self._h, SW_PIN)

            # Rotation: detect falling edge on CLK, read DT for direction
            if clk == 0 and last_clk == 1:
                dt = lgpio.gpio_read(self._h, DT_PIN)
                direction = -1 if dt == 1 else 1
                if self._on_rotate:
                    self._on_rotate(direction)

            # Button: detect falling edge on SW
            if sw == 0 and last_sw == 1:
                if self._on_press:
                    self._on_press()

            last_clk = clk
            last_sw = sw
            time.sleep(POLL_INTERVAL)

    def cleanup(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1)
        if not self.simulate and self._h is not None:
            lgpio.gpiochip_close(self._h)
            self._h = None
