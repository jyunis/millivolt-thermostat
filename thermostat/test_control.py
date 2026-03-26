"""Tests for thermostat control logic, setpoint management, and safety edge cases.

Run: THERMOSTAT_SIMULATE=1 uv run python -m pytest test_control.py -v
"""

import json
import math
import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

# Force simulation mode before any hardware imports
os.environ["THERMOSTAT_SIMULATE"] = "1"

import main as thermostat_main
from main import Thermostat, load_setpoint, save_setpoint


@pytest.fixture(autouse=True)
def tmp_state_file(tmp_path):
    """Redirect state file to a temp directory for every test."""
    path = str(tmp_path / "state.json")
    thermostat_main.STATE_FILE = path
    yield path


@pytest.fixture(autouse=True)
def reset_safety_defaults():
    """Ensure safety constants are at known values for every test."""
    thermostat_main.SENSOR_FAIL_LIMIT = 6
    thermostat_main.TEMP_PLAUSIBLE_MIN = -20.0
    thermostat_main.TEMP_PLAUSIBLE_MAX = 120.0
    thermostat_main.MAX_RUN_SECONDS = 10800.0
    thermostat_main.FAILSAFE_HOUR = 18
    thermostat_main.FAILSAFE_SETPOINT = 45.0
    yield


@pytest.fixture
def t():
    """Create a thermostat in simulation mode with known defaults."""
    thermostat_main.HYSTERESIS = 1.0
    thermostat_main.DEFAULT_SETPOINT = 68.0
    thermostat_main.SETPOINT_MIN = 50.0
    thermostat_main.SETPOINT_MAX = 85.0
    th = Thermostat()
    th.setpoint = 68.0
    yield th
    th.relay.off()


# ── Basic hysteresis ──────────────────────────────────────────────


class TestHysteresis:
    def test_turns_on_when_cold(self, t):
        """Relay fires when temp drops below setpoint - hysteresis."""
        t._control(66.5)  # below 67
        assert t.relay.is_on

    def test_turns_off_when_warm(self, t):
        """Relay cuts when temp rises above setpoint + hysteresis."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        t._control(69.5)  # above 69
        assert not t.relay.is_on

    def test_stays_off_in_dead_band(self, t):
        """Relay stays off when temp is in the dead band."""
        t._control(67.5)  # between 67 and 69
        assert not t.relay.is_on

    def test_stays_on_in_dead_band(self, t):
        """Relay stays on when temp is in the dead band (already heating)."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        t._control(67.5)
        assert t.relay.is_on

    def test_full_heat_cycle(self, t):
        """Simulate a full cycle: cold → heat on → warm → heat off → cool → stays off."""
        t._control(66.0)
        assert t.relay.is_on, "should turn on when cold"

        t._control(68.0)
        assert t.relay.is_on, "should stay on in dead band (rising)"

        t._control(69.5)
        assert not t.relay.is_on, "should turn off when warm"

        t._control(68.0)
        assert not t.relay.is_on, "should stay off in dead band (falling)"

        t._control(67.5)
        assert not t.relay.is_on, "should still be off at 67.5 (inside dead band)"

        t._control(67.0)
        assert not t.relay.is_on, "should still be off at exactly 67 (not strictly less)"

        t._control(66.5)
        assert t.relay.is_on, "should turn on again at 66.5 (below 67)"


# ── Boundary precision ────────────────────────────────────────────


class TestBoundaries:
    def test_exactly_at_lower_bound_stays_off(self, t):
        """At exactly setpoint - hysteresis, relay should NOT turn on (strictly less than)."""
        t._control(67.0)
        assert not t.relay.is_on

    def test_exactly_at_upper_bound_stays_on(self, t):
        """At exactly setpoint + hysteresis, relay should NOT turn off (strictly greater than)."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        t._control(69.0)
        assert t.relay.is_on

    def test_one_tick_below_lower_bound(self, t):
        """Just below the lower threshold turns on."""
        t._control(66.999)
        assert t.relay.is_on

    def test_one_tick_above_upper_bound(self, t):
        """Just above the upper threshold turns off."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        t._control(69.001)
        assert not t.relay.is_on

    def test_float_precision_near_boundary(self, t):
        """Ensure floating point arithmetic doesn't cause spurious switching."""
        # 68.0 - 1.0 should be exactly 67.0, but test with accumulated error
        t.setpoint = 68.1
        # 68.1 - 1.0 = 67.1 in exact math, but could be 67.09999... in float
        t._control(67.1)
        # Should NOT turn on — 67.1 is not < 67.1
        assert not t.relay.is_on


