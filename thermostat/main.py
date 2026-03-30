#!/usr/bin/env python3
"""Millivolt thermostat controller — Raspberry Pi Zero 2W.

Reads DS18B20 temperature sensor, drives relay for millivolt furnace,
displays status on SH1106 OLED, and accepts setpoint changes via rotary encoder.

Runs a FastAPI server for web UI and API access over Tailscale.
"""

import asyncio
import json
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

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
PORT = int(os.getenv("PORT", "5000"))

# Safety limits
SENSOR_FAIL_LIMIT = int(os.getenv("SENSOR_FAIL_LIMIT", "6"))
TEMP_PLAUSIBLE_MIN = float(os.getenv("TEMP_PLAUSIBLE_MIN", "-20"))
TEMP_PLAUSIBLE_MAX = float(os.getenv("TEMP_PLAUSIBLE_MAX", "120"))
MAX_RUN_SECONDS = float(os.getenv("MAX_RUN_SECONDS", "10800"))
FAILSAFE_HOUR = int(os.getenv("FAILSAFE_HOUR", "18"))           # 6 PM local
FAILSAFE_SETPOINT = float(os.getenv("FAILSAFE_SETPOINT", "45"))


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
        self._relay_on_since = None
        self._fault = False
        self._current_f = None  # last good reading
        self._loop = None       # asyncio event loop, set in run()
        self._wake = None       # asyncio.Event, signals immediate cycle
        self._last_interaction = None  # timestamp of last API/encoder setpoint change
        self._failsafe_active = False  # true when nightly setback is in effect

        # SSE subscribers — set of asyncio.Queue
        self._subscribers: set[asyncio.Queue] = set()

        # Encoder settle: delay control loop wake until user stops twisting
        self._settle_timer: threading.Timer | None = None
        self._settle_delay = 2.0  # seconds

    def _on_rotate(self, direction: int):
        """Encoder twist: adjust setpoint by 1F. Wakes control loop after settling."""
        new = self.setpoint + direction
        self.setpoint = max(SETPOINT_MIN, min(SETPOINT_MAX, new))
        save_setpoint(self.setpoint)
        self._last_interaction = datetime.now()
        self._failsafe_active = False
        # Immediate OLED feedback from the GPIO thread
        if self._current_f is not None:
            self.oled.render(self._current_f, self.setpoint, self.relay.is_on, self._fault)
        # Reset settle timer — only wake control loop after user stops twisting
        if self._settle_timer is not None:
            self._settle_timer.cancel()
        self._settle_timer = threading.Timer(self._settle_delay, self._wake_loop)
        self._settle_timer.start()

    def _on_press(self):
        """Encoder press: currently unused, reserved for future modes."""
        pass

    def _wake_loop(self):
        """Thread-safe wake of the async control loop."""
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    def set_setpoint(self, value: float):
        """Set setpoint from API. Clamps to valid range."""
        self.setpoint = max(SETPOINT_MIN, min(SETPOINT_MAX, value))
        save_setpoint(self.setpoint)
        self._last_interaction = datetime.now()
        self._failsafe_active = False
        if self._current_f is not None:
            self.oled.render(self._current_f, self.setpoint, self.relay.is_on, self._fault)
        self._wake_loop()

    def _is_plausible(self, temp_f: float) -> bool:
        return math.isfinite(temp_f) and TEMP_PLAUSIBLE_MIN <= temp_f <= TEMP_PLAUSIBLE_MAX

    def _record_failure(self, reason: str):
        self._consecutive_failures += 1
        print(f"thermostat: bad read #{self._consecutive_failures}: {reason}", flush=True)
        if self._consecutive_failures >= SENSOR_FAIL_LIMIT:
            self._trip_fault(f"sensor failed {self._consecutive_failures} times in a row")

    def _trip_fault(self, reason: str):
        if not self._fault:
            self._fault = True
            self.relay.off()
            self._relay_on_since = None
            print(f"thermostat: FAULT — {reason}. Relay locked off.", flush=True)

    def clear_fault(self):
        self._fault = False
        self._consecutive_failures = 0
        self._wake_loop()

    def _check_failsafe(self):
        """Nightly setback: if past FAILSAFE_HOUR and no interaction since
        today's cutoff, force setpoint to FAILSAFE_SETPOINT. Any API call
        or encoder twist resets this."""
        now = datetime.now()
        if now.hour < FAILSAFE_HOUR:
            return
        cutoff = now.replace(hour=FAILSAFE_HOUR, minute=0, second=0, microsecond=0)
        if self._last_interaction is not None and self._last_interaction >= cutoff:
            return
        # Past cutoff with no recent interaction — apply setback
        if not self._failsafe_active:
            self._failsafe_active = True
            self.setpoint = FAILSAFE_SETPOINT
            save_setpoint(self.setpoint)
            print(f"thermostat: nightly failsafe — setpoint forced to {FAILSAFE_SETPOINT}F", flush=True)

    def _control(self, current_f: float):
        """Hysteresis control with safety checks."""
        if self._fault:
            return

        self._check_failsafe()

        if not self._is_plausible(current_f):
            self._record_failure(f"implausible reading: {current_f}")
            return

        self._consecutive_failures = 0

        if self.relay.is_on and self._relay_on_since is not None:
            elapsed = time.monotonic() - self._relay_on_since
            if elapsed >= MAX_RUN_SECONDS:
                self._trip_fault(
                    f"relay on for {elapsed / 3600:.1f}h (limit {MAX_RUN_SECONDS / 3600:.1f}h)"
                )
                return

        if not self.relay.is_on and current_f < self.setpoint - HYSTERESIS:
            self.relay.on()
            self._relay_on_since = time.monotonic()
        elif self.relay.is_on and current_f > self.setpoint + HYSTERESIS:
            self.relay.off()
            self._relay_on_since = None

    def status(self) -> dict:
        """Current state as a JSON-serializable dict."""
        return {
            "current_f": round(self._current_f, 1) if self._current_f is not None else None,
            "setpoint_f": round(self.setpoint, 1),
            "heating": self.relay.is_on,
            "fault": self._fault,
            "failsafe_active": self._failsafe_active,
            "consecutive_failures": self._consecutive_failures,
        }

    def _notify_subscribers(self):
        """Push current status to all SSE subscribers."""
        data = json.dumps(self.status())
        dead = set()
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.add(q)
        self._subscribers -= dead

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        print("thermostat: starting", flush=True)
        while self._running:
            try:
                current_f = await self._loop.run_in_executor(None, self.sensor.read_f)
                self._current_f = current_f
                self._control(current_f)
                self.oled.render(current_f, self.setpoint, self.relay.is_on, self._fault)
            except Exception as e:
                self._record_failure(f"exception: {e}")
            self._notify_subscribers()
            # Sleep until interval OR until woken by setpoint change
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=LOOP_INTERVAL)
            except asyncio.TimeoutError:
                pass

    def shutdown(self):
        print("thermostat: shutting down", flush=True)
        self._running = False
        self.relay.off()
        self.oled.off()
        self.relay.cleanup()
        self.encoder.cleanup()


