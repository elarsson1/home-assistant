"""Scenario definitions used in docs/heat_stage_controller_scenarios.md.

Each scenario is a schedule function of the minute since its start (negative
during warm-up) returning outdoor temperature, tekmar setpoint and internal
gains, plus the clock time it starts at and how long it runs.

Internal gains are in the room model's units (multiples of the room's loss
per degree F): a person about 2, cooking 6, the fireplace 20, sun up to 10.
"""
import math
from dataclasses import dataclass, field
from typing import Callable

from simulator import ramp

HOUR = 60
PERSON, COOKING, FIREPLACE, PEAK_SUN = 2, 6, 20, 10


def between(minute, start_hour, end_hour):
    return start_hour * HOUR <= minute < end_hour * HOUR


@dataclass
class Scenario:
    key: str
    number: int
    title: str               # chart title: the finding
    description: str         # chart subtitle: the conditions
    start_hour: float        # clock time at minute 0
    minutes: int
    schedule: Callable
    events: list = field(default_factory=list)  # (minute, label) markers

    def clock(self, minute):
        total = int(self.start_hour * HOUR + minute)
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def cold_front(minute):
    """16:00 start. Outdoors 47 F until 22:00, 27 F by 07:00, 35 F by 16:00.
    Two people home 18:00-23:00, cooking 18:30-19:30, one person 07:00-08:30."""
    outdoor = ramp(minute, [(0, 47), (6 * HOUR, 46), (15 * HOUR, 27), (18 * HOUR, 27), (24 * HOUR, 35)])
    gains = 0
    if between(minute, 2, 7):
        gains += 2 * PERSON
    if between(minute, 2.5, 3.5):
        gains += COOKING
    if between(minute, 15, 16.5):
        gains += PERSON
    return dict(outdoor=outdoor, setpoint=68, gains=gains)


def fireplace_evening(minute):
    """14:00 start. Outdoors 44 F easing to 39 F. Two people 18:00-23:00,
    fireplace 19:00-20:45."""
    outdoor = ramp(minute, [(0, 44), (4 * HOUR, 42), (10 * HOUR, 40), (14 * HOUR, 39)])
    gains = 0
    if between(minute, 4, 9):
        gains += 2 * PERSON
    if between(minute, 5, 6.75):
        gains += FIREPLACE
    return dict(outdoor=outdoor, setpoint=68, gains=gains)


def setback_and_raise(minute):
    """21:00 start. Setpoint 62 F from 22:30 to 06:30, 70 F from 18:00.
    Outdoors 38 F, 33 F at dawn, 41 F in the afternoon."""
    outdoor = ramp(minute, [(0, 38), (9 * HOUR, 33), (14 * HOUR, 40), (19 * HOUR, 41), (24 * HOUR, 37)])
    setpoint = 68
    if between(minute, 1.5, 9.5):
        setpoint = 62
    if between(minute, 21, 25):
        setpoint = 70
    gains = 0
    if between(minute, 9.5, 11):
        gains += PERSON
    if between(minute, 20, 24):
        gains += 2 * PERSON
    return dict(outdoor=outdoor, setpoint=setpoint, gains=gains)


def mild_day_and_party(minute):
    """06:00 start. Outdoors 38 F to 56 F and back to 44 F. Sun 11:00-16:00,
    dinner party with eight people 18:00-22:00, cooking 18:00-19:30."""
    outdoor = ramp(minute, [(0, 38), (8 * HOUR, 55), (10 * HOUR, 56), (14 * HOUR, 48), (18 * HOUR, 44)])
    gains = 0
    if between(minute, 5, 10):
        gains += PEAK_SUN * math.sin(math.pi * (minute - 5 * HOUR) / (5 * HOUR))
    if between(minute, 12, 16):
        gains += 8 * PERSON
    if between(minute, 12, 13.5):
        gains += COOKING
    return dict(outdoor=outdoor, setpoint=68, gains=gains)


def deep_cold(minute):
    """Noon start. Outdoors 38 F falling to 14 F by 05:00, 22 F by noon.
    Two people 18:00-23:00."""
    outdoor = ramp(minute, [(0, 38), (4 * HOUR, 36), (17 * HOUR, 14), (20 * HOUR, 14), (24 * HOUR, 22)])
    gains = 2 * PERSON if between(minute, 6, 11) else 0
    return dict(outdoor=outdoor, setpoint=68, gains=gains)


