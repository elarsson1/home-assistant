"""Simulator for the heat stage controller (packages/heat_stage_controller.yaml).

Two parts:

* HeatStageController: a line-by-line port of steps 3 to 8 of the controller
  automation, revision 4.1, with the Living Room tunables. Optional what-if
  variants can be switched on through the `variants` argument; with none set
  it behaves exactly as the YAML.

* RoomModel: a simplified two-node model of the Living Room (floor and room
  air) plus the tekmar 561, the boiler supply and the heat pump. It is tuned
  to the observations in docs/heat_stage_controller_design.md, not fitted to
  data, so treat its timings as approximate.

`simulate()` runs the two together minute by minute and returns one row per
minute. Temperatures are in degrees F, time in minutes.
"""
import math
import random

# Living Room tunables, as at the top of the controller automation.
DEFAULT_TUNABLES = dict(
    freeze_protect_outdoor_temp=30,
    rest_droop=2,
    recovery_start_error=2,
    recovery_stop_error=1,
    min_minutes_recovery=15,
    min_minutes_heat_pump_off=15,
    boost_start_degree_minutes=30,
    assist_start_degree_minutes=60,
    assist_carry_degree_minutes=30,
    integral_decay_at_setpoint=0.25,
    staging_integrator_minimum=-30,
    assist_hold_minutes=20,
    assist_call_delta=1,
    level_settling_minutes=45,
    integral_minutes_per_degree=90,
    floor_behind_margin=1,
    learning_shortfall_weight=0.5,
    floor_stop_holding_error=2,
    floor_proportional_step=1,
    floor_max_lead=1,
    floor_max_lag=2,
    boost_release_minutes=180,
    offset_learning_rate=0.05,
    offset_max_deviation=3,
)

# What-if variants (all off by default). See "What the simulations suggest"
# in docs/heat_stage_controller_scenarios.md.
KNOWN_VARIANTS = {
    # Recovery -> boost restarts the staging integrator at
    # assist_start_degree_minutes instead of 0, so a short reading right after
    # recovery arms assist on the next run.
    "recovery_returns_armed",
    # In boost, a gap of recovery_start_error goes to assist instead of
    # recovery, so the heat pump runs to setpoint and stays armed.
    "boost_gap_goes_to_assist",
    # The boost release clock is held while the room reads
    # floor_stop_holding_error or more over setpoint (a fireplace, cooking).
    "pause_release_while_far_over",
}

FLOOR_SENSOR_CAP = 85  # tekmar floor sensor cap, max_temp of the _floor entity


def round_like_jinja(value):
    """`value | round(0) | int` in the Home Assistant templates."""
    return int(round(value))


