# Heat stage controller simulator

Runs the heat stage controller from [`packages/heat_stage_controller.yaml`](../../packages/heat_stage_controller.yaml) against a simple model of the Living Room, so changes to the logic or the tunables can be tried before they are deployed. The results are written up in [`docs/heat_stage_controller_scenarios.md`](../../docs/heat_stage_controller_scenarios.md).

## Files

| File | What it holds |
| --- | --- |
| `simulator.py` | `HeatStageController` (steps 3 to 8 of the automation, revision 4.1, ported line by line), `RoomModel` (floor, room, tekmar 561, boiler supply, heat pump) and `simulate()` |
| `scenarios.py` | The five scenarios in the doc (weather, setpoint schedule, internal gains) and `steady()` for constant conditions |
| `make_report.py` | Runs everything, writes the doc's charts to `docs/images/heat_stage_controller/` and per-minute CSVs to `output/`, and prints the tables quoted in the doc |

## Running it

From the repo root, with Python 3.9+ and matplotlib:

    python3 tools/heat_stage_sim/make_report.py              # charts, CSVs and tables
    python3 tools/heat_stage_sim/make_report.py --no-figures # CSVs and tables only

A full run takes about five seconds. `output/` is ignored by git.

## Trying a change

Tunables and what-if variants are arguments to `simulate()`:

```python
import sys; sys.path.insert(0, "tools/heat_stage_sim")
from simulator import simulate
from scenarios import SCENARIOS

cold_front = SCENARIOS[0]
rows, stage_changes = simulate(
    cold_front.schedule, cold_front.minutes,
    tunables={"assist_start_degree_minutes": 45},   # any key in DEFAULT_TUNABLES
    variants=("recovery_returns_armed",),            # see KNOWN_VARIANTS
)
for minute, old, new in stage_changes:
    print(cold_front.clock(minute), old, "->", new)
```

`mode="nudge_only"` drops the stored floor level (the floor minimum is built on the floor reading), and `mode="off"` disables the controller so the tekmar runs alone.

When the package logic changes, change `HeatStageController.run()` to match; it follows the automation's step numbers so the two can be read side by side.

## What the model is and is not

The controller part is exact. The room part is a two-node model (floor and room air) with the tekmar's proportional air loop, its floor loop as observed, the Navien outdoor reset and boost, and a heat pump on its own thermostat. Its constants are tuned to the observations in the design doc, not fitted to logged data, so timings are approximate. It has no wall mass, does not model the extra swing seen at the tekmar's own sensor, and does not simulate the Den. The model's assumptions are listed in the scenarios doc under "How the simulation works".
