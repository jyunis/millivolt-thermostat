"""Single-channel relay control on GPIO17."""

import os

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        import RPi.GPIO as GPIO
    except Exception:
        SIMULATE = True

RELAY_PIN = int(os.getenv("RELAY_PIN", "17"))


class Relay:
    def __init__(self):
        self.simulate = SIMULATE
        self._on = False
        if not self.simulate:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)

    @property
    def is_on(self) -> bool:
        return self._on

    def on(self):
        if not self._on:
            self._on = True
            if not self.simulate:
                GPIO.output(RELAY_PIN, GPIO.HIGH)

    def off(self):
        if self._on:
            self._on = False
            if not self.simulate:
                GPIO.output(RELAY_PIN, GPIO.LOW)

    def cleanup(self):
        if not self.simulate:
            GPIO.cleanup(RELAY_PIN)
