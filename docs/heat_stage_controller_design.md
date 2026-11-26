# Heat stage controller: design

Status: 2026-11-26. Active.

## 1. Purpose

Coordinate the radiant floor and the heat pump in the Living Room and Den so that:

- The radiant floor carries as much of the load as it can.
- The heat pump runs only when the radiant cannot hold or reach setpoint in a reasonable time.
- The room does not overshoot setpoint.
- A loss of Home Assistant or of either thermostat leaves both devices at ordinary settings.

It replaces two existing pieces:

- `automation.radiant_floor_minimum_from_outdoor_temperature`, which sets the floor minimum from a fitted heat-loss regression.
- The automation that turns the heat pump on and off from outdoor temperature alone.

## 2. Why the old approach falls short

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
4. **Radiant first.** A persistent shortfall first pushes the radiant harder (boost). The heat pump comes in only if that is not enough (assist), or for a large gap (recovery).
5. **Sane settings at all times.** The heat pump is only ever set to setpoint, 1 F below, or 2 F below. No extreme setpoints are used as switches.
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

## 5. Stages

| Stage | Heat pump | Floor minimum | Meaning |
|---|---|---|---|
| `radiant_only` | Off, or resting at setpoint - 2 F in freeze-risk weather | 0 (disabled) | The tekmar runs on its own air control. |
| `recovery` | Heat, at setpoint - 1 F | 0 (disabled) | Large gap. The heat pump closes most of it; the radiant, already calling, finishes. |
| `boost` | Off, or resting as above | Managed (section 7) | Persistent small shortfall. Push the radiant past the tekmar's droop. |
| `assist` | Heat, at setpoint | Managed (section 7) | Boost was not enough. The heat pump holds setpoint while the radiant keeps running. |

### Transitions

| From | To | Condition |
|---|---|---|
| `radiant_only` or `boost` | `recovery` | Room 2 F or more below setpoint, and the heat pump may start |
| `radiant_only` | `boost` | Shortfall persists (integrator at or above +30) |
| `recovery` | `radiant_only` | Room within 1 F of setpoint, and at least 15 minutes in recovery |
| `boost` | `assist` | Shortfall persists, and either the floor is within 1 F of its cap or boost has run 90 minutes, and the heat pump may start |
| `boost` | `radiant_only` | Excess persists (integrator at or below -20) |
| `assist` | `boost` | Excess persists, and at least 15 minutes in assist |

Notes:

- "The heat pump may start" means it is already in heat mode (resting), or has been off for at least 15 minutes.
- The 90-minute limit covers the case where supply water temperature, not the floor cap, limits the floor. On Nov 25 the floor took about 13 hours to rise from 80 F to 84 F, so this limit will often be what triggers assist.
- Leaving assist goes to boost, not to radiant only, so the floor minimum is trimmed down gradually and not dropped to zero.
- In assist, the heat pump throttles itself to zero before the room can rise above setpoint. A room above setpoint therefore means the radiant alone is enough. This is how "heat pump no longer needed" is detected without any run-status signal from the Mitsubishi.

## 6. The integrator

One signed value per zone, in degree-minutes. Positive is accumulated shortfall; negative is accumulated excess.

| Room reading | Effect each run |
|---|---|
| 1 F or more below setpoint | Add (error x minutes) |
| At setpoint | Leak toward zero at 0.25 per minute |
| 1 F or more above setpoint | Subtract (error x minutes) |

- **Why it leaks and does not reset:** the tekmar reports whole degrees, so a room that is really 0.5 F short flickers between "at setpoint" and "1 F short". Resetting at setpoint would mean it never accumulates.
- **Resulting times to escalate:** always 1 F short, 30 minutes. Short half the time, about 80 minutes. Short less than 20% of the time, never.
- A change of sign clears the opposite side first. Any stage change resets the integrator to zero.
- Elapsed minutes are capped at 10 per run, so an outage cannot add a large amount.

## 7. Floor minimum in boost and assist

Evaluated each run, first match wins:

| Condition | Action |
|---|---|
| Room 1 F or more above setpoint | Lower by 2 F. If the result is at or below the room setpoint, set 0. |
| Boost, and room at setpoint | Hold (the push is working) |
| Otherwise | Raise to the measured floor temperature plus a lead, never above the floor cap and never lower than the current value |

