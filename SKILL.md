# thermostat — Control the studio millivolt thermostat

Smart thermostat for the studio millivolt gas heater. Raspberry Pi Zero 2W with DS18B20 sensor, relay, OLED display, and rotary encoder. Accessible over Tailscale.

Base URL: `http://thermostat:5000`

## API

### Read state

`GET /status` — full state
```json
{
  "current_f": 66.5,
  "setpoint_f": 68.0,
  "heating": true,
  "fault": false,
  "failsafe_active": false,
  "consecutive_failures": 0
}
```

`GET /health` — quick check, returns `{"ok": false}` when faulted

### Set temperature

`PUT /setpoint?value=70` — set target temperature (range 50–85°F)
```json
{"current_f": 66.5, "setpoint_f": 70.0, "heating": true, "fault": false, "failsafe_active": false, "consecutive_failures": 0}
```

### Clear fault

`POST /clear-fault` — reset a latched fault (sensor failure, max run time, etc.)

Returns current status. Only do this after investigating the cause.

## Safety mechanisms

These are handled on the Pi autonomously — no network required:

- **Sensor watchdog**: 6 consecutive bad reads → relay locked off (fault)
- **Plausibility range**: readings outside -20°F to 120°F are rejected
- **Max run time**: 3 hours continuous heating → relay locked off (fault)
- **Nightly failsafe**: after 6 PM with no API/encoder interaction, setpoint drops to 45°F. Any API call or encoder twist clears it.

## Instructions

1. **Check status** before making changes: `GET /status`
2. **Set temperature**: `PUT /setpoint?value={temp}` — always confirm the new state in the response
3. **If fault is true**: report it to the user with the `consecutive_failures` count. Do NOT clear the fault without the user's explicit approval — a fault means something may be wrong with the hardware.
4. **If failsafe_active is true**: the nightly setback is in effect. Setting a new setpoint via the API will clear it automatically.
5. **Scheduling**: use cron or scheduled tasks on another host to call `PUT /setpoint` at desired times. The Pi holds whatever setpoint it's given until told otherwise.

## Examples

Check if the studio is warm enough:
```
GET http://thermostat:5000/status
→ current_f: 62.3, setpoint_f: 68.0, heating: true
"The studio is 62°F, heating to 68°F."
```

User says "turn up the heat to 72":
```
PUT http://thermostat:5000/setpoint?value=72
→ setpoint_f: 72.0, heating: true
"Set to 72°F. Currently heating."
```

User says "turn off the heat":
```
PUT http://thermostat:5000/setpoint?value=50
→ setpoint_f: 50.0, heating: false
"Set to minimum (50°F). Heat is off."
```
