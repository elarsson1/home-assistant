"""Run every simulation behind docs/heat_stage_controller_scenarios.md.

Writes:
  docs/images/heat_stage_controller/*.png   the charts in the scenarios doc
  tools/heat_stage_sim/output/*.csv         one row per minute for each run
and prints the tables quoted in the doc (stage changes, steady-state sweep,
what-if variants).

Usage, from the repo root:
  python3 tools/heat_stage_sim/make_report.py
  python3 tools/heat_stage_sim/make_report.py --no-figures   # tables and CSVs only

Needs Python 3.9+ and matplotlib.
"""
import argparse
import csv
import os
from collections import Counter

from simulator import simulate
from scenarios import SCENARIOS, HOUR, steady

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
FIGURE_DIR = os.path.join(REPO, "docs", "images", "heat_stage_controller")
CSV_DIR = os.path.join(HERE, "output")

STAGE_COLORS = {"radiant_only": "#c9c8c2", "boost": "#2a78d6", "assist": "#e8743b", "recovery": "#1fa67a"}
STAGE_LABELS = {"radiant_only": "radiant only", "boost": "boost", "assist": "assist", "recovery": "recovery"}
INK, QUIET, GRID = "#1f1f1f", "#6b6b6b", "#e6e6e6"
MODE_LABELS = {"off": "tekmar alone", "nudge_only": "nudge only", "full": "learned level"}

# tekmar floor-loop duty as observed (design doc, section 7). The room model
# uses 5% at -1 F; the single observation there was one 1-minute call in 15.
OBSERVED_FLOOR_LOOP_DUTY = [(-2, 0, "room 2 °F\nor more over"), (-1, 7, "room 1 °F over\n(upper cap)"),
                            (0, 55, "at setpoint,\nlevel = floor"), (1, 72, "room short, or\nlevel above floor"),
                            (2, 100, "outside the\nwindow")]


# ----------------------------------------------------------------- metrics
def summarize(rows, stage_changes):
    n = len(rows)
    return dict(
        reads_setpoint=sum(r["room_reading"] >= r["setpoint"] for r in rows) / n,
        two_short=sum(r["setpoint"] - r["room_reading"] >= 2 for r in rows) / n,
        heat_pump_starts=sum(a["heat_pump_mode"] == "off" and b["heat_pump_mode"] == "heat"
                             for a, b in zip(rows, rows[1:])),
        heat_pump_full_load_hours=sum(r["heat_pump_output_pct"] for r in rows) / 100 / 60,
        stage_changes=len(stage_changes),
    )


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


