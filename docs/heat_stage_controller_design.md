# Heat stage controller: design

Status: active since 2026-11-26 and being tested in live operation for performance and tuning. This document describes revision 3 of the control logic, written on 2026-11-27 after two days of live data (section 12). Thresholds and timings are starting values and are expected to change.

## Summary

This is a supervisory staging controller: a small state machine that sits above the two thermostats and decides which heat source should be working, using accumulated error to move between stages.

The tekmar and the heat pump each keep running their own control loops. A supervisor in Home Assistant watches room temperature against the tekmar's setpoint and tracks how far and how long the room has been off target, in degree-minutes. A persistent shortfall starts an outer PI loop on room temperature whose output is the tekmar's floor minimum, with the tekmar's own floor control as the inner loop. If the floor loop reaches its limit and the room is still short, the heat pump is armed, and it then follows the tekmar reading: a small call for heat while the room reads short, satisfied otherwise. A large gap skips straight to the heat pump for a fast recovery. When the tekmar is holding the room on its own again, the floor minimum is released.

It combines three standard control ideas:

- **Lead/lag staging.** The radiant is the lead source and the heat pump is the lag source, brought on only when the lead cannot hold the load. This is the pattern a two-stage thermostat or a multi-boiler plant uses.
- **Integral (degree-minute) staging.** Stage changes are driven by accumulated error, not instantaneous error, so a small persistent shortfall eventually escalates and a brief dip does not.
- **Supervisory (cascade) control.** The supervisor does not switch equipment directly. It adjusts the setpoints of the local controllers (floor minimum, heat pump setpoint) and lets them do the fast work. This is why a failure leaves both devices at sane settings.

The tekmar is a proportional-only controller, which is why it droops below setpoint under load. The boost stage supplies the missing integral action from outside: a cascade in which a PI loop on room temperature sets the floor target and the tekmar holds the floor near it.

It is not a PID loop in the usual sense, and it is not model-predictive. It has no thermal model and makes no forecasts. It reacts only to measured room temperature, which was the goal after the regression approach.

## 1. Purpose

Coordinate the radiant floor and the heat pump in the Living Room and Den so that:

- The radiant floor carries as much of the load as it can.
- The heat pump runs only when the radiant cannot hold or reach setpoint in a reasonable time.
- The room does not overshoot setpoint.
- A loss of Home Assistant or of either thermostat leaves both devices at ordinary settings.

It replaces two earlier automations, both now removed:

- `automation.radiant_floor_minimum_from_outdoor_temperature`, which sets the floor minimum from a fitted heat-loss regression.
- The automation that turns the heat pump on and off from outdoor temperature alone.

## 2. Why the current approach falls short

| Problem | Cause |
|---|---|
| Room overshoots setpoint | The regression floor minimum is open-loop. It never looks at room temperature, so internal gains (occupancy, appliances, sun) become overshoot. On Nov 25, with the heat pump on, the room averaged 70.7 F and peaked at 71.9 F against a 70 F setpoint. |
| Heat pump cannot help after a setback | It is switched by outdoor temperature only. On Nov 22 the radiant alone took about 4 hours to bring the room from 67 F to 70 F. |
| Heat pump runs when not needed | Same cause. |
| Room sits about 1 F low for hours overnight | The tekmar 561 is a proportional controller (see below). |

### Finding: the tekmar 561 droops by design

In radiant mode the 561 sets its on-time from the distance to setpoint: 100% at 1.5 F below, 50% at setpoint, 0% at 1.5 F above. The manual describes no integral correction and no setting to change this.

Consequences:

- The room holds setpoint exactly only when the load happens to equal 50% on-time. A heavier load settles below setpoint.
- Call duty is not a usable "radiant is maxed out" signal. In a week of data it reached 90% in 3 hours out of 117.
- During three overnight shortfalls (Nov 19, 20, 24) duty averaged 58% and the floor ran 68-78 F against an 85 F cap. The radiant had headroom it was not using.

The floor minimum is the lever that overrides this: the manual states the floor minimum overrides the air temperature.

Source: tekmar WiFi Thermostat 561 Installation, Operation and Maintenance manual (IOM-T-561), https://aerco.com/dfsmedia/0533dbba17714b1ab581ab07a4cbb521/488444-source/iom-t-561-2215

## 3. Design principles

1. **The tekmar air setpoint is the single source of truth.** The controller reads it and never writes it. Changes at the wall and from presets are honored on the next run.
2. **Two outputs per zone.** The tekmar floor minimum, and the heat pump's mode and setpoint.
3. **Feedback, not a heat-loss model.** Decisions come from measured room temperature against setpoint. The only weather input is the freeze-protection threshold.
4. **Radiant first.** A persistent shortfall first pushes the radiant harder (boost), and the floor minimum that results is kept and trimmed slowly so the radiant goes on carrying the base load. The heat pump comes in only if that is not enough (assist), or for a large gap (recovery).
5. **Sane settings at all times.** The heat pump is only ever set within a degree or two of the room setpoint or of its own reading. No extreme setpoints are used as switches.
6. **State lives in helpers.** Every run recomputes from helpers and live readings, so a restart resumes where it left off.

## 4. Entities

| Role | Living Room | Den |
|---|---|---|
| Heat pump | `climate.living_room` | `climate.den` |
| Radiant air thermostat (setpoint) | `climate.living_room_radiant` | `climate.den_radiant` |
| Radiant floor (floor minimum, cap) | `climate.living_room_radiant_floor` | `climate.den_radiant_floor` |
| Room temperature (level) | `sensor.living_room_radiant_temperature` | `sensor.den_radiant_temperature` |
| Outdoor temperature | `sensor.outside_gw3000b_outdoor_temp_24h_average` | same |

The room temperature entity is a per-zone parameter. Both zones use the tekmar's own air reading, which is also what the tekmar controls on.

An earlier draft used the Living Room AirGradient as a rate-of-change input. That was dropped: a replay of one week of data showed the same boost timing with or without it, apart from one extra boost entry after a return from away. The controller now has a single room input per zone.

### Which entities show a real heat call

Only `hvac_action` on the main radiant climate entity (`climate.<zone>_radiant`) reflects the tekmar's actual heat call. `binary_sensor.<zone>_radiant_radiant_heating` and the status of the `_floor` climate entity do not: over 12 hours on Nov 25-26 the binary sensor changed state once while `hvac_action` cycled every 12-14 minutes. The controller uses neither; it reads only setpoints and temperatures.

## 5. Stages

| Stage | Heat pump | Floor minimum | Meaning |
|---|---|---|---|
| `radiant_only` | Off, or resting at setpoint - 2 F in freeze-risk weather | Always 0 (disabled) | The tekmar runs on its own air control. |
| `boost` | Off, or resting as above | Set by the floor loop (section 7) | The outer PI loop is active. This is a long-lived state and is expected to be the normal winter state for the Living Room. |
| `assist` | Heat, armed, following the tekmar reading (section 8) | Set by the floor loop, with the learned level frozen | The floor loop is at its limit and the room is still short. |
| `recovery` | Heat, at setpoint - 1 F | Left exactly as it is | Large gap. The heat pump closes most of it; the radiant, already calling, finishes. |

