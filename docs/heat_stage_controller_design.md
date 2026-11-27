# Heat stage controller: design

Status: active since 2026-11-26 and being tested in live operation for performance and tuning. This document describes revision 4.1 of the control logic: revision 4, written on 2026-11-27 after two days of live data, with the fixes from an external review the same day (section 12). Thresholds and timings are starting values and are expected to change.

## Summary

This is a supervisory staging controller: a small state machine that sits above the two thermostats and decides which heat source should be working, using accumulated error to move between stages.

The tekmar and the heat pump each keep running their own control loops. A supervisor in Home Assistant watches room temperature against the tekmar's setpoint and tracks how far and how long the room has been off target, in degree-minutes. A persistent shortfall starts an outer PI loop on room temperature whose output is the tekmar's floor minimum, with the tekmar's own floor control as the inner loop. If the room stays short while the floor loop is working, the shortfall keeps accumulating and the heat pump is armed (sooner when the floor loop has nothing more to offer). It then follows the tekmar reading: a small call for heat while the room reads short, satisfied otherwise. A large gap skips straight to the heat pump for a fast recovery. When the tekmar is holding the room on its own again, the floor minimum is released.

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
4. **Radiant first.** A persistent shortfall first pushes the radiant harder (boost). The floor level learned there is kept and adjusted slowly, so the radiant goes on carrying the base load. The heat pump comes in only if that is not enough (assist), or for a large gap (recovery).
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
| `assist` | Heat, armed, following the tekmar reading (section 8) | Set by the floor loop, with the learned level frozen | The room has stayed short despite the floor loop. |
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
| `boost` | `radiant_only` | Room has not read short for 3 hours, and the floor minimum is under the floor reading |

Notes:

- "The heat pump may start" means it is already in heat mode (resting), or has been off for at least 15 minutes.
- "Short" means the room reads 1 F or more below setpoint.
- Recovery returns to wherever the floor minimum says it belongs. After a setback (floor minimum 0) that is `radiant_only`. If recovery interrupted `boost`, the minimum that was already working is kept.
- Boost ends when the tekmar is doing well on its own: the room has not read short for a long time, and the floor minimum is under the floor reading, so it is not what is holding the floor up. If the room is fine because the minimum is holding the floor up, boost is doing its job and continues. The learned level starts afresh the next time boost is entered.
- In boost, shortfall counts toward arming the heat pump at half weight while the floor level is learning upward, and in full whenever it is not (section 6).

## 6. Integrating the error

Each run computes one increment: the error that applied over the minutes since the last run, multiplied by those minutes. The increment is zero while the room read at setpoint.

Which error applied depends on what triggered the run:

| Trigger | Error used for the increment |
|---|---|
| This zone's room reading changed | Setpoint minus the reading from before the change |
| This zone's setpoint changed | The setpoint from before the change, minus the reading |
| 5-minute tick, start-up, or the other zone | The current error (nothing changed here since the last run) |

This is what makes a reading that flickers between two values count by how long each one lasted. The automation runs in queued mode so that every change gets its own run. Everything else (stage changes, the floor minimum, the heat pump) acts on the current error.

It feeds two accumulators, which need different memory:

| Accumulator | Units | Leaks at setpoint | Reset | Range |
|---|---|---|---|---|
| Learned floor level | F, at 1 F per 90 degree-minutes | No | Set to the floor reading on entering boost from radiant only | Room setpoint to floor cap |
| Staging integrator | Degree-minutes, signed | Yes, 0.25 per minute | On a stage change (to +30 on leaving assist) | -30 to 1000 |

### The learned floor level

The level is the floor temperature that holds the room at setpoint. It is a real memory: it is not tied to the current floor reading.

It learns only on valid evidence:

- the stage is `boost`, and has been for at least 45 minutes (`level_settling_minutes`). Coming from assist or recovery, the room still holds the heat pump's heat; coming from `radiant_only`, the room has not yet responded to the floor. The pause is timed from the stage change, so it also applies when the heat pump stays in heat mode in freeze-risk weather;
- the room was exactly 1 F off. Larger gaps are treated as disturbances (the fireplace, cooking, sun, a setback, an open door) and teach nothing.