# ── Singleton + FastAPI ───────────────────────────────────────────

thermostat: Thermostat | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global thermostat
    thermostat = Thermostat()
    task = asyncio.create_task(thermostat.run())
    yield
    thermostat.shutdown()
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/status")
async def get_status():
    return JSONResponse(thermostat.status())


@app.put("/setpoint")
async def put_setpoint(value: float):
    if not math.isfinite(value):
        return JSONResponse({"error": "value must be finite"}, status_code=400)
    if value < SETPOINT_MIN or value > SETPOINT_MAX:
        return JSONResponse(
            {"error": f"value must be between {SETPOINT_MIN} and {SETPOINT_MAX}"},
            status_code=400,
        )
    thermostat.set_setpoint(value)
    return JSONResponse(thermostat.status())


@app.post("/clear-fault")
async def post_clear_fault():
    thermostat.clear_fault()
    return JSONResponse(thermostat.status())


@app.get("/health")
async def health():
    return JSONResponse({"ok": not thermostat._fault})


@app.get("/events")
async def events():
    """SSE stream — pushes status JSON on every control cycle and setpoint change."""
    q: asyncio.Queue = asyncio.Queue(maxsize=8)
    thermostat._subscribers.add(q)

    async def stream():
        try:
            # Send current state immediately on connect
            yield f"data: {json.dumps(thermostat.status())}\n\n"
            while True:
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            thermostat._subscribers.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/", response_class=HTMLResponse)
async def web_ui():
    return WEB_UI_HTML