Each stage means one thing. In particular, `radiant_only` never carries a floor minimum; whenever a floor minimum is in use, the stage is `boost` or `assist`.

### Transitions

| From | To | Condition |
|---|---|---|
| `radiant_only` | `boost` | Staging integrator at or above +30. The learned floor level starts at the current floor reading. |
| `radiant_only` or `boost` | `recovery` | Room 2 F or more below setpoint, and the heat pump may start |
| `recovery` | `boost` | Room within 1 F of setpoint, at least 15 minutes in recovery, and a floor minimum is set |
| `recovery` | `radiant_only` | Same, but the floor minimum is 0 |
| `boost` | `assist` | Room reads short, staging integrator at or above +60, and the heat pump may start |
| `assist` | `boost` | Room not short, and 20 minutes since it last read short. The staging integrator restarts at +30. |
| `boost` | `radiant_only` | Room has not read short for 3 hours, and the floor minimum is at its lower limit (2 F under the floor reading) |

Notes:

- "The heat pump may start" means it is already in heat mode (resting), or has been off for at least 15 minutes.
- "Short" means the room reads 1 F or more below setpoint.
- Recovery returns to wherever the floor minimum says it belongs. After a setback (floor minimum 0) that is `radiant_only`. If recovery interrupted `boost`, the minimum that was already working is kept.
- Boost ends when the tekmar is doing well on its own: the room has not read short for a long time, and the floor minimum is sitting at its lower limit, where it is not holding the floor up. If the room is fine because the minimum is holding the floor up, boost is doing its job and continues.
- In boost the staging integrator only counts shortfall the floor loop could not absorb (section 6), so the heat pump is armed only when the floor loop is at its upper limit and the room is still short.

## 6. Integrating the error

Each run computes one increment: the room error multiplied by the minutes since the last run. Because the automation runs on every change of the tekmar reading, a reading that flickers between two values is weighted by how long each one lasted. The increment is zero while the room reads at setpoint.

It feeds two accumulators, which need different memory:

| Accumulator | Units | Leaks at setpoint | Reset | Limited |
|---|---|---|---|---|
| Floor level | F, at 1 F per 90 degree-minutes | No | Set to the floor reading on entering boost | Kept within the floor limits (section 7) |
| Staging integrator | Degree-minutes, signed | Yes, 0.25 per minute | On a stage change (to +30 on leaving assist) | +/-1000 |

How the increment is routed:

| Stage | Floor level | Staging integrator |
|---|---|---|
| `boost`, level learning | Takes as much of the increment as its limits allow | Gets only the part the level could not take |
| `boost`, level frozen (45 minutes after a heat pump run) | Unchanged | Gets the whole increment |
| `radiant_only`, `recovery`, `assist` | Unchanged | Gets the whole increment |

So in boost the staging integrator means "shortfall the floor loop is not absorbing". What it triggers:

| In stage | Staging integrator reaches | Action |
|---|---|---|
| `radiant_only` | +30 | Enter boost |
| `boost` | +60 | Arm the heat pump (assist) |

- **Why the staging integrator leaks and does not reset:** the tekmar reports whole degrees, so a room that is really 0.5 F short flickers between "at setpoint" and "1 F short". Resetting at setpoint would mean it never accumulates.
- **Carry after assist:** on leaving assist it restarts at +30, half way to the +60 that arms the heat pump, not 0. If the room sags again soon, the heat pump is re-armed after about 30 more degree-minutes. If the room holds, the leak fades that memory in about 2 hours. The carry must stay below `assist_start_degree_minutes`.
- **Time to the first heat pump run:** with the room steadily 1 F short and the floor not rising, the level takes about 90 minutes to reach its upper limit and the staging integrator then needs another 60, so about 2.5 hours. If the floor is still rising, the level keeps absorbing and the heat pump is armed later.
- Elapsed minutes are capped at 10 per run, so an outage cannot add a large amount.

## 7. The floor loop (boost and assist)

The tekmar modulates its heat call around the floor minimum as well as around the air setpoint, and it does so on a finer temperature reading than it reports. Observed:

| Floor minimum vs. floor reading | tekmar behavior |
|---|---|
| 0 (disabled), room at setpoint | Pulses of 3-7 minutes in every 14 or so |
| At or 1 F below the reading | Steady pulsing, about 10 minutes on and 4 off |
| 2 F above the reading | Continuous call (100-131 minutes observed) |

So the floor minimum is the setpoint of an inner proportional loop on floor temperature, and it saturates about 2 F above the reading. The controller runs an outer PI loop on room temperature and writes its output to the floor minimum:

    floor minimum = learned level                 (I)
                  + 1 F if the room reads short   (P)
                  - 1 F if the room reads over

- **I, the learned level.** A stored value in F (a helper per zone). It is the floor temperature that holds the room at setpoint. It moves 1 F per 90 degree-minutes of error, in either direction.
- **P, the nudge.** Applied the moment the room reads short or over, and removed the moment it returns to setpoint. This is what reacts at a setpoint crossing; the level does not have to unwind first.
- **Limits.** The floor minimum, and the level, are kept between the floor reading - 2 F and the floor reading + 1 F, and under the floor cap. That keeps the tekmar's floor control modulating: further above and it calls continuously, further below and it contributes nothing. Shortfall the level cannot take because it is at its upper limit goes to the staging integrator instead (section 6).
- **Frozen around heat pump runs.** The level is not updated in assist or for 45 minutes after the heat pump stops, so heat pump overshoot does not unwind it.

The thermostat takes whole degrees, so the output is rounded. Each degree of floor moves the room about 0.5 F.

Because the output cannot be more than 1 F above the floor reading, the level never runs far ahead of the floor. While the room reads short the minimum sits at that limit and it is the floor itself that climbs. The stored level matters most at setpoint: it holds the target steady while the floor settles slightly below it, where a rule tied to the floor reading would follow the floor down.

In `recovery` the floor minimum and the level are not touched. In `radiant_only` the floor minimum is always 0.

No derivative term is used yet. It would need a sub-degree room reading; see section 12 for the sensor comparison.

## 8. Heat pump setpoint and sensor offset

### In assist: follow the tekmar

The heat pump's own thermostat (a wireless MHK2) does not see what the tekmar sees. On the first day it rose from 63 to 65 F while the tekmar went from 64 to 67 F. In assist the tekmar reading therefore decides when the heat pump heats, and the heat pump's setpoint is placed relative to its own reading:

| Room reads | Heat pump setpoint |
|---|---|
| Short | Its own reading + 1 F (a small call for heat) |
| At or above setpoint | Its own reading - 1 F (satisfied, but still in heat mode) |

Assist ends 20 minutes after the room last read short. Then the heat pump is turned off and the stage returns to boost.

One rule covers both situations:

- **Radiant maxed out:** the room reads short again within the 20 minutes, so the heat pump stays armed and resumes at once. It runs for as long as the load requires, with no mode cycling.
- **Radiant just catching up:** the heat pump stops calling when the tekmar reads setpoint, the room holds, and it is switched off 20 minutes later.

The automation also runs whenever the tekmar reading changes, so the heat pump is cut within seconds of the room reaching setpoint, not at the next 5-minute tick.

If the heat pump's reading cannot be trusted (see below), assist falls back to setpoint + learned offset when calling, and 2 F below that when satisfied.

### Learned offset

The controller keeps a slow running average of (heat pump reading - room reading), clamped to +/-3 F. It is used for the recovery and freeze-rest setpoints, which are expressed in tekmar terms:

| Situation | Heat pump setpoint |
|---|---|
| `recovery` | Setpoint - 1 F + offset |
| Resting (freeze-risk weather) | Setpoint - 2 F + offset |

- The MHK2 does not depend on airflow through the unit, so the offset is learned in every mode, including off.
- If the wireless link drops, the unit falls back to its internal sensor, which can be far off (for example warm coils from other zones calling while this unit is off). Any reading more than 3 F from the tekmar reading (`offset_max_deviation`) is treated as that fallback: it is ignored for learning and not used to place the assist setpoint.
- During such a dropout the heat pump is controlling on its internal sensor. The controller does not detect or correct for this beyond not trusting the reading.

### Freeze protection

When the heat pump is not needed (`radiant_only` or `boost`):

- 24-hour outdoor average below `freeze_protect_outdoor_temp`, or outdoor reading missing: rest in heat mode at setpoint - 2 F.
- Otherwise: off.

The threshold is the outdoor temperature below which the radiant alone could not hold about 60 F in an unoccupied house. For the Living Room the earlier regression puts that near 26 F; the controller uses 30 F for margin. The Den is set to -20 F, which effectively disables it. There is 2 F of hysteresis.

## 9. Failure behavior

| Event | Behavior |
|---|---|
| Thermostat or heat pump unavailable | The zone is skipped for that run. Nothing is written. |
| A required reading is missing | The zone is skipped for that run. |
| Radiant thermostat set to off | Floor minimum set to 0, stage reset, heat pump left alone. |
| Heat pump in cool, dry or fan | The heat pump is left alone. |
| Controller disabled for a zone | Nothing is written; devices keep their current settings, including the floor minimum. |
| Home Assistant restarts | Stage, integrator, offset and timestamps are restored from helpers. The automation also runs at start. |
| Home Assistant down, in `radiant_only` or `recovery` | Both devices sit at ordinary settings. |
| Home Assistant down, in `boost` | The floor minimum stays where it was. This can overheat the room if the weather warms. It is the accepted risk of using the floor minimum, and is the same exposure the earlier regression control had. |
| Home Assistant down, in `assist` | As for boost, and the heat pump is left in heat mode within 1 F of its own reading. |
| Home Assistant down in freeze-risk weather | The heat pump is already resting at setpoint - 2 F and acts as a backstop. |

## 10. Helpers

Per zone, where `<zone>` is `living_room` or `den`. Fourteen helpers in total, all created.

| Entity | Type | Configuration |
|---|---|---|
| `input_boolean.hsc_<zone>_enabled` | Toggle | |
| `input_select.hsc_<zone>_stage` | Dropdown | Options: `radiant_only`, `recovery`, `boost`, `assist` |
| `input_datetime.hsc_<zone>_stage_since` | Date and time | When the current stage began |
| `input_datetime.hsc_<zone>_last_short` | Date and time | Last run on which the room read 1 F or more below setpoint, in any stage |
| `input_number.hsc_<zone>_integral` | Number | Staging integrator. Min -1000, max 1000, step 0.1 |
| `input_number.hsc_<zone>_floor_level` | Number | Learned floor level in F. Min 0, max 100, step 0.01 |
| `input_number.hsc_<zone>_hp_offset` | Number | Min -3, max 3, step 0.01 |

None of these has an initial value set, so Home Assistant restores the last value on restart.

## 11. Tunables

Set per zone at the top of the automation.

| Name | Default | Meaning |
|---|---|---|
| `freeze_protect_outdoor_temp` | 30 (Living Room), -20 (Den) | Below this 24-hour outdoor average, an idle heat pump rests in heat mode |
| `rest_droop` | 2 | Resting heat pump target, in F below setpoint |
| `recovery_start_error` | 2 | Enter recovery at this many F below setpoint |
| `recovery_stop_error` | 1 | Recovery stops this many F short of setpoint |
| `min_minutes_recovery` | 15 | Minimum time in recovery |
| `min_minutes_heat_pump_off` | 15 | Minimum heat pump off time before it may start |
| `boost_start_degree_minutes` | 30 | Shortfall that starts boost |
| `assist_start_degree_minutes` | 60 | Unabsorbed shortfall, in boost, that arms the heat pump |
| `assist_carry_degree_minutes` | 30 | Staging integrator value on leaving assist. Keep below `assist_start_degree_minutes`. |
| `integral_decay_at_setpoint` | 0.25 | Degree-minutes lost per minute at setpoint |
| `assist_hold_minutes` | 20 | Assist ends this long after the room last read short |
| `assist_call_delta` | 1 | Heat pump setpoint offset from its own reading in assist |
| `assist_blanking_minutes` | 45 | The floor level stays frozen this long after the heat pump stops |
| `integral_minutes_per_degree` | 90 | The floor level moves 1 F per this many degree-minutes of error |
| `floor_proportional_step` | 1 | Floor minimum nudge, in F, while the room reads short or over |
| `floor_max_lead` | 1 | The floor minimum is never more than this above the measured floor |
| `floor_max_lag` | 2 | ...or more than this below it |
| `boost_release_minutes` | 180 | Boost may end after this long without a short reading |
| `offset_learning_rate` | 0.05 | Weight of each new offset observation |
| `offset_max_deviation` | 3 | Heat pump readings further than this from the room reading are not trusted |

## 12. Test status and revision history

### Revision 1: first day of live operation (2026-11-26, 11:30-20:00)

Living Room, setpoint 65 F, outdoor 45-47 F. Two nearly identical cycles about 5.5 hours apart:

| Step | Cycle 1 | Cycle 2 |
|---|---|---|
| Room drops to 64 F | 11:56 | 17:21 |
| Boost starts | 12:25 | 17:50 |
| Floor rises its first 1 F | 13:42 (77 min in) | 19:32 (102 min in) |
| Assist starts (90-minute limit) | 13:55 | 19:20 |
| Room reaches 65 F | 14:00 (5 min) | 19:28 (8 min) |
| Room peaks at 67 F | 14:11 | 19:41 |
| Heat pump off | 14:20 (25 min run) | 19:45 (25 min run) |
| Boost drops to radiant only | 14:30 | 20:00 |

The Den stayed in `radiant_only` all day at 66-67 F.

What this showed:

- **Heat pump overshoot.** The room reached setpoint 5-8 minutes after the heat pump started, but revision 1 needed 20 degree-minutes above setpoint before leaving assist, so the heat pump ran 25 minutes and the room went 2 F over.
- **Boost needs more time.** During boost the tekmar called continuously, but the boiler supply sat near 120 F for the first 15 minutes and then climbed slowly, reaching about 170 F after 100 minutes. The floor's first degree arrived just as assist cut in.
- **Fast de-escalation was pointless.** The floor minimum stepped down 2 F per run from 79 F against a floor at 77 F, so the radiant shut off on the first step.
- **Dropping the floor minimum to 0 caused the cycle.** With no floor minimum the tekmar's own loop ran only 20-30% duty at setpoint. The room sagged, boost had to drive the floor back up from behind, the heat pump covered the gap and overshot, and the floor minimum was dumped again.

Compared with the regression control overnight on Nov 25-26 (same 65 F setpoint, outdoor in the mid-40s, floor minimum held at 76-77 F, heat pump in heat mode at 65 F):

| | Regression (23:33-07:11) | Revision 1 (11:35-20:11) |
|---|---|---|
| Room range | 65-68 F | 64-67 F |
| Average vs. setpoint | +1.3 F | -0.3 F |
| Time at the most common reading | 68% at 66 F | 49% at 64 F |
| Time within +/-1 F of setpoint | 92% | 93% |
| Time below setpoint | 0% | 49% |
| tekmar call pattern | Steady: about 10 min on, 4 off | About 3 min on, 10 off; then 100 min continuous; then 45 min off |

The regression ran warm but steady, because a sensible floor minimum was held all the time and the radiant kept delivering the base load. Revision 1 was centered better but swung.

### Revision 2 (2026-11-26)

Changes made in response:

1. Boost is a long-lived state that keeps and slowly trims the floor minimum. It ends only when the minimum has been trimmed to within 3 F of the room setpoint.
2. The 2 F-per-run step-down is replaced by a 1 F trim after 30 degree-minutes of excess.
3. The 90-minute boost timer is removed. A single signed integrator arms the heat pump at +120.
4. Assist follows the tekmar reading and ends 20 minutes after the room last read short, with the integrator restarting at +90.
5. Recovery no longer zeroes the floor minimum, and returns to `boost` or `radiant_only` depending on whether one is set.
6. The automation also runs on every change of the tekmar reading.
7. The integrator is plainly signed. It no longer clears the opposite side when the sign changes.
8. A `last_short` helper per zone was added.

Goal: hold the room within +/-1 F of setpoint.

#### Revision 2: first night (2026-11-26 20:00 to 2026-11-27 09:00)

Living Room, setpoint 65 F, outdoor rising from 48 to 54 F. The heat pump never ran.

| Time | Event |
|---|---|
| 20:00-21:55 | Radiant only. Room flickers between 64 and 65 F. |
| 21:55 | Boost starts. Floor minimum set to 82 F (floor + 2). |
| 21:59-00:10 | tekmar calls continuously for 131 minutes. |
| 22:43 | Room reaches 65 F. The minimum is held at 82 F, so the call continues. |
| 23:58-03:12 | Room at 67 F. |
| 00:10-02:55 | Minimum trimmed 1 F every 15 minutes, from 79 to 68 F. No effect; it was below the floor throughout. |
| 03:00 | Boost releases to radiant only. No heat call from 00:18 to at least 08:40. |

What this showed:

- **"Hold" kept pushing.** A minimum 2 F above the floor reading is a full-on command, and it was held after the room reached setpoint.
- **A 2 F lead defeats the tekmar's own modulation.** Within about 1 F of the floor reading the tekmar pulses; at 2 F it calls continuously.
- **The signed integrator delayed the trim.** About +44 degree-minutes accumulated while the room was short had to be worked off before the -30 trim threshold could be reached, about 20 minutes later than otherwise.
- **The trigger-on-change did improve the integrator.** The 64/65 F flicker was weighted by time, which correctly held boost off for 80 minutes.
- **The weather warmed**, which stretched the overshoot. The Den sat at 67-68 F all night in radiant only.

#### The tekmar reading swings more than the room

In the last two events the tekmar moved two to three times as far as either independent sensor in the Living Room:

| Event | tekmar | AirGradient | Hue motion sensor |
|---|---|---|---|
| Boost, night of Nov 26 | +2.7 F | +0.9 F | +0.8 F |
| Heat pump run, Nov 26 14:00 | +2.1 F | +0.7 F | +1.2 F |

The cause is not known. The tekmar remains the reference, since its own loop acts on that reading, but part of a "2 F over" reading may be local to the thermostat.

#### Sub-degree sensors for a future derivative term (72 hours, hourly)

| | AirGradient | Hue, Living Room | Hue, Office (vs. Den tekmar) |
|---|---|---|---|
| Hourly change correlated with tekmar | 0.77 | 0.78 | 0.47 |
| tekmar change per 1 F sensor change | 1.7 F | 1.0 F | 0.5 F |
| Update rate | Every minute | Every 5-10 minutes | Every 5-10 minutes |
| Step size | 0.02 F | 0.18 F | 0.18 F |

The AirGradient is the better rate source for the Living Room: the Hue's 0.18 F steps would put about +/-0.5 F per hour of noise on a 30-60 minute rate. The Office sensor sees swings the Den tekmar does not and is not usable for the Den.

### Revision 3 (2026-11-27)

Changes made in response:

1. The floor minimum is set by an outer PI loop on room temperature (section 7), replacing the push/hold/trim rules.
2. The floor minimum is limited to between the floor reading - 2 F and + 1 F, so the tekmar's own floor control keeps modulating.
3. A learned floor level per zone holds the integral term. It is frozen in assist and for 45 minutes after the heat pump stops.
4. The staging integrator, in boost, counts only shortfall the floor loop could not absorb. The heat pump is armed at +60 of that.
5. Boost ends when the room has not read short for 3 hours and the floor minimum is at its lower limit, replacing the "within 3 F of the room setpoint" test, which the new limits made unreachable.
6. A `floor_level` helper per zone was added.

Checked for revision 3: the YAML parses, and the new expressions were evaluated against live entities in the Home Assistant template engine.

Not yet established:

- Revision 3 has not run end to end.
- The width of the tekmar's floor modulation band. It has been seen saturated at +2 F and modulating at 0 to -1 F, and nothing in between. The +1 F upper limit assumes it still modulates there.
- Whether "level, no nudge" at setpoint is neutral. On the regression night a minimum at the floor reading ran about 70% duty and the room sat 1 F high.
- How often the floor minimum is rewritten when the room reading flickers, and whether the tekmar integration tolerates it.
- Whether a 1 F call in assist is gentle enough, and how the room behaves when the radiant is maxed out and the heat pump is carrying load. Assist has not run since revision 2.
- Behavior in weather cold enough for the floor to approach its cap.

Open observation from the backtest: on the Nov 22 return the room was 2-3 F low but recorded call duty was 80%, where the tekmar manual implies 100%. This may be the 0.1-hour resolution of the heat-hours counter.