def away_return_mild(minute):
    """10:00 start. Away setback at 60 F (two days, so the floor is cold),
    setpoint back to 68 F at 15:00. Outdoors 50-54 F. Two people home from
    15:00, cooking 18:30-19:30."""
    outdoor = ramp(minute, [(0, 50), (5 * HOUR, 54), (10 * HOUR, 51), (24 * HOUR, 48)])
    setpoint = 60 if minute < 5 * HOUR else 68
    gains = 0
    if between(minute, 5, 13):
        gains += 2 * PERSON
    if between(minute, 8.5, 9.5):
        gains += COOKING
    return dict(outdoor=outdoor, setpoint=setpoint, gains=gains)


def cold_snap_setback(minute):
    """18:00 start, after two days near 17 F (24 h average under 20 F, so an
    idle heat pump rests). Setpoint 62 F from 22:00 to 06:00, then 68 F.
    Outdoors 17 F falling to 10 F by 05:00, 19 F by 18:00."""
    outdoor = ramp(minute, [(0, 17), (11 * HOUR, 10), (14 * HOUR, 11), (24 * HOUR, 19)])
    setpoint = 62 if between(minute, 4, 12) else 68
    gains = 2 * PERSON if between(minute, 0, 4) or between(minute, 12, 14) else 0
    return dict(outdoor=outdoor, setpoint=setpoint, gains=gains)


def raise_after_assist(minute):
    """12:00 start. Outdoors 40 F. Setpoint raised 68 -> 70 F at 14:48, eight
    minutes after an assist run ended (heat pump off less than 15 minutes)."""
    outdoor = 40
    setpoint = 70 if minute >= RAISE_MINUTE else 68
    return dict(outdoor=outdoor, setpoint=setpoint, gains=0)


RAISE_MINUTE = 2 * HOUR + 48  # 8 minutes after the 14:40 assist run ends

SCENARIOS = [
    Scenario("cold_front", 1, "One recovery, then assist carried the cold front: the recovery loop is gone",
             "Scenario 1 · 24 h from 16:00 · setpoint 68 °F · outdoors 47 °F falling to 27 °F",
             16, 24 * HOUR, cold_front, [(150, "cooking"), (360, "front arrives"), (900, "27 °F")]),
    Scenario("fireplace", 2, "Boost still released mid-fireplace; afterwards recovery handed straight to assist",
             "Scenario 2 · 14 h from 14:00 · setpoint 68 °F · fireplace 19:00 to 20:45",
             14, 14 * HOUR, fireplace_evening, [(300, "fireplace"), (405, "fireplace off")]),
    Scenario("setback", 3, "After the setback, recovery handed to assist, which pushed the floor while the heat pump helped",
             "Scenario 3 · 24 h from 21:00 · setpoint 62 °F overnight, 68 °F day, 70 °F from 18:00",
             21, 24 * HOUR, setback_and_raise, [(90, "setback 62"), (570, "back to 68"), (1260, "raised to 70")]),
    Scenario("mild_party", 4, "Boost released at 10:30; sun and party were left to the floor's heat",
             "Scenario 4 · 18 h from 06:00 · setpoint 68 °F · outdoors 38 °F to 56 °F",
             6, 18 * HOUR, mild_day_and_party, [(300, "sun"), (720, "party"), (960, "party ends")]),
    Scenario("deep_cold", 5, "With the floor capped, assist stayed armed from 20:50 on",
             "Scenario 5 · 24 h from noon · setpoint 68 °F · outdoors 38 °F falling to 14 °F",
             12, 24 * HOUR, deep_cold, [(530, "assist holds"), (1020, "14 °F")]),
    Scenario("away_return", 6, "Back from away: recovery, then assist and boost took turns while the floor caught up",
             "Scenario 6 · 14 h from 10:00 · away setback 60 °F until 15:00, then 68 °F · outdoors 48 to 54 °F",
             10, 14 * HOUR, away_return_mild, [(300, "back to 68")]),
    Scenario("cold_snap_setback", 7, "At 10 °F the floor could not hold even the setback; the heat pump carried the night and the morning return",
             "Scenario 7 · 24 h from 18:00 · setpoint 62 °F 22:00 to 06:00 · outdoors 17 °F to 10 °F",
             18, 24 * HOUR, cold_snap_setback, [(240, "setback 62"), (720, "back to 68")]),
    Scenario("raise_after_assist", 8, "The raise waited out the heat pump's 15-minute off time, then recovery handed to assist",
             "Scenario 8 · 8 h from 12:00 · outdoors 40 °F · setpoint 68 then 70 °F",
             12, 8 * HOUR, raise_after_assist, [(RAISE_MINUTE, "raised to 70")]),
]


def steady(outdoor, setpoint=68):
    """Constant outdoor temperature, no gains."""
    return lambda minute: dict(outdoor=outdoor, setpoint=setpoint, gains=0)