# ----------------------------------------------------------------- figures
def plot_floor_loop_duty(plt, path):
    fig, ax = plt.subplots(figsize=(9, 4.6))
    xs = [p[0] for p in OBSERVED_FLOOR_LOOP_DUTY]
    ys = [p[1] for p in OBSERVED_FLOOR_LOOP_DUTY]
    ax.axvspan(-2, 1, color="#f0efec", zorder=0)
    ax.text(-0.5, 104, "controller's window: floor reading −2 to +1 °F", ha="center", color=QUIET, fontsize=9)
    ax.axhline(50, color=QUIET, ls="--", lw=1)
    ax.text(2.45, 45, "tekmar's own air loop with the room at setpoint: 50%", ha="right", color=QUIET, fontsize=9)
    ax.plot(xs, ys, color=STAGE_COLORS["boost"], lw=2, marker="o", ms=7)
    for x, y, _ in OBSERVED_FLOOR_LOOP_DUTY:
        ax.annotate(f"{y}%", (x, y), textcoords="offset points", xytext=(-10, 8), ha="right", fontweight="bold")
    ax.set_xticks(xs)
    ax.set_xticklabels([f"{'+' if x > 0 else ''}{x}\n{label}" for x, _, label in OBSERVED_FLOOR_LOOP_DUTY], fontsize=9)
    ax.set_xlim(-2.5, 2.5)
    ax.set_ylim(-3, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.set_yticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_xlabel("floor minimum minus floor reading (°F), and when the controller puts it there")
    ax.set_title("The window keeps the tekmar's floor loop between 0% and about 72% on", loc="left", fontweight="bold")
    style(ax)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_steady_45(plt, path, runs):
    nudge, full = runs["nudge_only"], runs["full"]
    fig, axes = plt.subplots(2, 1, figsize=(9, 5.2), sharex=True)
    for ax, rows, name, color in ((axes[0], nudge, "Nudge only", "#9a998f"),
                                  (axes[1], full, "Learned level", STAGE_COLORS["boost"])):
        hours = [r["minute"] / 60 for r in rows]
        ax.step(hours, [r["floor_minimum"] for r in rows], where="post", color=color, lw=2, label="floor minimum")
        ax.step(hours, [r["floor_reading"] for r in rows], where="post", color=QUIET, lw=1, ls="--", label="floor reading")
        short = [r["room_reading"] < r["setpoint"] for r in rows]
        ax.fill_between(hours, 79.3, 79.7, where=short, step="post", color="#f2b33d", alpha=0.8, lw=0,
                        label="tekmar reads 67 (short)")
        changes = sum(a["floor_minimum"] != b["floor_minimum"] for a, b in zip(rows, rows[1:]))
        ax.set_ylim(79, 83.5)
        ax.set_yticks([80, 81, 82, 83])
        ax.set_ylabel("°F")
        ax.set_title(f"{name}: {changes} floor-minimum changes, reads short {sum(short) / len(short):.0%} of the time",
                     loc="left", fontsize=10)
        style(ax)
    axes[0].legend(loc="upper right", fontsize=8, ncol=3, frameon=False)
    axes[1].set_xlabel("hours")
    fig.suptitle("With the level the floor minimum holds at 82 °F; the nudge alone flips it", x=0.01, ha="left",
                 fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_steady_sweep(plt, path, sweep):
    outdoor_temps = sorted({row["outdoor"] for row in sweep}, reverse=True)
    modes = ["off", "nudge_only", "full"]
    colors = {"off": "#dedcd5", "nudge_only": "#b5b3a9", "full": STAGE_COLORS["boost"]}
    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6.5), gridspec_kw=dict(height_ratios=[1.1, 1]))
    width = 0.26
    for i, mode in enumerate(modes):
        xs = [k + (i - 1) * width for k in range(len(outdoor_temps))]
        values = [next(r for r in sweep if r["outdoor"] == t and r["mode"] == mode) for t in outdoor_temps]
        bars = top.bar(xs, [v["reads_setpoint"] * 100 for v in values], width * 0.95, color=colors[mode],
                       label=MODE_LABELS[mode])
        top.bar_label(bars, fmt="%.0f", fontsize=8, padding=2)
        bottom.plot(xs, [v["mean_room"] for v in values], color=colors[mode], marker="o", lw=1.5,
                    label=MODE_LABELS[mode])
    full_rows = [next(r for r in sweep if r["outdoor"] == t and r["mode"] == "full") for t in outdoor_temps]
    top.set_xticks(range(len(outdoor_temps)))
    top.set_xticklabels([f"heat pump {r['heat_pump_pct']:.0f}% avg" for r in full_rows], fontsize=8, color=QUIET)
    top.set_ylim(0, 112)
    top.set_ylabel("% of time")
    top.set_title("Share of time the tekmar reads setpoint", loc="left", fontsize=10)
    top.legend(fontsize=8, ncol=3, frameon=False, loc="upper right")
    bottom.axhline(68, color=QUIET, lw=1)
    bottom.axhline(67.5, color=QUIET, lw=1, ls="--")
    bottom.text(-0.45, 67.3, "reading turns to 68 above this line", ha="left", fontsize=8, color=QUIET)
    bottom.set_xticks(range(len(outdoor_temps)))
    bottom.set_xticklabels([f"{t} °F outdoors" for t in outdoor_temps])
    bottom.set_ylabel("°F")
    bottom.set_title("Mean true room temperature", loc="left", fontsize=10)
    for ax in (top, bottom):
        style(ax)
    fig.suptitle("The level earns its keep between 45 and 40 °F; colder, the heat pump does the work",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_scenario(plt, path, scenario, rows):
    hours = [r["minute"] / 60 for r in rows]
    fig, axes = plt.subplots(5, 1, figsize=(9.5, 11), sharex=True,
                             gridspec_kw=dict(height_ratios=[2.2, 2, 1.2, 0.8, 1]))
    room_ax, floor_ax, integrator_ax, heat_pump_ax, outdoor_ax = axes

    # stage bands behind every panel
    segments = []
    for r in rows:
        if segments and segments[-1][2] == r["stage"]:
            segments[-1][1] = r["minute"] + 1
        else:
            segments.append([r["minute"], r["minute"] + 1, r["stage"]])
    for ax in axes[:4]:
        for start, end, stage in segments:
            ax.axvspan(start / 60, end / 60, color=STAGE_COLORS[stage], alpha=0.14 if stage != "radiant_only" else 0.25,
                       lw=0)
    for minute, label in scenario.events:
        for ax in axes:
            ax.axvline(minute / 60, color=QUIET, ls=":", lw=1)
        room_ax.annotate(label, (minute / 60, 1.0), xycoords=("data", "axes fraction"), xytext=(3, -12),
                         textcoords="offset points", fontsize=8, fontweight="bold")

    room_ax.plot(hours, [r["room_true"] for r in rows], color="#b5b3a9", lw=1.2, label="true room")
    room_ax.step(hours, [r["setpoint"] for r in rows], where="post", color=QUIET, ls="--", lw=1.2, label="setpoint")
    room_ax.step(hours, [r["room_reading"] for r in rows], where="post", color=INK, lw=1.5, label="tekmar reading")
    room_ax.set_ylabel("Room °F")
    room_ax.legend(fontsize=8, ncol=3, frameon=False, loc="lower right", bbox_to_anchor=(1, 0.98))

    def masked(key, condition):
        return [r[key] if condition(r) else float("nan") for r in rows]

    managed = lambda r: r["stage"] != "radiant_only" and r["floor_minimum"] > 0
    floor_ax.axhline(85, color=QUIET, ls=":", lw=1)
    floor_ax.step(hours, [r["floor_reading"] for r in rows], where="post", color=INK, lw=1.5, label="floor reading")
    floor_ax.step(hours, masked("floor_minimum", managed), where="post", color=STAGE_COLORS["boost"], lw=2,
                  label="floor minimum")
    floor_ax.plot(hours, masked("learned_floor_level", lambda r: r["stage"] != "radiant_only" and r["learned_floor_level"] > 0),
                  color=STAGE_COLORS["assist"], lw=1.8, ls="--", label="learned level")
    floor_ax.set_ylabel("Floor °F")
    floor_ax.legend(fontsize=8, ncol=3, frameon=False, loc="lower right", bbox_to_anchor=(1, 0.98))

    integrator_ax.plot(hours, [r["staging_integrator"] for r in rows], color=INK, lw=1.2)
    for level, label in ((60, "+60 assist"), (30, "+30 boost")):
        integrator_ax.axhline(level, color=QUIET, ls="--", lw=0.8)
        integrator_ax.text(hours[-1], level + 2, label, ha="right", fontsize=7, color=QUIET)
    integrator_ax.axhline(0, color=QUIET, lw=0.8)
    integrator_ax.set_ylim(-35, 70)
    integrator_ax.set_ylabel("Integrator\n°F·min")

    heat_pump_ax.fill_between(hours, [r["heat_pump_output_pct"] for r in rows], color=STAGE_COLORS["assist"],
                              alpha=0.6, lw=0)
    heat_pump_ax.set_ylim(0, 105)
    heat_pump_ax.set_ylabel("Heat pump\n% output")

    outdoor_ax.plot(hours, [r["outdoor"] for r in rows], color=INK, lw=1.2)
    gains_ax = outdoor_ax.twinx()
    gains_ax.fill_between(hours, [r["gains"] for r in rows], step="post", color="#b5b3a9", alpha=0.5, lw=0)
    gains_ax.set_ylim(0, 80)
    gains_ax.set_yticks([])
    outdoor_ax.set_ylabel("Outdoor °F")
    tick_hours = list(range(0, int(hours[-1]) + 1, 3))
    outdoor_ax.set_xticks(tick_hours)
    outdoor_ax.set_xticklabels([scenario.clock(h * 60) for h in tick_hours])
    outdoor_ax.set_xlim(0, hours[-1])
    for ax in axes:
        style(ax)

    handles = [plt.Rectangle((0, 0), 1, 1, color=STAGE_COLORS[s], alpha=0.5) for s in STAGE_COLORS]
    fig.legend(handles, [STAGE_LABELS[s] for s in STAGE_COLORS], loc="upper right", ncol=4, fontsize=8,
               frameon=False, bbox_to_anchor=(0.99, 0.955))
    fig.suptitle(scenario.title, x=0.01, ha="left", fontweight="bold")
    fig.text(0.01, 0.948, scenario.description + " · grey bars under the outdoor line: internal gains",
             fontsize=8.5, color=QUIET)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(path, dpi=130)
    plt.close(fig)


def plot_revision_comparison(plt, path, comparison):
    scenarios = [c["scenario"] for c in comparison if c["revision"] == "5"]
    labels = [f"{s.number}. {s.key.replace('_', ' ')}" for s in scenarios]
    panels = [("two_short", "Time 2 °F or more short", lambda v: f"{v:.0%}", 100),
              ("heat_pump_starts", "Heat pump off → heat changes", lambda v: f"{v}", 1),
              ("heat_pump_full_load_hours", "Heat pump full-load hours", lambda v: f"{v:.1f}", 1)]
    fig, axes = plt.subplots(1, 3, figsize=(11, 4.6), sharey=True)
    ys = range(len(scenarios))
    for ax, (key, title, fmt, scale) in zip(axes, panels):
        for offset, revision, color in ((-0.2, "4.1", "#c9c8c2"), (0.2, "5", STAGE_COLORS["boost"])):
            values = [next(c[key] for c in comparison if c["scenario"] is s and c["revision"] == revision)
                      for s in scenarios]
            bars = ax.barh([y + offset for y in ys], [v * scale for v in values], 0.38, color=color,
                           label=f"revision {revision}")
            for bar, value in zip(bars, values):
                ax.text(bar.get_width(), bar.get_y() + bar.get_height() / 2, " " + fmt(value),
                        va="center", fontsize=7.5, color=QUIET)
        ax.set_title(title, loc="left", fontsize=10)
        ax.invert_yaxis()
        style(ax)
        ax.grid(axis="x", color=GRID, lw=0.8)
        ax.grid(axis="y", visible=False)
    axes[0].set_yticks(list(ys))
    axes[0].set_yticklabels(labels, fontsize=8.5)
    handles, legend_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, legend_labels, fontsize=8, frameon=False, loc="upper right", ncol=2)
    fig.suptitle("Revision 5 removes the recovery loop: far less time 2 °F short, more heat pump hours",
                 x=0.01, ha="left", fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(path, dpi=150)
    plt.close(fig)


def style(ax):
    ax.grid(axis="y", color=GRID, lw=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=QUIET, labelsize=8)


# ----------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-figures", action="store_true", help="skip the PNG charts")
    args = parser.parse_args()
    plt = None
    if not args.no_figures:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        os.makedirs(FIGURE_DIR, exist_ok=True)

    print("## Scenarios (revision 5)\n")
    for scenario in SCENARIOS:
        rows, changes = simulate(scenario.schedule, scenario.minutes)
        write_csv(os.path.join(CSV_DIR, f"{scenario.key}.csv"),
                  [dict(clock=scenario.clock(r["minute"]), **r) for r in rows])
        stats = summarize(rows, changes)
        counts = Counter(new for _, _, new in changes)
        print(f"### {scenario.number}. {scenario.key}: reads setpoint {stats['reads_setpoint']:.0%}, "
              f"2 °F short {stats['two_short']:.0%}, heat pump starts {stats['heat_pump_starts']}, "
              f"stage entries {dict(counts)}")
        for minute, old, new in changes:
            print(f"    {scenario.clock(minute)}  {old} -> {new}")
        if plt:
            plot_scenario(plt, os.path.join(FIGURE_DIR, f"scenario_{scenario.number}_{scenario.key}.png"),
                          scenario, rows)

    print("\n## Steady state, 24 h at each outdoor temperature\n")
    print("| Outdoor | Controller | Reads setpoint | Mean room °F | Heat pump avg output |")
    print("| --- | --- | --- | --- | --- |")
    sweep = []
    for outdoor in (50, 45, 40, 35, 30):
        for mode in ("off", "nudge_only", "full"):
            rows, _ = simulate(steady(outdoor), 24 * HOUR, mode=mode)
            entry = dict(outdoor=outdoor, mode=mode,
                         reads_setpoint=sum(r["room_reading"] >= 68 for r in rows) / len(rows),
                         mean_room=sum(r["room_true"] for r in rows) / len(rows),
                         heat_pump_pct=sum(r["heat_pump_output_pct"] for r in rows) / len(rows))
            sweep.append(entry)
            print(f"| {outdoor} °F | {MODE_LABELS[mode]} | {entry['reads_setpoint']:.0%} | "
                  f"{entry['mean_room']:.2f} | {entry['heat_pump_pct']:.0f}% |")

    steady_runs = {mode: simulate(steady(45), 12 * HOUR, mode=mode)[0] for mode in ("nudge_only", "full")}
    write_csv(os.path.join(CSV_DIR, "steady_45_full.csv"), steady_runs["full"])
    write_csv(os.path.join(CSV_DIR, "steady_45_nudge_only.csv"), steady_runs["nudge_only"])

    print("\n## Revision 4.1 against revision 5\n")
    print("| Scenario | Revision | Reads setpoint | 2 °F short | Heat pump off → heat | Heat pump full-load hours | Stage changes |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    comparison = []
    for scenario in SCENARIOS:
        for revision in ("4.1", "5"):
            stats = summarize(*simulate(scenario.schedule, scenario.minutes, revision=revision))
            comparison.append(dict(scenario=scenario, revision=revision, **stats))
            print(f"| {scenario.number}, {scenario.key} | {revision} | {stats['reads_setpoint']:.0%} | "
                  f"{stats['two_short']:.0%} | {stats['heat_pump_starts']} | "
                  f"{stats['heat_pump_full_load_hours']:.1f} | {stats['stage_changes']} |")

    if plt:
        plot_floor_loop_duty(plt, os.path.join(FIGURE_DIR, "floor_loop_duty.png"))
        plot_steady_45(plt, os.path.join(FIGURE_DIR, "steady_45_floor_minimum.png"), steady_runs)
        plot_steady_sweep(plt, os.path.join(FIGURE_DIR, "steady_state_by_outdoor.png"), sweep)
        plot_revision_comparison(plt, os.path.join(FIGURE_DIR, "revision_comparison.png"), comparison)
        print(f"\nFigures written to {os.path.relpath(FIGURE_DIR, REPO)}")
    print(f"CSVs written to {os.path.relpath(CSV_DIR, REPO)}")


if __name__ == "__main__":
    main()
