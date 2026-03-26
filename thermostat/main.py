#!/usr/bin/env python3
"""Millivolt thermostat controller — Raspberry Pi Zero 2W.

Reads DS18B20 temperature sensor, drives relay for millivolt furnace,
displays status on SH1106 OLED, and accepts setpoint changes via rotary encoder.
"""

import json
import math
import os
import signal
import sys
import time

from encoder import RotaryEncoder
from oled import OledDisplay
from relay import Relay
from sensor import TempSensor

HYSTERESIS = float(os.getenv("HYSTERESIS", "1.0"))
DEFAULT_SETPOINT = float(os.getenv("DEFAULT_SETPOINT", "68.0"))
LOOP_INTERVAL = float(os.getenv("LOOP_INTERVAL", "5.0"))
SETPOINT_MIN = 50.0
SETPOINT_MAX = 85.0
STATE_FILE = os.getenv("STATE_FILE", "/var/lib/thermostat/state.json")

# Safety limits
SENSOR_FAIL_LIMIT = int(os.getenv("SENSOR_FAIL_LIMIT", "6"))       # consecutive bad reads before relay kill
TEMP_PLAUSIBLE_MIN = float(os.getenv("TEMP_PLAUSIBLE_MIN", "-20"))  # below this = sensor fault
TEMP_PLAUSIBLE_MAX = float(os.getenv("TEMP_PLAUSIBLE_MAX", "120"))  # above this = sensor fault
MAX_RUN_SECONDS = float(os.getenv("MAX_RUN_SECONDS", "10800"))      # 3 hours continuous max


def load_setpoint() -> float:
    """Load persisted setpoint, or return default."""
    try:
        with open(STATE_FILE) as f:
            val = float(json.load(f).get("setpoint", DEFAULT_SETPOINT))
            if math.isfinite(val):
                return val
            return DEFAULT_SETPOINT
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return DEFAULT_SETPOINT


def save_setpoint(setpoint: float):
    """Persist setpoint to disk so it survives reboots."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"setpoint": setpoint}, f)
    except OSError:
        pass


class Thermostat:
    def __init__(self):
        self.setpoint = load_setpoint()
        self.sensor = TempSensor()
        self.relay = Relay()
        self.oled = OledDisplay()
        self.encoder = RotaryEncoder(
            on_rotate=self._on_rotate,
            on_press=self._on_press,
        )
        self._running = True
        self._consecutive_failures = 0
        self._relay_on_since = None  # timestamp when relay last turned on
        self._fault = False          # latched fault state — relay locked off

    def _on_rotate(self, direction: int):
        """Encoder twist: adjust setpoint by 1F."""
        new = self.setpoint + direction
        self.setpoint = max(SETPOINT_MIN, min(SETPOINT_MAX, new))
        save_setpoint(self.setpoint)

    def _on_press(self):
        """Encoder press: currently unused, reserved for future modes."""
        pass

    def _is_plausible(self, temp_f: float) -> bool:
        """Reject non-finite or out-of-range readings as sensor faults."""
        return math.isfinite(temp_f) and TEMP_PLAUSIBLE_MIN <= temp_f <= TEMP_PLAUSIBLE_MAX

    def _record_failure(self, reason: str):
        """Track consecutive bad reads; trip fault after SENSOR_FAIL_LIMIT."""
        self._consecutive_failures += 1
        print(f"thermostat: bad read #{self._consecutive_failures}: {reason}", flush=True)
        if self._consecutive_failures >= SENSOR_FAIL_LIMIT:
            self._trip_fault(f"sensor failed {self._consecutive_failures} times in a row")

    def _trip_fault(self, reason: str):
        """Latch into fault state — relay off, stays off until restart."""
        if not self._fault:
            self._fault = True
            self.relay.off()
            self._relay_on_since = None
            print(f"thermostat: FAULT — {reason}. Relay locked off.", flush=True)

    def clear_fault(self):
        """Reset fault state (for testing or future remote reset)."""
        self._fault = False
        self._consecutive_failures = 0

    def _control(self, current_f: float):
        """Hysteresis control with safety checks."""
        if self._fault:
            return

        # Plausibility check
        if not self._is_plausible(current_f):
            self._record_failure(f"implausible reading: {current_f}")
            return

        # Good read — reset failure counter
        self._consecutive_failures = 0

        # Max run time check
        if self.relay.is_on and self._relay_on_since is not None:
            elapsed = time.monotonic() - self._relay_on_since
            if elapsed >= MAX_RUN_SECONDS:
                self._trip_fault(
                    f"relay on for {elapsed / 3600:.1f}h (limit {MAX_RUN_SECONDS / 3600:.1f}h)"
                )
                return

        # Hysteresis control
        if not self.relay.is_on and current_f < self.setpoint - HYSTERESIS:
            self.relay.on()
            self._relay_on_since = time.monotonic()
        elif self.relay.is_on and current_f > self.setpoint + HYSTERESIS:
            self.relay.off()
            self._relay_on_since = None

    def run(self):
        print("thermostat: starting", flush=True)
        while self._running:
            try:
                current_f = self.sensor.read_f()
                self._control(current_f)
                self.oled.render(current_f, self.setpoint, self.relay.is_on)
            except Exception as e:
                self._record_failure(f"exception: {e}")
            time.sleep(LOOP_INTERVAL)

    def shutdown(self):
        print("thermostat: shutting down", flush=True)
        self._running = False
        self.relay.off()
        self.oled.off()
        self.relay.cleanup()
        self.encoder.cleanup()


def main():
    thermostat = Thermostat()

    def handle_signal(sig, frame):
        thermostat.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    thermostat.run()


if __name__ == "__main__":
    main()