## 13. Rollout

Done:

1. The fourteen helpers in section 10 were created.
2. The scripts and the automation were added and loaded (revision 1, 2026-11-26).
3. The regression floor-minimum automation and the outdoor-temperature heat pump automation were removed.
4. A "Heat stage controller" section was added to the Heating Plant dashboard view, including "Last read short" and "Floor level (learned)" tiles per zone.
5. Both zones were enabled at 11:30 on 2026-11-26.

To deploy revision 3:

1. Pull the repo on the Home Assistant host.
2. Reload scripts and automations.
3. No helper values need to be reset. The stage and integrator carry over.

Things to watch and tune first:

- `floor_max_lead`: the key assumption. If the tekmar calls continuously at +1 F, lower it to 0; if the floor responds too slowly, it cannot usefully go above 1.
- `integral_minutes_per_degree`: lower it if the level is slow to find the right floor temperature; raise it if the room cycles slowly around setpoint.
- `assist_start_degree_minutes`: lower it if the room sits 1 F low for too long before the heat pump is armed; raise it to give the floor more time.
- `boost_release_minutes`: shorten it if boost lingers in mild weather.
- `assist_hold_minutes`: shorten it if the idle-but-armed period is a nuisance; lengthen it if the heat pump is being switched off and back on.
- Heat pump heat presets for these two zones are overwritten within 5 minutes while a zone is enabled. Radiant presets work as before.

## 14. Code

Copies of what is in the repo, for reference. The repo files are authoritative.

### 14.1 Scripts (`scripts.yaml`)

```yaml
# =============================================================================
# Heat stage controller - SCRIPTS (go in scripts.yaml)
#
# Small helpers used by the "Heat stage controller" automation. Each one only
# writes to a device or helper when the value actually needs to change.
# =============================================================================

# Record a stage change: set the stage, stamp the time, and set the integrator
# (to 0 unless an "integral" value is passed).
hsc_set_stage:
  alias: "HSC: set stage"
  mode: queued
  fields:
    zone_id:
      description: Zone id used in the helper names, e.g. living_room
      required: true
    stage:
      description: radiant_only, recovery, boost or assist
      required: true
    integral:
      description: Integrator value to start the new stage with (default 0)
      required: false
  sequence:
    - action: input_select.select_option
      target:
        entity_id: "input_select.hsc_{{ zone_id }}_stage"
      data:
        option: "{{ stage }}"
    - action: input_datetime.set_datetime
      target:
        entity_id: "input_datetime.hsc_{{ zone_id }}_stage_since"
      data:
        timestamp: "{{ now().timestamp() }}"
    - action: input_number.set_value
      target:
        entity_id: "input_number.hsc_{{ zone_id }}_integral"
      data:
        value: "{{ integral | default(0) }}"

# Put the heat pump in the wanted mode and, when heating, at the wanted setpoint.
hsc_set_heat_pump:
  alias: "HSC: set heat pump"
  mode: queued
  fields:
    heat_pump:
      description: Heat pump climate entity
      required: true
    mode:
      description: heat or off
      required: true
    setpoint:
      description: Heat pump setpoint in F (only used when mode is heat)
      required: false
  sequence:
    # Only act when the heat pump is in heat or off. If it is offline, or
    # someone put it in cool, dry or fan, leave it alone.
    - condition: template
      value_template: "{{ states(heat_pump) in ['heat', 'off'] }}"

    - if:
        - "{{ states(heat_pump) != mode }}"
      then:
        - action: climate.set_hvac_mode
          continue_on_error: true
          target:
            entity_id: "{{ heat_pump }}"
          data:
            hvac_mode: "{{ mode }}"
        # Give the unit a moment before sending the setpoint
        - delay: 5

    - if:
        - "{{ mode == 'heat' }}"
        - "{{ state_attr(heat_pump, 'temperature') | float(-99) != setpoint | float }}"
      then:
        - action: climate.set_temperature
          continue_on_error: true
          target:
            entity_id: "{{ heat_pump }}"
          data:
            temperature: "{{ setpoint | int }}"

# Set the tekmar floor minimum (0 disables it).
hsc_set_floor_minimum:
  alias: "HSC: set floor minimum"
  mode: queued
  fields:
    floor:
      description: tekmar floor climate entity
      required: true
    floor_minimum:
      description: Floor minimum in F; 0 disables it
      required: true
  sequence:
    - condition: template
      value_template: "{{ state_attr(floor, 'temperature') | float(-1) != floor_minimum | float }}"
    - action: climate.set_temperature
      continue_on_error: true
      target:
        entity_id: "{{ floor }}"
      data:
        temperature: "{{ floor_minimum | int }}"
```

### 14.2 Automation (`automations_external.yaml`)