WEB_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thermostat</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, system-ui, sans-serif;
    background: #111; color: #eee;
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; user-select: none;
  }
  .card {
    background: #1a1a1a; border-radius: 20px; padding: 2rem;
    width: 340px; text-align: center;
  }
  .current { font-size: 5rem; font-weight: 700; line-height: 1; }
  .label { font-size: 0.85rem; color: #888; margin-top: 0.25rem; }
  .setpoint-row {
    display: flex; align-items: center; justify-content: center;
    gap: 1rem; margin: 1.5rem 0;
  }
  .btn {
    width: 56px; height: 56px; border-radius: 50%; border: 2px solid #444;
    background: #222; color: #eee; font-size: 1.5rem; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.1s;
    -webkit-tap-highlight-color: transparent;
  }
  .btn:active { background: #333; }
  .setpoint-val { font-size: 2.5rem; font-weight: 600; min-width: 5rem; }
  .status {
    font-size: 1.1rem; font-weight: 600; padding: 0.5rem 1rem;
    border-radius: 8px; display: inline-block; margin-top: 0.5rem;
  }
  .heating { background: #b33a00; color: #fff; }
  .idle { background: #333; color: #aaa; }
  .fault {
    background: #8b0000; color: #fff; margin-top: 1rem;
    padding: 0.75rem 1rem; border-radius: 8px; cursor: pointer;
  }
  .fault:hover { background: #a00; }
  .failsafe {
    background: #444; color: #ccc; margin-top: 0.75rem;
    padding: 0.4rem 0.75rem; border-radius: 6px; font-size: 0.8rem;
  }
  .hidden { display: none; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 6px; vertical-align: middle; }
  .dot-on { background: #f60; }
  .dot-off { background: #555; }
</style>
</head>
<body>
<div class="card">
  <div class="current" id="temp">--</div>
  <div class="label">current temperature</div>
  <div class="setpoint-row">
    <button class="btn" id="down" aria-label="decrease">&minus;</button>
    <div class="setpoint-val" id="setpoint">--</div>
    <button class="btn" id="up" aria-label="increase">+</button>
  </div>
  <div class="label">set temperature</div>
  <div style="margin-top:1rem">
    <div class="status idle" id="status">--</div>
  </div>
  <div class="failsafe hidden" id="failsafe">Nightly setback active</div>
  <div class="fault hidden" id="fault" role="button" tabindex="0">
    FAULT — tap to clear
  </div>
</div>
<script>
const $temp = document.getElementById('temp');
const $set  = document.getElementById('setpoint');
const $stat = document.getElementById('status');
const $fault = document.getElementById('fault');
const $fs = document.getElementById('failsafe');

function update(s) {
  $temp.textContent = s.current_f != null ? s.current_f + '\u00b0' : '--';
  $set.textContent = s.setpoint_f + '\u00b0';
  if (s.fault) {
    $stat.textContent = 'FAULT';
    $stat.className = 'status fault';
  } else if (s.heating) {
    $stat.innerHTML = '<span class="dot dot-on"></span>HEATING';
    $stat.className = 'status heating';
  } else {
    $stat.innerHTML = '<span class="dot dot-off"></span>IDLE';
    $stat.className = 'status idle';
  }
  $fault.classList.toggle('hidden', !s.fault);
  $fs.classList.toggle('hidden', !s.failsafe_active);
}

// SSE for live updates
let es;
function connect() {
  es = new EventSource('/events');
  es.onmessage = e => update(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(connect, 2000); };
}
connect();

async function setTemp(delta) {
  const cur = parseFloat($set.textContent);
  if (isNaN(cur)) return;
  const r = await fetch('/setpoint?value=' + (cur + delta), { method: 'PUT' });
  if (r.ok) update(await r.json());
}

document.getElementById('up').addEventListener('click', () => setTemp(1));
document.getElementById('down').addEventListener('click', () => setTemp(-1));

// Long-press for fast adjust
let holdTimer;
function startHold(delta) {
  setTemp(delta);
  let delay = 400;
  function repeat() {
    holdTimer = setTimeout(() => { setTemp(delta); delay = Math.max(100, delay - 50); repeat(); }, delay);
  }
  repeat();
}
function stopHold() { clearTimeout(holdTimer); }
for (const [id, d] of [['up', 1], ['down', -1]]) {
  const el = document.getElementById(id);
  el.addEventListener('pointerdown', e => { e.preventDefault(); startHold(d); });
  el.addEventListener('pointerup', stopHold);
  el.addEventListener('pointerleave', stopHold);
  el.addEventListener('pointercancel', stopHold);
}

$fault.addEventListener('click', async () => {
  const r = await fetch('/clear-fault', { method: 'POST' });
  if (r.ok) update(await r.json());
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