# ── Plausibility checks ───────────────────────────────────────────


class TestPlausibility:
    def test_nan_is_implausible(self, t):
        """NaN is rejected — treated as sensor fault, not passed to control."""
        t._control(float("nan"))
        assert not t.relay.is_on
        assert t._consecutive_failures == 1

    def test_inf_is_implausible(self, t):
        """Infinity is rejected as implausible."""
        t._control(float("inf"))
        assert t._consecutive_failures == 1

    def test_negative_inf_is_implausible(self, t):
        t._control(float("-inf"))
        assert t._consecutive_failures == 1

    def test_absurdly_high_temp_is_implausible(self, t):
        """500F is a sensor fault, not a real reading."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        t._control(500.0)
        assert t._consecutive_failures == 1

    def test_absurdly_low_temp_is_implausible(self, t):
        """-40F is below plausible range — sensor fault."""
        t._control(-40.0)
        assert t._consecutive_failures == 1
        assert not t.relay.is_on

    def test_boundary_of_plausible_range(self, t):
        """Exactly at plausible min/max is accepted."""
        t._control(-20.0)
        assert t._consecutive_failures == 0
        t._control(120.0)
        assert t._consecutive_failures == 0

    def test_just_outside_plausible_range(self, t):
        t._control(-20.1)
        assert t._consecutive_failures == 1

    def test_valid_cold_temp_works_normally(self, t):
        """0F is cold but plausible — relay turns on."""
        t._control(0.0)
        assert t.relay.is_on
        assert t._consecutive_failures == 0

    def test_valid_negative_temp_works_normally(self, t):
        """-10F is plausible (Minnesota winter) — relay turns on."""
        t._control(-10.0)
        assert t.relay.is_on

    def test_good_read_resets_failure_counter(self, t):
        """A valid reading after bad ones resets the counter."""
        t._control(float("nan"))
        t._control(float("nan"))
        assert t._consecutive_failures == 2
        t._control(68.0)  # good read
        assert t._consecutive_failures == 0


# ── Sensor watchdog ───────────────────────────────────────────────


class TestSensorWatchdog:
    def test_trips_fault_after_consecutive_failures(self, t):
        """After SENSOR_FAIL_LIMIT bad reads, fault latches and relay kills."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        for i in range(thermostat_main.SENSOR_FAIL_LIMIT):
            t._control(float("nan"))
        assert t._fault
        assert not t.relay.is_on

    def test_under_limit_no_fault(self, t):
        """One fewer than the limit does not trip fault."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        for i in range(thermostat_main.SENSOR_FAIL_LIMIT - 1):
            t._control(float("nan"))
        assert not t._fault
        assert t.relay.is_on

    def test_good_read_prevents_fault(self, t):
        """A good read in the middle resets the counter — no fault."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        for i in range(thermostat_main.SENSOR_FAIL_LIMIT - 1):
            t._control(float("nan"))
        t._control(68.0)  # good read resets counter
        for i in range(thermostat_main.SENSOR_FAIL_LIMIT - 1):
            t._control(float("nan"))
        assert not t._fault
        assert t.relay.is_on

    def test_fault_latches_relay_off(self, t):
        """Once faulted, relay stays off even with good readings."""
        t._fault = True
        t._control(50.0)  # cold — would normally turn on
        assert not t.relay.is_on

    def test_fault_ignores_all_control(self, t):
        """Faulted thermostat ignores all input until cleared."""
        t._fault = True
        for temp in [30.0, 50.0, 66.0, 80.0, float("nan")]:
            t._control(temp)
        assert not t.relay.is_on
        assert t._fault

    def test_clear_fault_allows_control_again(self, t):
        """After clearing fault, normal control resumes."""
        t._fault = True
        t.clear_fault()
        assert not t._fault
        t._control(66.0)
        assert t.relay.is_on

    def test_exception_in_run_loop_counts_as_failure(self, t):
        """Sensor exceptions in the run loop feed the watchdog."""
        t.relay.on()
        t._relay_on_since = time.monotonic()
        # Simulate what the run loop does on exception
        for i in range(thermostat_main.SENSOR_FAIL_LIMIT):
            t._record_failure("exception: sensor read timeout")
        assert t._fault
        assert not t.relay.is_on

    def test_implausible_while_heating_eventually_kills_relay(self, t):
        """Sustained implausible readings while heating trip the watchdog."""
        t._control(66.0)  # turn on normally
        assert t.relay.is_on
        # Sensor starts returning garbage
        for i in range(thermostat_main.SENSOR_FAIL_LIMIT):
            t._control(200.0)
        assert t._fault
        assert not t.relay.is_on