The lead is 2 F when the room is short, and 1 F in assist with the room at setpoint. Raising at setpoint in assist shifts load from the heat pump to the radiant.

Because the floor minimum only ever leads the measured floor by 1-2 F, its ramp is paced by how fast the floor actually warms. On entry it starts from the current floor temperature, so there is no step change.

## 8. Heat pump setpoint and sensor offset

The heat pump and the tekmar read different temperatures. The controller keeps a slow running average of (heat pump reading - room reading), clamped to +/-3 F.

- The heat pump reads from a wireless MHK2 thermostat, which does not depend on airflow through the unit, so the offset is learned in every mode, including off.
- If the wireless link drops, the unit falls back to its internal sensor, which can be far off (for example warm coils from other zones calling while this unit is off). Any reading more than 3 F from the tekmar reading (`offset_max_deviation`) is treated as that fallback and ignored for learning.
- During such a dropout the heat pump is controlling on its internal sensor, so the learned offset does not describe what it is measuring. The controller does not detect or correct for this beyond ignoring the reading.

Heat pump setpoint = target in tekmar terms + learned offset, rounded to a whole degree.

| Stage | Target in tekmar terms |
|---|---|
| `recovery` | Setpoint - 1 F |
| `assist` | Setpoint |
| Resting (freeze-risk weather) | Setpoint - 2 F |

Modulation near setpoint comes from these targets being close to room temperature. In recovery after a large setpoint change the heat pump is allowed to run hard; it tapers as the room approaches its target.

### Freeze protection

When the heat pump is not needed (`radiant_only` or `boost`):

- 24-hour outdoor average below `freeze_protect_outdoor_temp`, or outdoor reading missing: rest in heat mode at setpoint - 2 F.
- Otherwise: off.

The threshold is the outdoor temperature below which the radiant alone could not hold about 60 F in an unoccupied house. For the Living Room the earlier regression puts that near 26 F; the draft uses 30 F for margin. The Den is set to -20 F, which effectively disables it. There is 2 F of hysteresis.

## 9. Failure behavior

| Event | Behavior |
|---|---|
| Thermostat or heat pump unavailable | The zone is skipped for that run. Nothing is written. |
| A required reading is missing | The zone is skipped for that run. |
| Radiant thermostat set to off | Floor minimum set to 0, stage reset, heat pump left alone. |
| Heat pump in cool, dry or fan | The heat pump is left alone. |
| Controller disabled for a zone | Nothing is written; devices keep their current settings, including the floor minimum. |
| Home Assistant restarts | Stage, integrator, offset and stage time are restored from helpers. The automation also runs at start. |
| Home Assistant down, in `radiant_only` or `recovery` | Both devices sit at ordinary settings. |
| Home Assistant down, in `boost` or `assist` | The floor minimum stays where it was. This can overheat the room if the weather warms. It is the accepted risk of using the floor minimum. |
| Home Assistant down in freeze-risk weather | The heat pump is already resting at setpoint - 2 F and acts as a backstop. |

## 10. Helpers to create

Per zone, where `<zone>` is `living_room` or `den`. Ten helpers in total.

| Entity | Type | Configuration |
|---|---|---|
| `input_boolean.hsc_<zone>_enabled` | Toggle | Initially off |
| `input_select.hsc_<zone>_stage` | Dropdown | Options: `radiant_only`, `recovery`, `boost`, `assist` |
| `input_datetime.hsc_<zone>_stage_since` | Date and time | Has date and has time |
| `input_number.hsc_<zone>_integral` | Number | Min -1000, max 1000, step 0.1, unit "F min" |
| `input_number.hsc_<zone>_hp_offset` | Number | Min -3, max 3, step 0.01, unit "F", initial 0 |

## 11. Tunables

Set per zone at the top of the automation.

