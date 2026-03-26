"""EC11 rotary encoder with push button on GPIO23/24/25."""

import os
import threading

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        import RPi.GPIO as GPIO
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

        if not self.simulate:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(CLK_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(DT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.setup(SW_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            self._last_clk = GPIO.input(CLK_PIN)

            GPIO.add_event_detect(CLK_PIN, GPIO.FALLING,
                                  callback=self._rotation_callback, bouncetime=50)
            GPIO.add_event_detect(SW_PIN, GPIO.FALLING,
                                  callback=self._button_callback, bouncetime=300)

    def _rotation_callback(self, channel):
        with self._lock:
            clk = GPIO.input(CLK_PIN)
            dt = GPIO.input(DT_PIN)
            if clk != self._last_clk:
                direction = 1 if dt != clk else -1
                if self._on_rotate:
                    self._on_rotate(direction)
            self._last_clk = clk

    def _button_callback(self, channel):
        if self._on_press:
            self._on_press()

    def cleanup(self):
        if not self.simulate:
            GPIO.remove_event_detect(CLK_PIN)
            GPIO.remove_event_detect(SW_PIN)
            GPIO.cleanup([CLK_PIN, DT_PIN, SW_PIN])