# ── Max run time ──────────────────────────────────────────────────


class TestMaxRunTime:
    def test_trips_after_max_run(self, t):
        """Relay running for MAX_RUN_SECONDS triggers fault."""
        t._control(66.0)  # relay on
        assert t.relay.is_on
        # Pretend relay has been on for 3 hours
        t._relay_on_since = time.monotonic() - thermostat_main.MAX_RUN_SECONDS
        t._control(66.0)  # next control cycle checks elapsed time
        assert t._fault
        assert not t.relay.is_on

    def test_no_fault_just_under_limit(self, t):
        """One second under the limit — no fault."""
        t._control(66.0)
        t._relay_on_since = time.monotonic() - (thermostat_main.MAX_RUN_SECONDS - 1)
        t._control(66.0)
        assert not t._fault
        assert t.relay.is_on

    def test_normal_cycle_resets_timer(self, t):
        """Relay off then on again resets the run timer."""
        t._control(66.0)  # on
        first_on = t._relay_on_since
        t._control(69.5)  # off
        assert t._relay_on_since is None
        t._control(66.0)  # on again
        assert t._relay_on_since is not None
        assert t._relay_on_since >= first_on

    def test_max_run_with_custom_limit(self, t):
        """Shorter MAX_RUN_SECONDS trips sooner."""
        thermostat_main.MAX_RUN_SECONDS = 60.0  # 1 minute
        t._control(66.0)
        t._relay_on_since = time.monotonic() - 61
        t._control(66.0)
        assert t._fault

    def test_relay_off_no_timer_running(self, t):
        """When relay is off, no run timer accumulates."""
        t._control(68.0)  # dead band, relay stays off
        assert t._relay_on_since is None


# ── Setpoint management ──────────────────────────────────────────


class TestSetpoint:
    def test_encoder_increases_setpoint(self, t):
        t._on_rotate(1)
        assert t.setpoint == 69.0

    def test_encoder_decreases_setpoint(self, t):
        t._on_rotate(-1)
        assert t.setpoint == 67.0

    def test_clamped_at_max(self, t):
        t.setpoint = 85.0
        t._on_rotate(1)
        assert t.setpoint == 85.0

    def test_clamped_at_min(self, t):
        t.setpoint = 50.0
        t._on_rotate(-1)
        assert t.setpoint == 50.0

    def test_rapid_increases_clamp(self, t):
        for _ in range(100):
            t._on_rotate(1)
        assert t.setpoint == 85.0

    def test_rapid_decreases_clamp(self, t):
        for _ in range(100):
            t._on_rotate(-1)
        assert t.setpoint == 50.0

    def test_setpoint_change_during_heating_no_immediate_toggle(self, t):
        """Raising setpoint while heating shouldn't turn relay off."""
        t._control(66.0)  # heat on
        assert t.relay.is_on
        t._on_rotate(1)   # setpoint 68 → 69
        # Still on — current temp 66 is still below new threshold (69 - 1 = 68)
        t._control(66.0)
        assert t.relay.is_on

    def test_setpoint_drop_while_heating_can_turn_off(self, t):
        """Lowering setpoint enough while heating can cause relay to cut."""
        t._control(66.0)  # heat on
        assert t.relay.is_on
        # Drop setpoint to 64 — upper threshold becomes 65
        t.setpoint = 64.0
        t._control(66.0)  # 66 > 65, should turn off
        assert not t.relay.is_on


# ── Setpoint persistence ─────────────────────────────────────────


class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_state_file):
        save_setpoint(72.5)
        assert load_setpoint() == 72.5

    def test_missing_file_returns_default(self, tmp_state_file):
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_corrupt_json_returns_default(self, tmp_state_file):
        with open(tmp_state_file, "w") as f:
            f.write("{{{invalid json")
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_empty_file_returns_default(self, tmp_state_file):
        with open(tmp_state_file, "w") as f:
            f.write("")
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_missing_key_returns_default(self, tmp_state_file):
        with open(tmp_state_file, "w") as f:
            json.dump({"other_key": 99}, f)
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_non_numeric_value_returns_default(self, tmp_state_file):
        with open(tmp_state_file, "w") as f:
            json.dump({"setpoint": "not a number"}, f)
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_nan_in_file_returns_default(self, tmp_state_file):
        """NaN in state file is rejected — returns default setpoint."""
        with open(tmp_state_file, "w") as f:
            f.write('{"setpoint": NaN}')
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_inf_in_file_returns_default(self, tmp_state_file):
        """Infinity in state file is rejected — returns default setpoint."""
        with open(tmp_state_file, "w") as f:
            f.write('{"setpoint": Infinity}')
        assert load_setpoint() == thermostat_main.DEFAULT_SETPOINT

    def test_encoder_persists_setpoint(self, t, tmp_state_file):
        t._on_rotate(1)
        t._on_rotate(1)
        loaded = load_setpoint()
        assert loaded == 70.0

    def test_extreme_value_in_file(self, tmp_state_file):
        """Manually edited file with out-of-range value loads as-is.
        The clamping only happens on encoder rotation, not on load."""
        with open(tmp_state_file, "w") as f:
            json.dump({"setpoint": 200.0}, f)
        assert load_setpoint() == 200.0