class HeatStageController:
    """State and logic of one zone of the heat stage controller."""

    def __init__(self, mode="full", tunables=None, variants=()):
        # mode "full": revision 4.1 as written
        # mode "nudge_only": no stored level; the floor minimum is built on
        #     the floor reading (floor reading + nudge, same window)
        # mode "off": controller disabled (tekmar alone, heat pump off)
        if mode not in ("full", "nudge_only", "off"):
            raise ValueError(f"unknown mode {mode!r}")
        unknown_variants = set(variants) - KNOWN_VARIANTS
        if unknown_variants:
            raise ValueError(f"unknown variants {sorted(unknown_variants)}")
        self.mode = mode
        self.tunables = dict(DEFAULT_TUNABLES, **(tunables or {}))
        self.variants = set(variants)

        # Helpers (input_* entities in the package)
        self.stage = "radiant_only"
        self.stage_since_minute = -10_000
        self.last_short_minute = None
        self.staging_integrator = 0.0
        self.learned_floor_level = 0.0  # 0 means never set
        self.heat_pump_offset = -1.5
        # Not a helper: when the room last read far over (variant only)
        self.last_far_over_minute = None

        self.last_run_minute = None
        self.stage_changes = []  # (minute, from_stage, to_stage)

    # script.hsc_set_stage
    def set_stage(self, minute, new_stage, integrator_value=0.0):
        self.stage_changes.append((minute, self.stage, new_stage))
        self.stage = new_stage
        self.stage_since_minute = minute
        self.staging_integrator = float(integrator_value)

    def run(self, minute, devices, trigger="tick", value_before_change=None):
        """One run of the automation for this zone.

        trigger: "tick" (5-minute pattern or start-up), "room" (the tekmar
        reading changed) or "setpoint" (the tekmar setpoint changed).
        value_before_change: the reading or setpoint from before the change,
        for "room" and "setpoint" triggers.
        """
        tun = self.tunables
        if self.mode == "off":
            self.last_run_minute = minute
            return

        # Minutes since the automation last ran, capped at 10
        if self.last_run_minute is None:
            minutes_since_last_run = 5
        else:
            minutes_since_last_run = round(min(minute - self.last_run_minute, 10), 2)
        self.last_run_minute = minute

        # ---- 1. Readings ----
        setpoint = devices.setpoint
        room_temp = devices.room_reading
        heat_pump_reading = devices.heat_pump_reading
        floor_temp = devices.floor_reading
        floor_cap = FLOOR_SENSOR_CAP
        floor_minimum_now = devices.floor_minimum
        outdoor_24h = devices.outdoor_24h_average
        integrator = self.staging_integrator
        offset = self.heat_pump_offset
        stage = self.stage

        # ---- 3. Derived values ----
        error = setpoint - room_temp  # positive = room below setpoint
        minutes_in_stage = minute - self.stage_since_minute
        minutes_since_last_short = (minute - self.last_short_minute
                                    if self.last_short_minute is not None else 9999)
        heat_pump_can_start = (devices.heat_pump_mode == "heat"
                               or minute - devices.heat_pump_mode_changed_minute
                               >= tun["min_minutes_heat_pump_off"])
        heat_pump_reading_trusted = (abs(heat_pump_reading - (room_temp + offset))
                                     <= tun["offset_max_deviation"])

        # The error that applied over the minutes since the last run
        interval_room_temp = value_before_change if trigger == "room" else room_temp
        interval_setpoint = value_before_change if trigger == "setpoint" else setpoint
        interval_error = interval_setpoint - interval_room_temp
        error_increment = (interval_error * minutes_since_last_run
                           if (interval_error >= 1 or interval_error <= -1) else 0.0)

        if self.mode == "nudge_only":
            floor_level = floor_temp
        else:
            floor_level = self.learned_floor_level if self.learned_floor_level > 0 else floor_temp
        floor_upper_limit = min(floor_temp + tun["floor_max_lead"], floor_cap)
        floor_lower_limit = floor_temp - tun["floor_max_lag"]

        radiant_behind = (floor_temp <= floor_level - tun["floor_behind_margin"]
                          or floor_temp >= floor_cap - 1)
        floor_level_may_learn = (stage == "boost"
                                 and minutes_in_stage >= tun["level_settling_minutes"]
                                 and 1 <= abs(interval_error) < 2)
        floor_level_learning_upward = (floor_level_may_learn
                                       and interval_error >= 1
                                       and floor_temp > floor_level - tun["floor_behind_margin"])

        # ---- 4. Heat pump offset (slow running average) ----
        if heat_pump_reading_trusted:
            weight = min(tun["offset_learning_rate"] * minutes_since_last_run / 5, 1)
            blended = offset + weight * ((heat_pump_reading - room_temp) - offset)
            self.heat_pump_offset = round(max(-3, min(3, blended)), 2)

        # ---- 5. Last short ----
        if error >= 1:
            self.last_short_minute = minute
        if error <= -tun["floor_stop_holding_error"]:
            self.last_far_over_minute = minute

        # ---- 6. Learned floor level ----
        if self.mode == "full":
            if floor_level_learning_upward:
                level_start = max(floor_level, floor_temp)
                self.learned_floor_level = round(
                    min(level_start + error_increment / tun["integral_minutes_per_degree"], floor_cap), 2)
            elif floor_level_may_learn and interval_error <= -1:
                self.learned_floor_level = round(
                    max(floor_level + error_increment / tun["integral_minutes_per_degree"], setpoint), 2)

        # ---- 6. Staging integrator ----
        if floor_level_learning_upward and not radiant_behind:
            staging_increment = error_increment * tun["learning_shortfall_weight"]
        else:
            staging_increment = error_increment
        if error_increment != 0:
            self.staging_integrator = round(max(min(integrator + staging_increment,
                                                    tun["assist_start_degree_minutes"]),
                                                tun["staging_integrator_minimum"]), 1)
        else:
            leak = tun["integral_decay_at_setpoint"] * minutes_since_last_run
            if integrator > 0:
                self.staging_integrator = round(max(integrator - leak, 0), 1)
            elif integrator < 0:
                self.staging_integrator = round(min(integrator + leak, 0), 1)
        integrator = self.staging_integrator

        # ---- 7. Stage transitions (first match wins) ----
        release_paused = ("pause_release_while_far_over" in self.variants
                          and self.last_far_over_minute is not None
                          and minute - self.last_far_over_minute < tun["boost_release_minutes"])
        if ("boost_gap_goes_to_assist" in self.variants and stage == "boost"
                and error >= tun["recovery_start_error"] and heat_pump_can_start):
            self.set_stage(minute, "assist")
        elif (stage in ("radiant_only", "boost") and error >= tun["recovery_start_error"]
              and heat_pump_can_start):
            self.set_stage(minute, "recovery")
        elif stage == "radiant_only" and integrator >= tun["boost_start_degree_minutes"]:
            if self.mode == "full":
                self.learned_floor_level = float(floor_temp)
            self.set_stage(minute, "boost")
        elif (stage == "recovery" and error <= tun["recovery_stop_error"]
              and minutes_in_stage >= tun["min_minutes_recovery"] and floor_minimum_now > 0):
            carry = (tun["assist_start_degree_minutes"]
                     if "recovery_returns_armed" in self.variants else 0)
            self.set_stage(minute, "boost", carry)
        elif (stage == "recovery" and error <= tun["recovery_stop_error"]
              and minutes_in_stage >= tun["min_minutes_recovery"]):
            self.set_stage(minute, "radiant_only")
        elif (stage == "boost" and error >= 1
              and integrator >= tun["assist_start_degree_minutes"] and heat_pump_can_start):
            self.set_stage(minute, "assist")
        elif (stage == "assist" and error < 1
              and minutes_since_last_short >= tun["assist_hold_minutes"]):
            self.set_stage(minute, "boost", tun["assist_carry_degree_minutes"])
        elif (stage == "boost" and error < 1
              and minutes_since_last_short >= tun["boost_release_minutes"]
              and floor_minimum_now < floor_temp and not release_paused):
            self.set_stage(minute, "radiant_only")

        # ---- 8. Outputs for the stage we are in now ----
        stage = self.stage
        offset = self.heat_pump_offset
        if self.mode == "nudge_only":
            floor_level = floor_temp
        else:
            floor_level = self.learned_floor_level if self.learned_floor_level > 0 else floor_temp

        # 8a. Floor minimum
        if stage == "radiant_only":
            devices.floor_minimum = 0
        elif stage == "recovery":
            pass  # left exactly as it is
        elif error <= -tun["floor_stop_holding_error"]:
            devices.floor_minimum = round_like_jinja(floor_lower_limit)
        else:
            if error >= 1:
                nudge = tun["floor_proportional_step"]
            elif error <= -1:
                nudge = -tun["floor_proportional_step"]
            else:
                nudge = 0
            upper_limit_now = floor_temp - 1 if error <= -1 else floor_upper_limit
            wanted = min(max(floor_level + nudge, floor_lower_limit), upper_limit_now)
            devices.floor_minimum = round_like_jinja(wanted)

        # 8b. Heat pump
        if stage == "recovery":
            devices.set_heat_pump(minute, "heat",
                                  round_like_jinja(setpoint - tun["recovery_stop_error"] + offset))
        elif stage == "assist" and error >= 1:
            devices.set_heat_pump(minute, "heat", round_like_jinja(
                heat_pump_reading + tun["assist_call_delta"] if heat_pump_reading_trusted
                else setpoint + offset))
        elif stage == "assist":
            devices.set_heat_pump(minute, "heat", round_like_jinja(
                heat_pump_reading - tun["assist_call_delta"] if heat_pump_reading_trusted
                else setpoint + offset - 2))
        else:
            freeze_threshold = (tun["freeze_protect_outdoor_temp"]
                                + (2 if devices.heat_pump_mode == "heat" else 0))
            if outdoor_24h < freeze_threshold:
                devices.set_heat_pump(minute, "heat",
                                      round_like_jinja(setpoint - tun["rest_droop"] + offset))
            else:
                devices.set_heat_pump(minute, "off")