| Room | Floor vs. level | Level |
|---|---|---|
| 1 F short | Floor within 1 F of the level, or above it | Rises. If the floor is above the level, it first jumps up to the floor reading. |
| 1 F short | Floor 1 F or more under the level | Holds. The floor is still climbing toward the level, so the error does not yet say whether the level is enough. Without this the level would run away upward. |
| 1 F over | Anywhere | Falls, at the same slow rate. There is no jump down to the floor reading. |
| At setpoint, or 2 F or more off | Anywhere | Holds |

The asymmetry is deliberate. After the fireplace has been on, the floor is well below the level while the room passes back through "1 F over"; a downward jump would erase the level at exactly the moment it is needed.

### The staging integrator

Shortfall counted toward arming the heat pump. Time over setpoint works it back down and a little below zero, so a room that has just been over is slower to escalate. The negative side is capped at -30 (`staging_integrator_minimum`): enough to add about 30 minutes of patience after a spell over setpoint, but not so much that a long warm spell or an evening with the fireplace on builds up a debt that delays the heat pump.

| Situation | Weight of shortfall |
|---|---|
| `radiant_only`, `recovery`, `assist` | Full |
| `boost`, level learning upward: room 1 F short, past the first 45 minutes of boost, floor within 1 F of the level or above it, and not within 1 F of the cap | Half |
| `boost`, any other time | Full |

Half weight is patience while the floor loop is still working on the problem. Whenever the level cannot learn, the floor loop has nothing more to offer, and the shortfall counts in full.

What it triggers:

| In stage | Reaches | Action |
|---|---|---|
| `radiant_only` | +30 | Enter boost |
| `boost` | +60, with the room reading short | Arm the heat pump (assist) |

Simulated times to arm the heat pump with the room steadily 1 F short:

| Situation | Time |
|---|---|
| Floor at the cap | About 1 hour |
| Floor well under the level | About 1 hour |
| The same, straight after a long spell over setpoint (for example the fireplace), with the integrator at its -30 minimum | About 1.5 hours |
| Just back in boost from assist (integrator starts at +30) | About 30 minutes |
| Boost just started from `radiant_only` | About 1.25 hours (45 minutes at full weight, then half) |
| Long-running boost, level learning upward | About 2 hours |

- **Why it leaks and does not reset:** the tekmar reports whole degrees, so a room that is really 0.5 F short flickers between "at setpoint" and "1 F short". Resetting at setpoint would mean it never accumulates.
- **Carry after assist:** on leaving assist it restarts at +30, half way to the +60 that arms the heat pump. If the room sags again soon, the heat pump is re-armed in about 30 minutes, because shortfall counts in full while the level is paused after the stage change. If the room holds, the leak fades that memory in about 2 hours. The carry must stay below `assist_start_degree_minutes`.
- Elapsed minutes are capped at 10 per run, so an outage cannot add a large amount.

## 7. The floor loop (boost and assist)

The tekmar modulates its heat call around the floor minimum as well as around the air setpoint, and it does so on a finer temperature reading than it reports. Observed:

| Floor minimum vs. floor reading | tekmar behavior | Observed |
|---|---|---|
| 0 (disabled), room at setpoint | Pulses of 3-7 minutes in every 14 or so | Several periods |
| 2 F above | Continuous call (100-131 minutes) | Both boost runs, Nov 26 |
| 1 F above | Pulsing, about 72% on | Nov 25 23:30 to Nov 26 02:52 (minimum 77, floor 76) |
| At the reading | Pulsing, about 55% on | Nov 26 02:52 to 07:11 (minimum 77, floor 77) |
| 1 F below | One 1-minute call in 15 minutes | Nov 27 00:10 to 00:25, room 2 F over. A single short sample. |
| 2 F or more below | No calls | Several periods |

So the floor minimum is the setpoint of an inner proportional loop on floor temperature. It saturates about 2 F above the reading and appears to do little from 1 F below. The band at and below the reading is thinly observed; section 12 lists the measurement still to be made. The controller runs an outer loop on room temperature and writes its output to the floor minimum:

    floor minimum = learned level                 (I)
                  + 1 F if the room reads short   (P)
                  - 1 F if the room reads over

The output, but not the level, is then limited to a window around the floor reading:

| Room | Window for the floor minimum |
|---|---|
| Short or at setpoint | Floor reading - 2 F to floor reading + 1 F, and under the cap |
| 1 F over | Floor reading - 2 F to floor reading - 1 F |
| 2 F or more over | Floor reading - 2 F: stop holding the floor. The level is kept. |

