"""DS18B20 temperature sensor via 1-Wire."""

import os

SIMULATE = os.getenv("THERMOSTAT_SIMULATE", "0") == "1"

if not SIMULATE:
    try:
        from w1thermsensor import W1ThermSensor, Unit
    except Exception:
        SIMULATE = True

_sim_temp_f = 65.0


class TempSensor:
    def __init__(self):
        self.simulate = SIMULATE
        self._sensor = None
        if not self.simulate:
            self._sensor = W1ThermSensor()

    def read_f(self) -> float:
        """Read current temperature in Fahrenheit."""
        if self.simulate:
            return _sim_temp_f
        return self._sensor.get_temperature(Unit.DEGREES_F)
