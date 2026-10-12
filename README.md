# Millivolt Thermostat

A smart thermostat for a standing-pilot (millivolt) gas heater in my studio,
built on a Raspberry Pi Zero 2 W. It has a physical knob and display, a web UI
and REST API with live updates, and it's provisioned end to end with Ansible.

Not pretty but it gets the job done:

<img width="600" alt="Smart Thermostat for Millivolt Furnace" src="https://github.com/user-attachments/assets/3951803d-42bc-4284-b172-5bfd77ab05cb" />


Millivolt heaters have no 24 V transformer, so off-the-shelf smart thermostats
can't run on them. This one switches the heater's thermopile circuit with a
relay and powers itself separately.

## Hardware

| Part | Connection |
|---|---|
| Raspberry Pi Zero 2 W | Raspberry Pi OS (Bookworm/Trixie) |
| DS18B20 temperature sensor | 1-Wire on GPIO4 |
| Single-channel relay module (active-low) | GPIO17, wired across the heater's millivolt thermostat terminals |
| SH1106 1.3" OLED (128×64) | I²C, address `0x3C` |
| EC11 rotary encoder with push button | GPIO23 (CLK), GPIO24 (DT), GPIO25 (SW) |

The pins can be overridden with environment variables (`RELAY_PIN`,
`ENCODER_*_PIN`).

## Failing safe

This controls a gas appliance, so the safety logic runs on the Pi itself and
works without any network connection:

- **The relay defaults to off.** It's claimed in the off state at startup and
  driven off again on shutdown.
- **Sensor watchdog:** after 6 consecutive bad readings, the relay latches off
  in a fault state that only an explicit `POST /clear-fault` can clear.
- **Plausibility range:** readings outside −20°F to 120°F are rejected.
- **Maximum run time:** 3 hours of continuous heating latches a fault.
- **Nightly setback:** after 6 PM, if nobody has touched the knob or the API,
  the setpoint drops to 45°F, so the heater can't run all night unattended.
- **Process watchdog:** the service uses systemd `Type=notify` with
  `WatchdogSec=60`, so systemd restarts it if the control loop hangs. The Pi's
  hardware watchdog is also enabled, along with reboot-on-kernel-panic.
- **Hysteresis and a settle delay:** the relay switches with ±1°F of
  hysteresis, and the control loop waits until you stop turning the knob
  before acting on it. Together these prevent rapid relay cycling.

Most of these limits can be changed with environment variables. The control
logic is tested in simulation mode with no hardware attached:
`thermostat/test_control.py`, 93 tests.

## Software

- `thermostat/main.py`: the control loop plus a FastAPI app with a web UI,
  REST endpoints (`GET /status`, `PUT /setpoint?value=`,
  `POST /clear-fault`, `GET /health`), and Server-Sent Events (`GET /events`)
  for live updates.
- `sensor.py`, `relay.py`, `encoder.py`, `oled.py`: thin hardware wrappers.
  Setting `THERMOSTAT_SIMULATE=1` (or having no GPIO library available)
  replaces each one with a simulated version.
- State is kept on tmpfs, so the SD card isn't written to constantly.
- [`SKILL.md`](SKILL.md) is the API reference for an AI agent that controls
  the thermostat.

## Provisioning

```sh
cd ansible
# edit inventory.yml with your Pi's address and user
ansible-playbook -i inventory.yml playbook.yml
```

Roles:
- **`common`**: persistent journald with size limits, a swap file in place of
  zram, `/tmp` and `/var/tmp` on tmpfs, a forced fsck on boot, the hardware
  watchdog, and `uv`.
- **`wifi-resilience`**: NetworkManager retries forever, plus a
  gateway-ping watchdog.
- **`i2c`** and **`onewire`**: enable the device-tree overlays.
- **`scheduler`**: a small cron-to-HTTP scheduler service.
- **`thermostat-app`**: deploys the app as a systemd service.

`common`, `wifi-resilience`, and `scheduler` are vendored from a shared roles
repo I use across my Pis.

## Run locally (simulated)

```sh
cd thermostat
THERMOSTAT_SIMULATE=1 uv run main.py     # web UI on :5000
THERMOSTAT_SIMULATE=1 uv run --group dev pytest -q
```

## License

MIT