| Name | Default | Meaning |
|---|---|---|
| `freeze_protect_outdoor_temp` | 30 (Living Room), -20 (Den) | Below this 24-hour outdoor average, an idle heat pump rests in heat mode |
| `rest_droop` | 2 | Resting heat pump target, in F below setpoint |
| `recovery_start_error` | 2 | Enter recovery at this many F below setpoint |
| `recovery_stop_error` | 1 | Heat pump stops this many F short of setpoint |
| `shortfall_degree_minutes` | 30 | Integrator level that escalates a stage |
| `excess_degree_minutes` | 20 | Integrator level (negative) that de-escalates a stage |
| `integral_decay_at_setpoint` | 0.25 | Degree-minutes lost per minute at setpoint |
| `boost_max_minutes` | 90 | Time boost gets before assist is allowed |
| `min_minutes_heat_pump_on` | 15 | Minimum time in recovery or assist |
| `min_minutes_heat_pump_off` | 15 | Minimum heat pump off time before it may start |
| `offset_learning_rate` | 0.05 | Weight of each new offset observation |
| `offset_max_deviation` | 3 | Heat pump readings further than this from the room reading are ignored when learning the offset |
| `floor_lead_below_setpoint` | 2 | Floor minimum lead over measured floor when the room is short |
| `floor_lead_at_setpoint` | 1 | Lead in assist with the room at setpoint |
| `floor_step_down` | 2 | Floor minimum reduction per run when the room is above setpoint |

## 12. What has and has not been tested

Tested:

- Both YAML files parse.
- Individual expressions were evaluated against live entities in the Home Assistant template engine.
- An earlier single-template version of the stage rules passed 14 synthetic scenarios.
- Entry rules were replayed over one week of Living Room history (Nov 18-25). With the leaky integrator, no saturation test and no trend test, all three overnight shortfalls would have triggered within about 1 to 1.5 hours, and the Nov 22 return from away would have entered recovery immediately.

Not tested:

- The automation and scripts have not run end to end. That requires installing them.
- The boost stage and the signed integrator are new since the scenario test.
- Without a trend test, boost can start while the room is still climbing on its own (for example about 30 minutes after recovery hands off), and assist can start during a slow climb once boost has run 90 minutes. `boost_max_minutes` is the tunable for this.
- The backtest was a replay on hourly data. It shows when rules would fire, not how the room would respond.
- No data yet from weather cold enough for the floor to approach its cap with the heat pump off.

Open observation: on the Nov 22 return the room was 2-3 F low but recorded call duty was 80%, where the tekmar manual implies 100%. This may be the 0.1-hour resolution of the heat-hours counter.

## 13. Rollout

1. Create the helpers in section 10. Leave both `enabled` toggles off.
2. Add the scripts to `scripts.yaml` and the automation to `automations.yaml`. Reload both.
3. Disable `automation.radiant_floor_minimum_from_outdoor_temperature` and the outdoor-temperature heat pump on/off automation.
4. Set both floor minimums to 0 and both stages to `radiant_only`.
5. Enable the Living Room first. Watch the stage, integrator and floor minimum for a few days before enabling the Den.
6. Heat pump heat presets for these two zones will be overwritten within 5 minutes while a zone is enabled. Radiant presets work as they do today.

Things to watch and tune first:

- `boost_max_minutes`: too short brings the heat pump in before the floor has responded; too long leaves the room short.
- `floor_lead_below_setpoint`: lower it if boost or assist overshoots.

## 14. Code

### 14.1 Scripts (`scripts.yaml`)

