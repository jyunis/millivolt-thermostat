"""Single-channel relay control on GPIO17. Active-low relay module."""

import logging
import os

log = logging.getLogger("thermostat.relay")

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        import lgpio
    except Exception:
        SIMULATE = True

RELAY_PIN = int(os.getenv("RELAY_PIN", "17"))


class Relay:
    def __init__(self):
        self.simulate = SIMULATE
        self._on = False
        self._h = None
        if not self.simulate:
            self._h = lgpio.gpiochip_open(0)
            lgpio.gpio_claim_output(self._h, RELAY_PIN, 1)  # HIGH = relay off

    @property
    def is_on(self) -> bool:
        return self._on

    def on(self):
        if not self._on:
            self._on = True
            log.info("HEAT ON")
            if not self.simulate:
                lgpio.gpio_write(self._h, RELAY_PIN, 0)  # LOW = relay on

    def off(self):
        if self._on:
            self._on = False
            log.info("HEAT OFF")
            if not self.simulate:
                lgpio.gpio_write(self._h, RELAY_PIN, 1)  # HIGH = relay off

    def cleanup(self):
        if not self.simulate and self._h is not None:
            lgpio.gpio_write(self._h, RELAY_PIN, 1)  # ensure off on shutdown
            lgpio.gpiochip_close(self._h)
            self._h = None