The window keeps the tekmar's floor control modulating: further above and it calls continuously, further below and it contributes nothing. While the room reads 1 F over, the minimum is never higher than 1 F under the floor reading, whatever the level says. The steps are deliberately one degree at a time in both directions: a 2 F swing either way would turn the tekmar's proportional control into an on/off switch.

How this plays out (simulated):

- **Cold snap.** The room reads short with the floor at the level. The level creeps up, the minimum leads the floor by a degree, and the floor follows. If the room stays short, the heat pump is armed within one to two hours.
- **Fireplace or cooking.** The room goes several degrees over. The level holds, the minimum drops to the bottom of the window, and the floor cools. When the room comes back to setpoint the level is still where it was, so the minimum goes straight to the top of the window. If the room then reads short, the radiant is behind and the heat pump is armed in about an hour and a half.
- **Mild day.** The room reads 1 F over. The level comes down slowly and the minimum sits under the floor reading. After three hours with no short reading, boost releases.
- **Stale level.** If the load falls while learning is blocked, the level stays too high. At setpoint the minimum then pushes, the room goes 1 F over, the minimum backs off, and the level comes down a little each time. This is slow (hours) and costs about 1 F of overshoot at a time. It is the accepted price of keeping the level while the fireplace is on; a long warm spell usually ends boost anyway, which resets the level on the next entry.

The thermostat takes whole degrees, so the output is rounded. Each degree of floor moves the room about 0.5 F.

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

The controller keeps a slow running average of (heat pump reading - room reading), clamped to +/-3 F. The weight of a new reading is scaled by the time since the last run, so the average adapts at the same pace however often the automation runs. It is used for the recovery and freeze-rest setpoints, which are expressed in tekmar terms:

| Situation | Heat pump setpoint |
|---|---|
| `recovery` | Setpoint - 1 F + offset |
| Resting (freeze-risk weather) | Setpoint - 2 F + offset |

- The MHK2 does not depend on airflow through the unit, so the offset is learned in every mode, including off.
- If the wireless link drops, the unit falls back to its internal sensor, which can be far off (for example warm coils from other zones calling while this unit is off). Any reading more than 3 F (`offset_max_deviation`) from where it is expected, which is the tekmar reading plus the learned offset, is treated as that fallback: it is ignored for learning and not used to place the assist setpoint. If the true offset ever moved by more than 3 F from the learned one, readings would stay untrusted and the offset could not relearn; that would indicate a sensor problem, and the offset helper can be set by hand.
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
| Radiant thermostat set to off | Floor minimum set to 0 and stage reset. The heat pump is left alone, unless the stage was `assist` or `recovery`: then the controller had it running, and turns it off. |
| Heat pump in cool, dry or fan | The heat pump is left alone. |
| Controller disabled for a zone | Nothing is written; devices keep their current settings, including the floor minimum. |
| Home Assistant restarts | Stage, integrator, offset and timestamps are restored from helpers. The automation also runs at start. |
| Home Assistant down, in `radiant_only` or `recovery` | Both devices sit at ordinary settings. |
| Home Assistant down, in `boost` | The floor minimum stays where it was. This can overheat the room if the weather warms. It is the accepted risk of using the floor minimum, and is the same exposure the earlier regression control had. |
| Home Assistant down, in `assist` | As for boost, and the heat pump is left in heat mode within 1 F of its own reading. |
| Home Assistant down in freeze-risk weather | The heat pump is already resting at setpoint - 2 F and acts as a backstop. |

## 10. Helpers

Per zone, where `<zone>` is `living_room` or `den`. Fourteen helpers in total, defined in YAML in the package (section 14). They are not editable in the UI.

| Entity | Type | Configuration |
|---|---|---|
| `input_boolean.hsc_<zone>_enabled` | Toggle | |
| `input_select.hsc_<zone>_stage` | Dropdown | Options: `radiant_only`, `recovery`, `boost`, `assist` |
| `input_datetime.hsc_<zone>_stage_since` | Date and time | When the current stage began |
| `input_datetime.hsc_<zone>_last_short` | Date and time | Last run on which the room read 1 F or more below setpoint, in any stage |
| `input_number.hsc_<zone>_integral` | Number | Staging integrator. Min -1000, max 1000, step 0.1 |
| `input_number.hsc_<zone>_floor_level` | Number | Learned floor level in F. Min 0, max 100, step 0.01 |
| `input_number.hsc_<zone>_hp_offset` | Number | Min -3, max 3, step 0.01 |