class RoomModel:
    """Two-node Living Room model, the tekmar 561, the boiler and the heat pump.

    Units: heat in multiples of the room's loss per degree F of room-to-outdoor
    difference, so the loss coefficient is 1.

        floor: FLOOR_CAPACITY dTf/dt = Q_floor - FLOOR_TO_ROOM (Tf - Tr)
        room:  ROOM_CAPACITY  dTr/dt = FLOOR_TO_ROOM (Tf - Tr) - (Tr - To) + Q_hp + Q_gains

    FLOOR_TO_ROOM = 1 / 0.6, so the floor needed to hold the room is
    Tr + 0.6 (Tr - To), the same form as the k formula in the design notes.
    """
    ROOM_LOSS = 1.0
    FLOOR_TO_ROOM = 1 / 0.6
    FLOOR_CAPACITY = 20.0          # hours x heat units per degree
    ROOM_CAPACITY = 6.0
    SUPPLY_TO_FLOOR = 0.7          # heat transfer from the supply water when calling
    HEAT_PUMP_FULL_OUTPUT = 45.0   # about 1.2 x the loss at 30 F outdoors
    HEAT_PUMP_SENSOR_OFFSET = -1.5 # the MHK2 reads this much below the tekmar
    TEKMAR_CYCLE_MINUTES = 14

    # tekmar floor loop duty against (floor minimum - floor temperature),
    # from the observations in the design doc, section 7
    FLOOR_LOOP_DUTY_POINTS = [(-2, 0.0), (-1, 0.05), (0, 0.55), (1, 0.72), (2, 1.0)]

    def __init__(self, room_temp, floor_temp, seed=1):
        self.room_temp = room_temp
        self.floor_temp = floor_temp
        self.floor_heat = 0.0          # lagged heat into the floor
        self.heat_pump_heat = 0.0      # lagged heat pump output
        self.navien_boost = 0.0        # supply temperature added by Navien boost
        self.continuous_call_minutes = 0
        self.calling = False
        self.heat_pump_running = False
        self.heat_pump_modulation = 0.6
        self.random = random.Random(seed)
        self.room_sensor_noise = 0.0
        self.heat_pump_sensor_noise = 0.0
        self.duty = 0.0

        # What the controller reads and writes
        self.setpoint = 68
        self.floor_minimum = 0
        self.heat_pump_mode = "off"
        self.heat_pump_mode_changed_minute = -10_000
        self.heat_pump_setpoint = 66
        self.outdoor = 40
        self.outdoor_24h_average = 40

    # script.hsc_set_heat_pump
    def set_heat_pump(self, minute, mode, setpoint=None):
        if mode != self.heat_pump_mode:
            self.heat_pump_mode = mode
            self.heat_pump_mode_changed_minute = minute
        if mode == "heat":
            self.heat_pump_setpoint = setpoint

    @property
    def room_reading(self):
        """tekmar air reading, whole degrees."""
        return round_like_jinja(self.room_temp + self.room_sensor_noise)

    @property
    def floor_reading(self):
        return round_like_jinja(self.floor_temp)

    @property
    def heat_pump_reading(self):
        """MHK2 reading, whole degrees."""
        return round_like_jinja(self.room_temp + self.HEAT_PUMP_SENSOR_OFFSET + self.heat_pump_sensor_noise)

    def supply_temperature(self, outdoor):
        """Navien outdoor reset plus boost, limited by the mixing valve."""
        return min(140, 100 + (70 - outdoor) + self.navien_boost)

    def floor_loop_duty(self):
        if self.floor_minimum <= 0:
            return 0.0
        difference = self.floor_minimum - self.floor_temp
        points = self.FLOOR_LOOP_DUTY_POINTS
        if difference <= points[0][0]:
            return points[0][1]
        if difference >= points[-1][0]:
            return points[-1][1]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= difference <= x1:
                return y0 + (y1 - y0) * (difference - x0) / (x1 - x0)
        return 0.0

    def step(self, minute, outdoor, gains):
        """Advance one minute."""
        # Slowly wandering sensor noise, a tenth of a degree or so
        self.room_sensor_noise = 0.9 * self.room_sensor_noise + self.random.gauss(0, 0.04)
        self.heat_pump_sensor_noise = 0.9 * self.heat_pump_sensor_noise + self.random.gauss(0, 0.04)

        # tekmar 561: proportional air loop (100% at 1.5 F below setpoint,
        # 50% at setpoint, 0% at 1.5 F above, on the unrounded temperature)
        # and the floor loop; it calls when either asks
        air_loop_duty = max(0.0, min(1.0, 0.5 + (self.setpoint - (self.room_temp + self.room_sensor_noise)) / 3))
        duty = max(air_loop_duty, self.floor_loop_duty())
        if self.floor_temp >= FLOOR_SENSOR_CAP:
            duty = 0.0
        self.duty = duty
        phase = minute % self.TEKMAR_CYCLE_MINUTES
        self.calling = phase < duty * self.TEKMAR_CYCLE_MINUTES - 1e-9 or duty >= 0.999
        self.continuous_call_minutes = self.continuous_call_minutes + 1 if self.calling else 0

        # Navien boost: supply climbs after 15 minutes of continuous call
        if self.continuous_call_minutes > 15:
            self.navien_boost = min(self.navien_boost + 0.3, 30)
        elif not self.calling:
            self.navien_boost = max(self.navien_boost - 1.0, 0)
        floor_heat_target = (self.SUPPLY_TO_FLOOR * (self.supply_temperature(outdoor) - self.floor_temp)
                             if self.calling else 0.0)
        self.floor_heat += (floor_heat_target - self.floor_heat) * (1 - math.exp(-1 / 4))

        # Heat pump on its own thermostat: starts 0.5 F below its setpoint,
        # stops 1 F above, inverter modulation with a slow integral
        if self.heat_pump_mode == "heat":
            heat_pump_error = self.heat_pump_setpoint - (
                self.room_temp + self.HEAT_PUMP_SENSOR_OFFSET + self.heat_pump_sensor_noise)
            if not self.heat_pump_running and heat_pump_error >= 0.5:
                self.heat_pump_running = True
                self.heat_pump_modulation = 0.6
            elif self.heat_pump_running and heat_pump_error <= -1.0:
                self.heat_pump_running = False
            if self.heat_pump_running:
                self.heat_pump_modulation = max(0.25, min(1.0, self.heat_pump_modulation + 0.02 * heat_pump_error))
                fraction = max(0.25, min(1.0, self.heat_pump_modulation + 0.3 * heat_pump_error))
                heat_pump_target = self.HEAT_PUMP_FULL_OUTPUT * fraction
            else:
                heat_pump_target = 0.0
        else:
            self.heat_pump_running = False
            heat_pump_target = 0.0
        self.heat_pump_heat += (heat_pump_target - self.heat_pump_heat) * (1 - math.exp(-1 / 4))

        hours = 1 / 60
        floor_change = (self.floor_heat - self.FLOOR_TO_ROOM * (self.floor_temp - self.room_temp)) / self.FLOOR_CAPACITY
        room_change = (self.FLOOR_TO_ROOM * (self.floor_temp - self.room_temp)
                       - self.ROOM_LOSS * (self.room_temp - outdoor)
                       + self.heat_pump_heat + gains) / self.ROOM_CAPACITY
        self.floor_temp += floor_change * hours
        self.room_temp += room_change * hours


