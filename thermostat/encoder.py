"""EC11 rotary encoder with push button on GPIO23/24/25."""

import os
import threading

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        import lgpio
    except Exception:
        SIMULATE = True

CLK_PIN = int(os.getenv("ENCODER_CLK_PIN", "23"))
DT_PIN = int(os.getenv("ENCODER_DT_PIN", "24"))
SW_PIN = int(os.getenv("ENCODER_SW_PIN", "25"))


class RotaryEncoder:
    """Tracks rotation and button presses via GPIO interrupts."""

    def __init__(self, on_rotate=None, on_press=None):
        self.simulate = SIMULATE
        self._on_rotate = on_rotate  # callback(direction: int)  +1 or -1
        self._on_press = on_press    # callback()
        self._lock = threading.Lock()
        self._h = None

        if not self.simulate:
            self._h = lgpio.gpiochip_open(0)

            # CLK and SW use alert (includes input claim); DT is read-only
            lgpio.gpio_claim_alert(self._h, CLK_PIN, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
            lgpio.gpio_claim_input(self._h, DT_PIN, lgpio.SET_PULL_UP)
            lgpio.gpio_claim_alert(self._h, SW_PIN, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)

            lgpio.gpio_set_debounce_micros(self._h, CLK_PIN, 50_000)
            lgpio.gpio_set_debounce_micros(self._h, SW_PIN, 300_000)

            self._last_clk = lgpio.gpio_read(self._h, CLK_PIN)

            self._cb_clk = lgpio.callback(self._h, CLK_PIN,
                                          lgpio.FALLING_EDGE,
                                          self._rotation_callback)
            self._cb_sw = lgpio.callback(self._h, SW_PIN,
                                         lgpio.FALLING_EDGE,
                                         self._button_callback)

    def _rotation_callback(self, chip, gpio, level, timestamp):
        with self._lock:
            clk = lgpio.gpio_read(self._h, CLK_PIN)
            dt = lgpio.gpio_read(self._h, DT_PIN)
            if clk != self._last_clk:
                direction = 1 if dt != clk else -1
                if self._on_rotate:
                    self._on_rotate(direction)
            self._last_clk = clk

    def _button_callback(self, chip, gpio, level, timestamp):
        if self._on_press:
            self._on_press()

    def cleanup(self):
        if not self.simulate and self._h is not None:
            self._cb_clk.cancel()
            self._cb_sw.cancel()
            lgpio.gpiochip_close(self._h)
            self._h = None