None of these has an `initial:` value set, so Home Assistant restores the last value on restart.

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
| `assist_start_degree_minutes` | 60 | Weighted shortfall, in boost, that arms the heat pump |
| `assist_carry_degree_minutes` | 30 | Staging integrator value on leaving assist. Keep below `assist_start_degree_minutes`. |
| `integral_decay_at_setpoint` | 0.25 | Degree-minutes lost per minute at setpoint |
| `staging_integrator_minimum` | -30 | Most excess the staging integrator will remember |
| `assist_hold_minutes` | 20 | Assist ends this long after the room last read short |
| `assist_call_delta` | 1 | Heat pump setpoint offset from its own reading in assist |
| `level_settling_minutes` | 45 | The floor level does not learn for this long after boost starts |
| `integral_minutes_per_degree` | 90 | The floor level moves 1 F per this many degree-minutes of error |
| `floor_behind_margin` | 1 | The radiant is behind when the floor is this far under the level |
| `learning_shortfall_weight` | 0.5 | Weight of shortfall toward assist while the level is learning upward |
| `floor_stop_holding_error` | 2 | Room this far over setpoint: stop holding the floor |
| `floor_proportional_step` | 1 | Floor minimum nudge, in F, while the room reads short or over |
| `floor_max_lead` | 1 | The floor minimum is never more than this above the measured floor |
| `floor_max_lag` | 2 | ...or more than this below it |
| `boost_release_minutes` | 180 | Boost may end after this long without a short reading |
| `offset_learning_rate` | 0.05 | Weight of a new offset observation, per 5 minutes |
| `offset_max_deviation` | 3 | Heat pump readings further than this from the room reading plus the learned offset are not trusted |

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

Revision 3 was deployed on the morning of 2026-11-27 and superseded the same day, before it produced enough data to assess.

### Revision 4 (2026-11-27)

Reviewing revision 3 showed that its learned level changed very little. Because the level was clamped to the same window around the floor reading as the output, it could only shift the floor minimum by about a degree, and after a spell over setpoint it delayed the push by 90 to 180 minutes. It also could not arm the heat pump while the floor was still rising, however slowly.

Changes:

1. The learned level is no longer tied to the floor reading. Only the output is limited to the window (section 7).
2. The level learns only when the room is exactly 1 F off, and upward only once the floor has reached it (section 6). It holds through large excursions such as the fireplace running.
3. With the room 2 F or more over, the floor minimum goes to the bottom of the window. With the room 1 F over, it is never more than 1 F under the floor reading.
4. Shortfall counts toward arming the heat pump in full when the radiant is behind, and at half weight otherwise, replacing the "unabsorbed shortfall" routing.
5. The staging integrator's negative side is capped at -30.
6. Boost ends when the room has not read short for 3 hours and the floor minimum is under the floor reading.

Checked for revision 4: the YAML parses, the new expressions were evaluated against live entities in the Home Assistant template engine, and the floor loop was simulated for the cold, fireplace, mild and stale-level cases. Two faults found by the simulation were fixed before release: a downward jump that erased the level after the fireplace had been on, and an uncapped negative integrator debt that blocked the heat pump.

On the same day the scripts, the automation and the helpers were moved into one package, with the helpers defined in YAML (section 14).

### Revision 4.1 (2026-11-27): external review

An external review of the package found three bugs and two design problems. Four were accepted and fixed; one rested on an error in this document.