# ── Shutdown safety ───────────────────────────────────────────────


class TestShutdown:
    def test_shutdown_turns_off_relay(self, t):
        t.relay.on()
        assert t.relay.is_on
        t.shutdown()
        assert not t.relay.is_on

    def test_shutdown_when_already_off(self, t):
        """Shutdown is safe even if relay was never on."""
        t.shutdown()
        assert not t.relay.is_on

    def test_shutdown_stops_loop(self, t):
        t.shutdown()
        assert not t._running


# ── Hysteresis parameter variations ──────────────────────────────


class TestHysteresisValues:
    def test_zero_hysteresis_bang_bang(self, t):
        """With zero hysteresis, relay toggles at exactly the setpoint.
        This would cause rapid cycling in practice."""
        thermostat_main.HYSTERESIS = 0.0
        t.setpoint = 68.0
        # Just below setpoint — on
        t._control(67.999)
        assert t.relay.is_on
        # Just above setpoint — off
        t._control(68.001)
        assert not t.relay.is_on

    def test_large_hysteresis(self, t):
        """Large hysteresis creates wide dead band."""
        thermostat_main.HYSTERESIS = 5.0
        t.setpoint = 68.0
        # Must drop below 63 to turn on
        t._control(63.5)
        assert not t.relay.is_on
        t._control(62.9)
        assert t.relay.is_on
        # Must rise above 73 to turn off
        t._control(72.9)
        assert t.relay.is_on
        t._control(73.1)
        assert not t.relay.is_on


# ── Combined scenarios ────────────────────────────────────────────


class TestCombinedScenarios:
    def test_sensor_dies_mid_heating_kills_relay(self, t):
        """Real scenario: sensor cable comes loose while furnace is running."""
        t._control(66.0)  # normal heat on
        assert t.relay.is_on
        # Sensor starts returning implausible values (disconnected DS18B20 reads 185F or 0)
        for _ in range(thermostat_main.SENSOR_FAIL_LIMIT):
            t._control(185.0)  # above plausible max
        assert t._fault
        assert not t.relay.is_on

    def test_intermittent_sensor_no_fault(self, t):
        """Flaky sensor that recovers doesn't trip fault."""
        t._control(66.0)  # heat on
        assert t.relay.is_on
        # 5 bad reads (just under limit of 6)
        for _ in range(5):
            t._control(float("nan"))
        assert not t._fault
        # Then a good read
        t._control(67.0)
        assert t._consecutive_failures == 0
        assert t.relay.is_on  # still heating normally

    def test_furnace_cant_keep_up_trips_max_run(self, t):
        """Furnace runs for 3 hours but temp never reaches setpoint.
        Something is wrong (door open, furnace dying, etc.)."""
        t._control(66.0)  # heat on
        # Simulate 3 hours passing with temp barely rising
        t._relay_on_since = time.monotonic() - thermostat_main.MAX_RUN_SECONDS
        t._control(66.5)  # still cold
        assert t._fault
        assert not t.relay.is_on

    def test_normal_operation_no_false_faults(self, t):
        """Extended normal operation never triggers any safety mechanism."""
        temps = [
            66.0, 66.5, 67.0, 67.5, 68.0, 68.5, 69.0, 69.5,  # rising
            69.0, 68.5, 68.0, 67.5, 67.0, 66.5, 66.0,          # falling
        ] * 10  # many cycles
        for temp in temps:
            t._control(temp)
            assert not t._fault


# ── API-facing methods ────────────────────────────────────────────


