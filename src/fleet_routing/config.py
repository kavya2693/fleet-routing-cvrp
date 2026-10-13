"""Run profiles and operating constants.

Two profiles share every code path. `full` is sized to the production problem
(about 300 vehicles, four depots, a few thousand stops a day); `small` is one depot
and a hundred-odd stops so the whole pipeline finishes inside a CI job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Profile:
    name: str
    seed: int
    region_km: tuple[float, float]
    depots_km: tuple[tuple[float, float], ...]
    fleet_size: int
    n_zones: int
    history_start: date
    history_end: date
    weekday_orders: int
    n_history_shipments: int
    # Consecutive order days (Mon-Fri) replayed through the dispatcher, one quiet and one peak week.
    eval_windows: tuple[tuple[date, date], ...]
    backtest_weeks: int
    solver_seconds: float


FULL = Profile(
    name="full",
    seed=7,
    region_km=(240.0, 180.0),
    depots_km=((55.0, 50.0), (185.0, 45.0), (60.0, 135.0), (180.0, 130.0)),
    fleet_size=300,
    n_zones=40,
    history_start=date(2024, 1, 1),
    history_end=date(2025, 12, 31),
    weekday_orders=2000,
    n_history_shipments=200_000,
    eval_windows=((date(2025, 10, 13), date(2025, 10, 17)), (date(2025, 12, 8), date(2025, 12, 12))),
    backtest_weeks=12,
    solver_seconds=6.0,
)

SMALL = Profile(
    name="small",
    seed=11,
    region_km=(110.0, 90.0),
    depots_km=((50.0, 45.0),),
    fleet_size=12,
    n_zones=8,
    history_start=date(2024, 6, 1),
    history_end=date(2025, 12, 31),
    weekday_orders=90,
    n_history_shipments=20_000,
    eval_windows=((date(2025, 12, 9), date(2025, 12, 11)),),
    backtest_weeks=6,
    solver_seconds=1.5,
)

PROFILES = {p.name: p for p in (FULL, SMALL)}

# Fleet. A tractor-trailer carries 26 pallets, a rigid box truck 14; the mix is 70/30.
TRAILER_PALLETS = 26
RIGID_PALLETS = 14
RIGID_SHARE = 0.30

# Shift: leave the depot from 06:00, back by 18:00. Times are minutes after midnight.
SHIFT_START = 6 * 60
SHIFT_END = 18 * 60
SPEED_KMH = 50.0
# Straight-line distance understates road distance; 1.3 is a common circuity factor.
CIRCUITY = 1.3
SERVICE_BASE_MIN = 12
SERVICE_PER_PALLET_MIN = 4

# Rostering: drivers are booked a week ahead from the forecast, with headroom for error
# and for the fact that routes never fill trucks to 100%.
ROSTER_HEADROOM = 1.10
ROSTER_FILL = 0.85
MEAN_PALLETS_PER_STOP = 2.1

# Misroute handling. A misrouted shipment is re-delivered the next day. Its cost in km
# (second trip, handling, service credit) is an assumption, not a measured figure; the
# run sweeps it to show what the router trades for each setting.
MISROUTE_DELAY_DAYS = 1
MISROUTE_COST_KM = 150.0
MISROUTE_COST_SWEEP_KM = (500.0, 2000.0)
# The dispatch office can phone this share of each day's consignees before loading.
ADDRESS_CHECK_SHARE = 0.05
# Orders still undelivered this many days after a window closes are counted, not chased.
CLEAR_OUT_DAYS = 3