1. **Error credited to the wrong reading (bug).** Each run multiplied the current error by the minutes since the last run, but when a change of reading triggered the run, those minutes were spent at the old reading. A one-minute dip was credited with however long the room had sat at setpoint before it. Fixed: the increment uses the value from before the change (section 6), and the automation is queued so no change is dropped.
2. **Learning pause could be skipped (bug).** The 45-minute pause after assist was tied to the heat pump being off, so it never applied in freeze-risk weather, where the heat pump stays in heat mode. Fixed: the level does not learn for the first 45 minutes of any boost period.
3. **Heat pump trust window off-centre (bug).** Readings were compared with the room reading, not with the room reading plus the learned offset. Fixed.
4. **Floor minimum 1 F under the reading (not changed).** The review argued that the minimum still does work 1 F under the floor reading, so the cap while the room is over and the boost release test were both too lenient. It relied on a table in section 7 that was wrong: the steady pulsing was seen 1 F above and at the reading, not at and 1 F below it. The one sample at 1 F below shows almost no calls. The table is corrected and the rules are kept, pending the measurement below. The alternative, dropping the minimum 2 F under the floor on any over-reading, would defeat the tekmar's proportional control.
5. **Half weight applied while the level could not learn.** Fixed: shortfall counts at half weight only while the level is learning upward (section 6).

Smaller fixes: the offset average is scaled by elapsed time; the heat pump is turned off if the radiant thermostat is turned off during assist or recovery.

One consequence to watch: with the room steadily 1 F short, the heat pump is now armed about 1.25 hours after boost starts from `radiant_only`, where revision 4 took 1.7 to 2 hours. Raise `assist_start_degree_minutes` if that gives boost too little time.

Checked for revision 4.1: the YAML parses, and the trigger expressions were evaluated in the Home Assistant template engine for a reading change, a setpoint change and a time tick.

Not yet established:

- Revision 4.1 has not run end to end.
- How much work the floor minimum does at and below the floor reading. To measure: with a zone disabled in the controller, set the floor minimum by hand to 1 F under the floor reading for about an hour, then 2 F under, with the room at setpoint and again with it 1 F over, and record the heat call duty from `hvac_action`. If the tekmar still calls substantially at 1 F under, the cap while the room is over and the boost release test both need to move down a degree.
- How often the floor minimum is rewritten when the room reading flickers, and whether the tekmar integration tolerates it.
- How a level learned at one setpoint behaves after the setpoint is changed.
- Whether a 1 F call in assist is gentle enough, and how the room behaves when the radiant is maxed out and the heat pump is carrying load. Assist has not run since revision 2.
- Behavior in weather cold enough for the floor to approach its cap.

Open observation from the backtest: on the Nov 22 return the room was 2-3 F low but recorded call duty was 80%, where the tekmar manual implies 100%. This may be the 0.1-hour resolution of the heat-hours counter.

## 13. Rollout

Done:

1. The fourteen helpers in section 10 were created in the UI. They have since been replaced by YAML helpers with the same entity IDs, with their values restored.
2. The scripts and the automation were added and loaded (revision 1, 2026-11-26). They now live in the package (section 14).
3. The regression floor-minimum automation and the outdoor-temperature heat pump automation were removed.
4. A "Heat stage controller" section was added to the Heating Plant dashboard view, including "Last read short" and "Floor level (learned)" tiles per zone.
5. Both zones were enabled at 11:30 on 2026-11-26.

To deploy revision 4.1:

1. Pull the repo on the Home Assistant host.
2. Reload automations. (Only the automation changed. A change to the scripts needs a script reload, and a change to the helpers needs the input helper reloads or a restart.)
3. No helper values need to be reset. The stage, integrator and floor level carry over.

Things to watch and tune first:

- `floor_max_lead`: the tekmar was seen pulsing at about 72% with the minimum 1 F above the floor reading, so 1 keeps it modulating. At 2 it calls continuously.
- `integral_minutes_per_degree`: lower it if the level is slow to find the right floor temperature; raise it if the room cycles slowly around setpoint.
- `learning_shortfall_weight`: raise it toward 1 if the room sits 1 F low too long while the floor is at the level.
- `assist_start_degree_minutes`: lower it if the room sits 1 F low for too long before the heat pump is armed; raise it to give the floor more time.
- `boost_release_minutes`: shorten it if boost lingers in mild weather.
- `assist_hold_minutes`: shorten it if the idle-but-armed period is a nuisance; lengthen it if the heat pump is being switched off and back on.
- Heat pump heat presets for these two zones are overwritten within 5 minutes while a zone is enabled. Radiant presets work as before.

## 14. Code

The code lives in one package, [`packages/heat_stage_controller.yaml`](../packages/heat_stage_controller.yaml): the helpers (section 10), the three `hsc_*` scripts and the controller automation, with its tunables at the top. It is loaded by `homeassistant: packages: !include_dir_named packages` in `configuration.yaml`.