class TestAPIInterface:
    def test_set_setpoint_from_api(self, t):
        t.set_setpoint(72.0)
        assert t.setpoint == 72.0

    def test_set_setpoint_clamped_high(self, t):
        t.set_setpoint(100.0)
        assert t.setpoint == thermostat_main.SETPOINT_MAX

    def test_set_setpoint_clamped_low(self, t):
        t.set_setpoint(30.0)
        assert t.setpoint == thermostat_main.SETPOINT_MIN

    def test_set_setpoint_persists(self, t, tmp_state_file):
        t.set_setpoint(71.0)
        assert load_setpoint() == 71.0

    def test_set_setpoint_triggers_oled(self, t):
        """OLED updates immediately on API setpoint change (needs a current reading)."""
        t._current_f = 67.0
        t.set_setpoint(70.0)
        # No assertion on OLED output (simulated), but no exception = good

    def test_status_reflects_state(self, t):
        t._current_f = 66.5
        t._control(66.5)  # heat on
        s = t.status()
        assert s["current_f"] == 66.5
        assert s["setpoint_f"] == 68.0
        assert s["heating"] is True
        assert s["fault"] is False

    def test_status_no_reading_yet(self, t):
        s = t.status()
        assert s["current_f"] is None

    def test_status_during_fault(self, t):
        t._fault = True
        s = t.status()
        assert s["fault"] is True
        assert s["heating"] is False

    def test_clear_fault_via_api(self, t):
        t._fault = True
        t._consecutive_failures = 10
        t.clear_fault()
        s = t.status()
        assert s["fault"] is False
        assert t._consecutive_failures == 0

    def test_status_shows_failsafe(self, t):
        t._failsafe_active = True
        s = t.status()
        assert s["failsafe_active"] is True


# ── Nightly failsafe setback ─────────────────────────────────────


class TestNightlyFailsafe:
    def _mock_time(self, hour, minute=0):
        """Return a datetime.now() mock for a given hour today."""
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)

    def test_triggers_after_cutoff_no_interaction(self, t):
        """Past 6 PM with no interaction → setpoint forced to 45F."""
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(20)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t.setpoint = 70.0
            t._last_interaction = None
            t._control(66.0)
        assert t.setpoint == 45.0
        assert t._failsafe_active

    def test_no_trigger_before_cutoff(self, t):
        """Before 6 PM → no failsafe, even without interaction."""
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(14)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t.setpoint = 70.0
            t._last_interaction = None
            t._control(66.0)
        assert t.setpoint == 70.0
        assert not t._failsafe_active

    def test_api_call_after_cutoff_prevents_failsafe(self, t):
        """API call at 7 PM → no failsafe at 8 PM."""
        now_7pm = self._mock_time(19)
        now_8pm = self._mock_time(20)
        t.set_setpoint(72.0)
        t._last_interaction = now_7pm
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = now_8pm
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t._control(66.0)
        assert t.setpoint == 72.0
        assert not t._failsafe_active

    def test_old_interaction_triggers_failsafe(self, t):
        """Interaction at 3 PM, now 8 PM → failsafe triggers."""
        t._last_interaction = self._mock_time(15)
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(20)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t.setpoint = 70.0
            t._control(66.0)
        assert t.setpoint == 45.0
        assert t._failsafe_active

    def test_encoder_resets_failsafe(self, t):
        """Failsafe active, then encoder twist → failsafe cleared."""
        t._failsafe_active = True
        t.setpoint = 55.0
        t._on_rotate(1)
        assert t.setpoint == 56.0
        assert not t._failsafe_active

    def test_api_resets_failsafe(self, t):
        """Failsafe active, then API call → failsafe cleared."""
        t._failsafe_active = True
        t.setpoint = 45.0
        t.set_setpoint(68.0)
        assert t.setpoint == 68.0
        assert not t._failsafe_active

    def test_failsafe_only_fires_once(self, t):
        """Failsafe doesn't repeatedly log/save once already active."""
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(20)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t.setpoint = 70.0
            t._last_interaction = None
            t._control(66.0)
            assert t._failsafe_active
            # Manually raise setpoint without resetting failsafe flag
            # (simulating no API/encoder, just direct mutation for test)
            old_setpoint = t.setpoint
            t._control(66.0)
            assert t.setpoint == old_setpoint  # didn't re-fire

    def test_failsafe_during_fault_is_skipped(self, t):
        """Faulted thermostat skips failsafe check (fault takes priority)."""
        t._fault = True
        t.setpoint = 70.0
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(20)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t._control(66.0)
        assert t.setpoint == 70.0  # fault returned early, no failsafe

    def test_failsafe_with_custom_hour(self, t):
        """Custom FAILSAFE_HOUR is respected."""
        thermostat_main.FAILSAFE_HOUR = 22
        with patch("main.datetime") as mock_dt:
            mock_dt.now.return_value = self._mock_time(20)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            t.setpoint = 70.0
            t._last_interaction = None
            t._control(66.0)
        assert t.setpoint == 70.0  # 8 PM < 10 PM, no failsafe
        assert not t._failsafe_active