def simulate(schedule, minutes, mode="full", warmup_minutes=36 * 60, initial=(68, 80),
             seed=1, tunables=None, variants=()):
    """Run the controller against the room model.

    schedule(minute) returns dict(outdoor=..., setpoint=..., gains=...) and is
    called for minute = -warmup_minutes ... minutes - 1.
    Returns (rows, stage_changes) for minute >= 0, one row per minute.
    """
    room = RoomModel(*initial, seed=seed)
    controller = HeatStageController(mode, tunables, variants)
    outdoor_history = []
    rows = []
    room.setpoint = schedule(-warmup_minutes)["setpoint"]
    previous_room_reading = room.room_reading

    for minute in range(-warmup_minutes, minutes):
        conditions = schedule(minute)
        outdoor_history.append(conditions["outdoor"])
        if len(outdoor_history) > 1440:
            outdoor_history.pop(0)
        room.outdoor = conditions["outdoor"]
        room.outdoor_24h_average = sum(outdoor_history) / len(outdoor_history)

        # Setpoint change at the wall or from a preset: run right away
        if conditions["setpoint"] != room.setpoint:
            old_setpoint = room.setpoint
            room.setpoint = conditions["setpoint"]
            controller.run(minute, room, "setpoint", old_setpoint)

        room.step(minute, conditions["outdoor"], conditions["gains"])

        # Room reading change: run right away
        if room.room_reading != previous_room_reading:
            controller.run(minute, room, "room", previous_room_reading)
            previous_room_reading = room.room_reading

        # 5-minute time pattern
        if minute % 5 == 0:
            controller.run(minute, room, "tick")

        if minute >= 0:
            rows.append(dict(
                minute=minute,
                outdoor=round(conditions["outdoor"], 1),
                gains=round(conditions["gains"], 1),
                setpoint=room.setpoint,
                room_true=round(room.room_temp, 2),
                room_reading=room.room_reading,
                floor_true=round(room.floor_temp, 2),
                floor_reading=room.floor_reading,
                floor_minimum=room.floor_minimum,
                learned_floor_level=round(controller.learned_floor_level, 2),
                staging_integrator=controller.staging_integrator,
                stage=controller.stage,
                heat_pump_output_pct=round(room.heat_pump_heat / room.HEAT_PUMP_FULL_OUTPUT * 100),
                heat_pump_mode=room.heat_pump_mode,
                heat_pump_setpoint=room.heat_pump_setpoint,
                tekmar_calling=1 if room.calling else 0,
                heat_pump_offset=controller.heat_pump_offset,
            ))
    changes = [change for change in controller.stage_changes if change[0] >= 0]
    return rows, changes


def ramp(minute, points):
    """Piecewise-linear value through points [(minute, value), ...]."""
    if minute <= points[0][0]:
        return points[0][1]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= minute <= t1:
            return v0 + (v1 - v0) * (minute - t0) / (t1 - t0)
    return points[-1][1]