```yaml
# =============================================================================
# Heat stage controller - SCRIPTS (go in scripts.yaml)
# DRAFT FOR REVIEW - not installed
#
# Small helpers used by the "Heat stage controller" automation. Each one only
# writes to a device or helper when the value actually needs to change.
# =============================================================================

# Record a stage change: set the stage, stamp the time, clear the integrator.
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
        value: 0

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

### 14.2 Automation (`automations.yaml`)

```yaml
# =============================================================================
# Heat stage controller - AUTOMATION (goes in automations.yaml)
# DRAFT FOR REVIEW - not installed
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
#                 is 0. Heat pump is off (or resting in freeze-risk weather).
#   recovery      Room is well below setpoint (setback recovery). Heat pump
#                 runs to 1 F short of setpoint; the radiant finishes the rest.
#   boost         Room is persistently a little short. Heat pump stays off;
#                 the floor minimum creeps up to push the radiant harder than
#                 the tekmar's proportional control would on its own.
#   assist        Boost was not enough. Heat pump holds setpoint while the
#                 floor minimum keeps the radiant running.
#
# Uses three small scripts from heat_stage_controller_scripts.yaml:
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
        recovery_stop_error: 1            # heat pump stops this far short of setpoint
        shortfall_degree_minutes: 30      # accumulated shortfall that escalates a stage
        excess_degree_minutes: 20         # accumulated excess that de-escalates a stage
        integral_decay_at_setpoint: 0.25  # degree-minutes lost per minute at setpoint
        boost_max_minutes: 90             # time boost gets before the heat pump may assist
        min_minutes_heat_pump_on: 15
        min_minutes_heat_pump_off: 15
        offset_learning_rate: 0.05
        offset_max_deviation: 3           # ignore heat pump readings further than this from the room reading
        floor_lead_below_setpoint: 2      # floor minimum leads measured floor temp by this
        floor_lead_at_setpoint: 1         # ...and by this in assist when the room is at setpoint
        floor_step_down: 2                # per run, when room is above setpoint

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
        shortfall_degree_minutes: 30
        excess_degree_minutes: 20
        integral_decay_at_setpoint: 0.25
        boost_max_minutes: 90
        min_minutes_heat_pump_on: 15
        min_minutes_heat_pump_off: 15
        offset_learning_rate: 0.05
        offset_max_deviation: 3           # ignore heat pump readings further than this from the room reading
        floor_lead_below_setpoint: 2
        floor_lead_at_setpoint: 1
        floor_step_down: 2

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
              integral_entity: "input_number.hsc_{{ repeat.item.id }}_integral"
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

                  # The heat pump may be started if it is already in heat mode
                  # (resting), or has been off long enough.
                  heat_pump_minutes_in_mode: >-
                    {{ (now() - states[zone.heat_pump].last_changed).total_seconds() / 60 }}
                  heat_pump_can_start: >-
                    {{ heat_pump_mode == 'heat'
                       or (now() - states[zone.heat_pump].last_changed).total_seconds() / 60
                          >= zone.min_minutes_heat_pump_off }}

              # -------------------------------------------------------------
              # 4. Learn the heat pump's sensor offset
              #    (heat pump reading minus room reading, slow running average)
              #
              #    The heat pump reads from a wireless MHK2 thermostat, so the
              #    reading is valid in any mode. If the wireless link drops,
              #    the unit falls back to its internal sensor, which can be
              #    far off. Readings that differ from the room reading by more
              #    than offset_max_deviation are treated as that fallback and
              #    ignored.
              # -------------------------------------------------------------
              - if:
                  - "{{ is_number(heat_pump_reading) }}"
                  - "{{ (heat_pump_reading - room_temp) | abs <= zone.offset_max_deviation }}"
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
              # 5. Update the integrator (degree-minutes)
              #    Positive = accumulated shortfall (room below setpoint)
              #    Negative = accumulated excess    (room above setpoint)
              #    It leaks slowly toward zero while the room reads at
              #    setpoint, so a whole-degree reading that flickers between
              #    "at setpoint" and "1 F short" still accumulates.
              # -------------------------------------------------------------
              - choose:

                  # Below setpoint: accumulate shortfall (positive).
                  - conditions:
                      - "{{ error >= 1 }}"
                    sequence:
                      - action: input_number.set_value
                        target:
                          entity_id: "{{ integral_entity }}"
                        data:
                          value: >-
                            {{ [([integral, 0] | max) + error * minutes_since_last_run, 1000] | min | round(1) }}

                  # Above setpoint: accumulate excess (negative).
                  - conditions:
                      - "{{ error <= -1 }}"
                    sequence:
                      - action: input_number.set_value
                        target:
                          entity_id: "{{ integral_entity }}"
                        data:
                          value: >-
                            {{ [([integral, 0] | min) + error * minutes_since_last_run, -1000] | max | round(1) }}

                # At setpoint: leak toward zero from either side.
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
                  shortfall_persists: "{{ states(integral_entity) | float(0) >= zone.shortfall_degree_minutes }}"
                  excess_persists: "{{ states(integral_entity) | float(0) <= -zone.excess_degree_minutes }}"

              # -------------------------------------------------------------
              # 6. Stage transitions (first matching rule wins).
              #    script.hsc_set_stage also resets the integrator to 0.
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
                  # Room has been persistently short on the tekmar's own control.
                  - conditions:
                      - "{{ stage == 'radiant_only' }}"
                      - "{{ shortfall_persists }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: boost

                  # RECOVERY -> RADIANT ONLY
                  # Heat pump has brought the room to 1 F short of setpoint.
                  - conditions:
                      - "{{ stage == 'recovery' }}"
                      - "{{ error <= zone.recovery_stop_error }}"
                      - "{{ minutes_in_stage >= zone.min_minutes_heat_pump_on }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: radiant_only

                  # BOOST -> ASSIST
                  # Still short, and the radiant has nothing more to give:
                  # either the floor is at its cap, or boost has had its time
                  # (the floor is limited by supply water temperature).
                  - conditions:
                      - "{{ stage == 'boost' }}"
                      - "{{ shortfall_persists }}"
                      - "{{ floor_temp >= floor_cap - 1 or minutes_in_stage >= zone.boost_max_minutes }}"
                      - "{{ heat_pump_can_start }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: assist

                  # BOOST -> RADIANT ONLY
                  # Room has been above setpoint: the push is no longer needed.
                  - conditions:
                      - "{{ stage == 'boost' }}"
                      - "{{ excess_persists }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: radiant_only

                  # ASSIST -> BOOST
                  # Room has been above setpoint. The heat pump throttles
                  # itself to zero before the room can rise above setpoint,
                  # so this means the radiant alone is now enough. Drop the
                  # heat pump but keep managing the floor minimum.
                  - conditions:
                      - "{{ stage == 'assist' }}"
                      - "{{ excess_persists }}"
                      - "{{ minutes_in_stage >= zone.min_minutes_heat_pump_on }}"
                    sequence:
                      - action: script.hsc_set_stage
                        data:
                          zone_id: "{{ zone.id }}"
                          stage: boost

              # -------------------------------------------------------------
              # 7. Apply outputs for the stage we are in now.
              #    Stage and offset are re-read because the steps above may
              #    have just changed them.
              # -------------------------------------------------------------
              - variables:
                  stage: "{{ states(stage_entity) }}"
                  heat_pump_offset: "{{ states(offset_entity) | float(0) }}"

              # ---- 7a. Floor minimum ----
              - choose:

                  # Not boosting or assisting: floor minimum is disabled and
                  # the tekmar runs on its own air control.
                  - conditions:
                      - "{{ stage not in ['boost', 'assist'] }}"
                    sequence:
                      - action: script.hsc_set_floor_minimum
                        data:
                          floor: "{{ zone.floor }}"
                          floor_minimum: 0

                  # Room above setpoint: back the floor off. A floor minimum
                  # at or below the room setpoint does nothing, so go straight
                  # to 0 from there.
                  - conditions:
                      - "{{ error <= -1 }}"
                    sequence:
                      - variables:
                          lowered_minimum: "{{ floor_minimum_now - zone.floor_step_down }}"
                      - action: script.hsc_set_floor_minimum
                        data:
                          floor: "{{ zone.floor }}"
                          floor_minimum: "{{ lowered_minimum | int if lowered_minimum > setpoint else 0 }}"

                  # Boost with the room at setpoint: the push is working, hold.
                  - conditions:
                      - "{{ stage == 'boost' }}"
                      - "{{ error < 1 }}"
                    sequence: []

                # Otherwise raise the floor minimum to just ahead of the
                # measured floor temperature, never above the cap and never
                # lower than it already is. (In assist this also runs with the
                # room at setpoint, to shift load from the heat pump to the
                # radiant.)
                default:
                  - variables:
                      floor_lead: >-
                        {{ zone.floor_lead_below_setpoint if error >= 1
                           else zone.floor_lead_at_setpoint }}
                  - variables:
                      raised_minimum: "{{ [floor_minimum_now, floor_temp + floor_lead] | max }}"
                  - action: script.hsc_set_floor_minimum
                    data:
                      floor: "{{ zone.floor }}"
                      floor_minimum: "{{ [raised_minimum, floor_cap] | min | int }}"

              # ---- 7b. Heat pump ----
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

                  # ASSIST: hold setpoint
                  - conditions:
                      - "{{ stage == 'assist' }}"
                    sequence:
                      - action: script.hsc_set_heat_pump
                        data:
                          heat_pump: "{{ zone.heat_pump }}"
                          mode: heat
                          setpoint: "{{ (setpoint + heat_pump_offset) | round(0) | int }}"

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