```yaml
# =============================================================================
# Heat stage controller - AUTOMATION
# Design: docs/heat_stage_controller_design.md
#
# Coordinates each zone's radiant floor (tekmar) and heat pump (Mitsubishi).
#
# The tekmar air setpoint is the single source of truth and is never written
# here. The controller only writes:
#   - the tekmar floor minimum
#   - the heat pump's mode and setpoint
#
# Stages (stored in input_select.hsc_<zone>_stage):
#   radiant_only  Radiant runs on the tekmar's own air control. Floor minimum
#                 is always 0 here. Heat pump is off (or resting in
#                 freeze-risk weather).
#   boost         The floor minimum is under management by an outer PI loop
#                 on room temperature (the tekmar's own floor control is
#                 the inner loop). Heat pump is off. This is a long-lived
#                 state.
#   assist        Boost alone is not holding the room. The heat pump is armed
#                 and follows the tekmar reading: a small call for heat while
#                 the room reads short, satisfied otherwise. Ends once the
#                 room has not read short for a while.
#   recovery      Room is well below setpoint (setback recovery). Heat pump
#                 runs to 1 F short of setpoint; the radiant finishes the rest.
#
# Two accumulators per zone are fed by the same error x minutes:
#   floor level         the learned floor temperature that holds setpoint
#                       (the "I" term of the floor loop)
#   staging integrator  degree-minutes of shortfall the floor loop is not
#                       absorbing; drives the stage changes
#
# Uses three small scripts from scripts.yaml:
#   script.hsc_set_stage, script.hsc_set_heat_pump, script.hsc_set_floor_minimum
# =============================================================================

- id: hsc_heat_stage_controller
  alias: Heat stage controller
  mode: single
  max_exceeded: silent

  triggers:
    - trigger: time_pattern
      minutes: "/5"
    # React right away to a setpoint change at the wall or from a preset
    - trigger: state
      entity_id:
        - climate.living_room_radiant
        - climate.den_radiant
      attribute: temperature
    # React right away when the room reading changes, so the heat pump is
    # cut as soon as the room reaches setpoint (not up to 5 minutes later)
    - trigger: state
      entity_id:
        - sensor.living_room_radiant_temperature
        - sensor.den_radiant_temperature
    - trigger: homeassistant
      event: start

  variables:
    unavailable_states: ["unknown", "unavailable", "none", "None", ""]

    # Minutes since this automation last ran. Used to accumulate
    # degree-minutes. Capped at 10 so an outage cannot add a large amount.
    minutes_since_last_run: >-
      {% set last_run = this.attributes.last_triggered %}
      {{ [((now() - last_run).total_seconds() / 60) if last_run else 5, 10] | min | round(2) }}

    zones:
      - id: living_room
        heat_pump: climate.living_room
        radiant: climate.living_room_radiant
        floor: climate.living_room_radiant_floor
        room_temp: sensor.living_room_radiant_temperature    # tekmar air reading
        outdoor_temp: sensor.outside_gw3000b_outdoor_temp_24h_average
        # --- tunables ---
        freeze_protect_outdoor_temp: 30   # below this, an idle heat pump rests in heat mode
        rest_droop: 2                     # ...this many F below setpoint
        recovery_start_error: 2           # enter recovery when room is this far below setpoint
        recovery_stop_error: 1            # recovery stops this far short of setpoint
        min_minutes_recovery: 15          # minimum time in recovery
        min_minutes_heat_pump_off: 15     # minimum off time before the heat pump may start
        boost_start_degree_minutes: 30    # shortfall that starts boost
        assist_start_degree_minutes: 60   # unabsorbed shortfall, in boost, that arms the heat pump
        assist_carry_degree_minutes: 30   # integrator value on leaving assist (see step 7); keep below assist_start
        integral_decay_at_setpoint: 0.25  # degree-minutes lost per minute at setpoint
        assist_hold_minutes: 20           # assist ends this long after the room last read short
        assist_call_delta: 1              # heat pump setpoint offset from its own reading in assist
        assist_blanking_minutes: 45       # floor level stays frozen this long after the heat pump stops
        integral_minutes_per_degree: 90   # floor level moves 1 F per this many degree-minutes of error
        floor_proportional_step: 1        # floor minimum nudge (F) while the room reads short or over
        floor_max_lead: 1                 # floor minimum is never more than this above the measured floor
        floor_max_lag: 2                  # ...or more than this below it
        boost_release_minutes: 180        # boost may end after this long without a short reading
        offset_learning_rate: 0.05
        offset_max_deviation: 3           # ignore heat pump readings further than this from the room reading

      - id: den
        heat_pump: climate.den
        radiant: climate.den_radiant
        floor: climate.den_radiant_floor
        room_temp: sensor.den_radiant_temperature            # tekmar air reading
        outdoor_temp: sensor.outside_gw3000b_outdoor_temp_24h_average
        # --- tunables ---
        freeze_protect_outdoor_temp: -20
        rest_droop: 2
        recovery_start_error: 2
        recovery_stop_error: 1
        min_minutes_recovery: 15
        min_minutes_heat_pump_off: 15
        boost_start_degree_minutes: 30
        assist_start_degree_minutes: 60
        assist_carry_degree_minutes: 30
        integral_decay_at_setpoint: 0.25
        assist_hold_minutes: 20
        assist_call_delta: 1
        assist_blanking_minutes: 45
        integral_minutes_per_degree: 90
        floor_proportional_step: 1
        floor_max_lead: 1
        floor_max_lag: 2
        boost_release_minutes: 180
        offset_learning_rate: 0.05
        offset_max_deviation: 3

  actions:
    - repeat:
        for_each: "{{ zones }}"
        sequence:

          # -----------------------------------------------------------------
          # 1. Read everything this zone needs
          # -----------------------------------------------------------------
          - variables:
              zone: "{{ repeat.item }}"
              enabled_entity: "input_boolean.hsc_{{ repeat.item.id }}_enabled"
              stage_entity: "input_select.hsc_{{ repeat.item.id }}_stage"
              stage_since_entity: "input_datetime.hsc_{{ repeat.item.id }}_stage_since"
              last_short_entity: "input_datetime.hsc_{{ repeat.item.id }}_last_short"
              integral_entity: "input_number.hsc_{{ repeat.item.id }}_integral"
              floor_level_entity: "input_number.hsc_{{ repeat.item.id }}_floor_level"
              offset_entity: "input_number.hsc_{{ repeat.item.id }}_hp_offset"

          - variables:
              radiant_mode: "{{ states(zone.radiant) }}"
              heat_pump_mode: "{{ states(zone.heat_pump) }}"
              floor_state: "{{ states(zone.floor) }}"
              stage: "{{ states(stage_entity) }}"

              setpoint: "{{ state_attr(zone.radiant, 'temperature') }}"
              room_temp: "{{ states(zone.room_temp) }}"
              heat_pump_reading: "{{ state_attr(zone.heat_pump, 'current_temperature') }}"
              floor_temp: "{{ state_attr(zone.floor, 'current_temperature') }}"
              floor_cap: "{{ state_attr(zone.floor, 'max_temp') }}"
              floor_minimum_now: "{{ state_attr(zone.floor, 'temperature') }}"

              # These fall back to a neutral value if missing
              outdoor_temp: "{{ states(zone.outdoor_temp) }}"
              integral: "{{ states(integral_entity) | float(0) }}"
              heat_pump_offset: "{{ states(offset_entity) | float(0) }}"

          - variables:
              devices_available: >-
                {{ radiant_mode not in unavailable_states
                   and heat_pump_mode not in unavailable_states
                   and floor_state not in unavailable_states
                   and stage in ['radiant_only', 'recovery', 'boost', 'assist'] }}
              readings_valid: >-
                {{ is_number(setpoint) and is_number(room_temp)
                   and is_number(floor_temp) and is_number(floor_cap)
                   and is_number(floor_minimum_now) }}

          # -----------------------------------------------------------------
          # 2. Decide whether to act on this zone at all
          # -----------------------------------------------------------------
          - choose:

              # Controller disabled, or a device is offline: do nothing this
              # run. Devices keep their last (sane) settings.
              - conditions:
                  - "{{ not is_state(enabled_entity, 'on') or not devices_available }}"
                sequence: []

              # Radiant thermostat is off (summer): release the floor minimum,
              # reset the stage, and leave the heat pump alone.
              - conditions:
                  - "{{ radiant_mode != 'heat' }}"
                sequence:
                  - action: script.hsc_set_floor_minimum
                    data:
                      floor: "{{ zone.floor }}"
                      floor_minimum: 0
                  - if:
                      - "{{ stage != 'radiant_only' }}"
                    then:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: radiant_only

              # A reading is missing: skip this run and try again next time.
              - conditions:
                  - "{{ not readings_valid }}"
                sequence: []

            # Normal case: run the controller
            default:

              # -------------------------------------------------------------
              # 3. Derived values
              # -------------------------------------------------------------
              - variables:
                  # Positive error = room is below setpoint
                  error: "{{ setpoint - room_temp }}"

                  minutes_in_stage: >-
                    {% set since = states(stage_since_entity) %}
                    {{ ((now() - (since | as_datetime | as_local)).total_seconds() / 60)
                       if since not in unavailable_states else 9999 }}

                  # Minutes since the room last read 1 F or more below setpoint
                  minutes_since_last_short: >-
                    {% set last_short = states(last_short_entity) %}
                    {{ ((now() - (last_short | as_datetime | as_local)).total_seconds() / 60)
                       if last_short not in unavailable_states else 9999 }}

                  # The heat pump may be started if it is already in heat mode
                  # (resting), or has been off long enough.
                  heat_pump_can_start: >-
                    {{ heat_pump_mode == 'heat'
                       or (now() - states[zone.heat_pump].last_changed).total_seconds() / 60
                          >= zone.min_minutes_heat_pump_off }}

                  # The heat pump reads from a wireless MHK2 thermostat. If
                  # that link drops, the unit falls back to its internal
                  # sensor, which can be far off. A reading that differs from
                  # the room reading by more than offset_max_deviation is
                  # treated as that fallback and not trusted.
                  heat_pump_reading_trusted: >-
                    {{ is_number(heat_pump_reading)
                       and (heat_pump_reading - room_temp) | abs <= zone.offset_max_deviation }}

              # Values for the floor loop (steps 6 and 8a)
              - variables:
                  # Degree-minutes of error since the last run. Zero while
                  # the room reads at setpoint.
                  error_increment: >-
                    {{ (error * minutes_since_last_run) if (error >= 1 or error <= -1) else 0 }}

                  # The learned floor level. If it has never been set, start
                  # from the measured floor temperature.
                  floor_level: >-
                    {{ states(floor_level_entity) | float(0)
                       if states(floor_level_entity) | float(0) > 0 else floor_temp }}

                  # The floor minimum is kept close to the measured floor so
                  # the tekmar's own floor control keeps modulating. Further
                  # above than this and it calls continuously; further below
                  # and it contributes nothing.
                  floor_upper_limit: "{{ [floor_temp + zone.floor_max_lead, floor_cap] | min }}"
                  floor_lower_limit: "{{ floor_temp - zone.floor_max_lag }}"

                  # The floor level is only learned in boost. It is frozen
                  # while the heat pump is armed and for a while after it
                  # stops, so heat pump overshoot does not unwind it.
                  floor_level_learning: >-
                    {{ stage == 'boost'
                       and not (heat_pump_mode == 'off'
                                and (now() - states[zone.heat_pump].last_changed).total_seconds() / 60
                                    < zone.assist_blanking_minutes) }}

              # -------------------------------------------------------------
              # 4. Learn the heat pump's sensor offset
              #    (heat pump reading minus room reading, slow running
              #    average). Used for the recovery and freeze-rest setpoints.
              # -------------------------------------------------------------
              - if:
                  - "{{ heat_pump_reading_trusted }}"
                then:
                  - variables:
                      blended_offset: >-
                        {{ heat_pump_offset
                           + zone.offset_learning_rate * ((heat_pump_reading - room_temp) - heat_pump_offset) }}
                  - action: input_number.set_value
                    target:
                      entity_id: "{{ offset_entity }}"
                    data:
                      value: "{{ [[blended_offset, -3] | max, 3] | min | round(2) }}"

              # -------------------------------------------------------------
              # 5. Remember when the room last read short
              # -------------------------------------------------------------
              - if:
                  - "{{ error >= 1 }}"
                then:
                  - action: input_datetime.set_datetime
                    target:
                      entity_id: "{{ last_short_entity }}"
                    data:
                      timestamp: "{{ now().timestamp() }}"

              # -------------------------------------------------------------
              # 6. Integrate the error: one increment, two accumulators
              #
              #    Floor level (F): the slow "I" term of the floor loop. It
              #    moves 1 F per integral_minutes_per_degree of error, is
              #    kept within the floor limits, and never leaks.
              #
              #    Staging integrator (degree-minutes, signed): shortfall
              #    the floor loop is NOT absorbing. While the level is
              #    learning, that is only the part of the increment the
              #    level could not take because it is at a limit. Otherwise
              #    it is the whole increment. While the room reads at
              #    setpoint it leaks slowly toward zero.
              # -------------------------------------------------------------
              - variables:
                  level_wanted: "{{ floor_level + error_increment / zone.integral_minutes_per_degree }}"
              - variables:
                  level_new: "{{ [[level_wanted, floor_lower_limit] | max, floor_upper_limit] | min }}"
              - variables:
                  staging_increment: >-
                    {{ ((level_wanted - level_new) * zone.integral_minutes_per_degree)
                       if floor_level_learning else error_increment }}

              # Store the floor level
              - if:
                  - "{{ floor_level_learning }}"
                then:
                  - action: input_number.set_value
                    target:
                      entity_id: "{{ floor_level_entity }}"
                    data:
                      value: "{{ level_new | round(2) }}"

              # Update the staging integrator
              - choose:

                  # Room is 1 F or more away from setpoint, either side
                  - conditions:
                      - "{{ error_increment != 0 }}"
                    sequence:
                      - action: input_number.set_value
                        target:
                          entity_id: "{{ integral_entity }}"
                        data:
                          value: >-
                            {{ [[integral + staging_increment, 1000] | min, -1000] | max | round(1) }}

                # At setpoint: leak toward zero from either side
                default:
                  - variables:
                      leak: "{{ zone.integral_decay_at_setpoint * minutes_since_last_run }}"
                  - if:
                      - "{{ integral != 0 }}"
                    then:
                      - action: input_number.set_value
                        target:
                          entity_id: "{{ integral_entity }}"
                        data:
                          value: >-
                            {{ ([integral - leak, 0] | max if integral > 0
                                else [integral + leak, 0] | min) | round(1) }}

              # Re-read the integrator after the update
              - variables:
                  integral: "{{ states(integral_entity) | float(0) }}"

              # -------------------------------------------------------------
              # 7. Stage transitions (first matching rule wins).
              #    script.hsc_set_stage sets the integrator to 0 unless an
              #    "integral" value is passed.
              # -------------------------------------------------------------
              - choose:

                  # RADIANT ONLY or BOOST -> RECOVERY
                  # Room is well below setpoint, e.g. back from setback.
                  - conditions:
                      - "{{ stage in ['radiant_only', 'boost'] }}"
                      - "{{ error >= zone.recovery_start_error }}"
                      - "{{ heat_pump_can_start }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: recovery

                  # RADIANT ONLY -> BOOST
                  # Room has been persistently short on the tekmar's own
                  # control. Start managing the floor minimum, with the
                  # learned level starting at the current floor reading.
                  - conditions:
                      - "{{ stage == 'radiant_only' }}"
                      - "{{ integral >= zone.boost_start_degree_minutes }}"
                    sequence:
                      - action: input_number.set_value
                        target:
                          entity_id: "{{ floor_level_entity }}"
                        data:
                          value: "{{ floor_temp }}"
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: boost

                  # RECOVERY -> BOOST
                  # Recovery is done and a floor minimum was already being
                  # managed when it started (recovery does not touch it).
                  - conditions:
                      - "{{ stage == 'recovery' }}"
                      - "{{ error <= zone.recovery_stop_error }}"
                      - "{{ minutes_in_stage >= zone.min_minutes_recovery }}"
                      - "{{ floor_minimum_now > 0 }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: boost

                  # RECOVERY -> RADIANT ONLY
                  # Recovery is done and no floor minimum was in use.
                  - conditions:
                      - "{{ stage == 'recovery' }}"
                      - "{{ error <= zone.recovery_stop_error }}"
                      - "{{ minutes_in_stage >= zone.min_minutes_recovery }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: radiant_only

                  # BOOST -> ASSIST
                  # The floor loop is at its upper limit and the room is
                  # still short: the radiant cannot take up more. Arm the
                  # heat pump.
                  - conditions:
                      - "{{ stage == 'boost' }}"
                      - "{{ error >= 1 }}"
                      - "{{ integral >= zone.assist_start_degree_minutes }}"
                      - "{{ heat_pump_can_start }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: assist

                  # ASSIST -> BOOST
                  # The room has not read short for a while, so the heat pump
                  # is not needed. If the radiant is maxed out the room reads
                  # short again within the hold time and assist simply
                  # continues.
                  # The integrator restarts at the carry value, not 0: if the
                  # room sags again soon, the heat pump comes back quickly;
                  # if the room holds, that memory leaks away.
                  - conditions:
                      - "{{ stage == 'assist' }}"
                      - "{{ error < 1 }}"
                      - "{{ minutes_since_last_short >= zone.assist_hold_minutes }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: boost
                          integral: "{{ zone.assist_carry_degree_minutes }}"

                  # BOOST -> RADIANT ONLY
                  # The tekmar is doing well on its own: the room has not
                  # read short for a long time, and the floor minimum is at
                  # its lower limit, where it is not holding the floor up.
                  # (If the room is fine BECAUSE the minimum is holding the
                  # floor up, boost is doing its job and continues.)
                  - conditions:
                      - "{{ stage == 'boost' }}"
                      - "{{ error < 1 }}"
                      - "{{ minutes_since_last_short >= zone.boost_release_minutes }}"
                      - "{{ floor_minimum_now <= floor_lower_limit }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: radiant_only

              # -------------------------------------------------------------
              # 8. Apply outputs for the stage we are in now.
              #    Stage, offset, integrator and floor level are re-read
              #    because the steps above may have just changed them.
              # -------------------------------------------------------------
              - variables:
                  stage: "{{ states(stage_entity) }}"
                  heat_pump_offset: "{{ states(offset_entity) | float(0) }}"
                  integral: "{{ states(integral_entity) | float(0) }}"
                  floor_level: >-
                    {{ states(floor_level_entity) | float(0)
                       if states(floor_level_entity) | float(0) > 0 else floor_temp }}

              # ---- 8a. Floor minimum ----
              - choose:

                  # RADIANT ONLY: floor minimum is always disabled and the
                  # tekmar runs on its own air control.
                  - conditions:
                      - "{{ stage == 'radiant_only' }}"
                    sequence:
                      - action: script.hsc_set_floor_minimum
                        data:
                          floor: "{{ zone.floor }}"
                          floor_minimum: 0

                  # RECOVERY: leave the floor minimum exactly as it is.
                  - conditions:
                      - "{{ stage == 'recovery' }}"
                    sequence: []

                # BOOST or ASSIST: the floor loop.
                #   floor minimum = learned level (I)
                #                 + a nudge while the room reads short or over (P)
                # kept within the floor limits so the tekmar keeps modulating.
                default:
                  - variables:
                      proportional_nudge: >-
                        {{ zone.floor_proportional_step if error >= 1
                           else (-zone.floor_proportional_step if error <= -1 else 0) }}
                  - variables:
                      floor_minimum_wanted: >-
                        {{ [[floor_level + proportional_nudge, floor_lower_limit] | max, floor_upper_limit] | min }}
                  # The thermostat takes whole degrees
                  - action: script.hsc_set_floor_minimum
                    data:
                      floor: "{{ zone.floor }}"
                      floor_minimum: "{{ floor_minimum_wanted | round(0) | int }}"

              # ---- 8b. Heat pump ----
              - choose:

                  # RECOVERY: run to just short of setpoint
                  - conditions:
                      - "{{ stage == 'recovery' }}"
                    sequence:
                      - action: script.hsc_set_heat_pump
                        data:
                          heat_pump: "{{ zone.heat_pump }}"
                          mode: heat
                          setpoint: "{{ (setpoint - zone.recovery_stop_error + heat_pump_offset) | round(0) | int }}"

                  # ASSIST, room short: a small call for heat. The setpoint is
                  # set just above the heat pump's own reading, so the tekmar
                  # reading decides when it heats and the gap stays small
                  # enough for it to modulate. If its reading cannot be
                  # trusted, fall back to setpoint plus the learned offset.
                  - conditions:
                      - "{{ stage == 'assist' }}"
                      - "{{ error >= 1 }}"
                    sequence:
                      - action: script.hsc_set_heat_pump
                        data:
                          heat_pump: "{{ zone.heat_pump }}"
                          mode: heat
                          setpoint: >-
                            {{ ((heat_pump_reading + zone.assist_call_delta) if heat_pump_reading_trusted
                                else (setpoint + heat_pump_offset)) | round(0) | int }}

                  # ASSIST, room at or above setpoint: satisfied. The setpoint
                  # is set just below the heat pump's own reading so it stops
                  # heating but stays armed in heat mode.
                  - conditions:
                      - "{{ stage == 'assist' }}"
                    sequence:
                      - action: script.hsc_set_heat_pump
                        data:
                          heat_pump: "{{ zone.heat_pump }}"
                          mode: heat
                          setpoint: >-
                            {{ ((heat_pump_reading - zone.assist_call_delta) if heat_pump_reading_trusted
                                else (setpoint + heat_pump_offset - 2)) | round(0) | int }}

                # RADIANT ONLY or BOOST: heat pump is not needed
                default:
                  - variables:
                      # 2 F of hysteresis so the heat pump does not flip
                      # between resting and off around the threshold
                      freeze_threshold: >-
                        {{ zone.freeze_protect_outdoor_temp
                           + (2 if heat_pump_mode == 'heat' else 0) }}

                  - choose:
                      # Freeze-risk weather (or outdoor reading missing): rest
                      # the heat pump in heat mode a little below setpoint, so
                      # it is a safety net if Home Assistant goes down.
                      - conditions:
                          - "{{ not is_number(outdoor_temp) or outdoor_temp < freeze_threshold }}"
                        sequence:
                          - action: script.hsc_set_heat_pump
                            data:
                              heat_pump: "{{ zone.heat_pump }}"
                              mode: heat
                              setpoint: "{{ (setpoint - zone.rest_droop + heat_pump_offset) | round(0) | int }}"
                    default:
                      - action: script.hsc_set_heat_pump
                        data:
                          heat_pump: "{{ zone.heat_pump }}"
                          mode: "off"
```
