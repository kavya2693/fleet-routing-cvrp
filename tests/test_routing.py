"""The replay is the referee for every routing number, so it is tested on its own first,
including against plans that are deliberately wrong."""

from __future__ import annotations

import pandas as pd
import pytest

from fleet_routing import config
from fleet_routing.routing import Instance, Plan, ortools_plan, replay, sweep_plan


def stops(rows: list[tuple[float, float, int, int, int]], **extra: list[float]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["x", "y", "pallets", "tw_start", "tw_end"])
    df["service_min"] = 10
    df["age_days"] = extra.get("age_days", [0] * len(df))
    df["risk_slope"] = extra.get("risk_slope", [0.0] * len(df))
    return df


ALL_DAY = (8 * 60, 17 * 60)


def test_replay_measures_distance_position_and_lateness() -> None:
    # Depot at origin, stop A 10 km east, stop B 20 km east. B's window closes at 06:30, so
    # any route that reaches it after A is late.
    s = stops([(10, 0, 2, *ALL_DAY), (20, 0, 3, 6 * 60, 6 * 60 + 30)])
    inst = Instance((0.0, 0.0), s, [26])
    st, rt = replay(inst, Plan(routes=[[0, 1]]))
    assert rt["km"].iloc[0] == pytest.approx(40 * config.CIRCUITY)
    assert st["position"].tolist() == [1, 2]
    assert st["late"].tolist() == [False, True]
    assert rt["load"].iloc[0] == 5


def test_replay_exposes_an_overloaded_route() -> None:
    s = stops([(5, 0, 10, *ALL_DAY), (6, 0, 10, *ALL_DAY)])
    inst = Instance((0.0, 0.0), s, [14])
    _, rt = replay(inst, Plan(routes=[[0, 1]]))
    assert (rt["load"] > rt["capacity"]).all()


def _random_instance(n: int, n_veh: int, seed: int) -> Instance:
    import numpy as np

    rng = np.random.default_rng(seed)
    start = rng.integers(8, 15, n) * 60
    narrow = rng.random(n) < 0.3
    s = stops(
        [
            (float(x), float(y), int(p), int(a) if w else ALL_DAY[0], int(a) + 120 if w else ALL_DAY[1])
            for x, y, p, a, w in zip(
                rng.uniform(0, 60, n),
                rng.uniform(0, 60, n),
                rng.integers(1, 5, n),
                start,
                narrow,
                strict=True,
            )
        ]
    )
    return Instance((30.0, 30.0), s, [26] * (n_veh - 1) + [14])


@pytest.mark.parametrize("planner", ["sweep", "ortools"])
def test_planners_respect_capacity_and_shift(planner: str) -> None:
    inst = _random_instance(60, 6, seed=3)
    plan = sweep_plan(inst) if planner == "sweep" else ortools_plan(inst, time_limit_s=2)
    st, rt = replay(inst, plan)
    assert (rt["load"] <= rt["capacity"]).all()
    assert (rt["return"] <= config.SHIFT_END).all()
    assert st["stop"].is_unique
    if planner == "ortools":
        # Hard time windows: whatever OR-Tools serves, it serves on time.
        assert not st["late"].any()


def test_ortools_drops_the_newest_order_when_capacity_runs_out() -> None:
    # One 14-pallet truck, two 10-pallet orders: one must roll over. The one that has
    # already waited a day carries the larger drop penalty and should be the one served.
    s = stops([(5, 0, 10, *ALL_DAY), (5, 1, 10, *ALL_DAY)], age_days=[0, 1])
    inst = Instance((0.0, 0.0), s, [14])
    st, _ = replay(inst, ortools_plan(inst, time_limit_s=1))
    assert st["stop"].tolist() == [1]


def test_risk_pricing_moves_a_risky_stop_to_the_front() -> None:
    # Three stops on the corners of a square. Going round the square puts the far corner B
    # second whichever way the truck turns; sending it to B first costs an 8 km detour.
    # Priced high enough, risky B goes first; with the term off, distance wins.
    s = stops([(10, 0, 1, *ALL_DAY), (10, 10, 1, *ALL_DAY), (0, 10, 1, *ALL_DAY)], risk_slope=[0.0, 0.2, 0.0])
    inst = Instance((0.0, 0.0), s, [26])
    plain, _ = replay(inst, ortools_plan(inst, time_limit_s=1))
    priced, _ = replay(inst, ortools_plan(inst, time_limit_s=1, risk_weight=10_000))
    assert plain.set_index("stop").at[1, "position"] == 2
    assert priced.set_index("stop").at[1, "position"] == 1
