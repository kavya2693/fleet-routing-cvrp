from __future__ import annotations

import numpy as np
import pandas as pd

from fleet_routing import forecast
from fleet_routing.config import SMALL
from fleet_routing.data import Dataset, build_network, public_holidays, zone_demand


def test_generator_is_reproducible_from_the_seed() -> None:
    a = zone_demand(SMALL, build_network(SMALL, np.random.default_rng(1)), np.random.default_rng(2))[0]
    b = zone_demand(SMALL, build_network(SMALL, np.random.default_rng(1)), np.random.default_rng(2))[0]
    pd.testing.assert_frame_equal(a, b)


def test_demand_carries_the_patterns_the_planner_depends_on(small: Dataset) -> None:
    total = small.demand_true.sum(axis=1)
    dow = total.groupby(total.index.dayofweek).mean()
    assert dow[6] < 0.2 * dow[0:5].mean()  # Sundays are near-empty
    hols = [pd.Timestamp(d) for d in public_holidays(range(2024, 2026)) if pd.Timestamp(d) in total.index]
    weekday_hols = [d for d in hols if d.dayofweek < 5]
    assert total[weekday_hols].mean() < 0.3 * dow[0:5].mean()
    december = total[
        (total.index.month == 12) & (total.index.day.isin(range(5, 19))) & (total.index.dayofweek < 5)
    ]
    october = total[(total.index.month == 10) & (total.index.dayofweek < 5)]
    assert december.mean() > 1.15 * october.mean()
    # The feed has gaps; the truth does not.
    assert small.demand_observed.isna().to_numpy().sum() > 0
    assert not small.demand_true.isna().to_numpy().any()


def test_every_dispatch_order_is_inside_its_depot_fleet(small: Dataset) -> None:
    o = small.orders
    assert set(o["depot"]) <= set(small.depots["depot"])
    assert (o["tw_end"] > o["tw_start"]).all()
    assert o["order_id"].is_unique
    assert small.depots["vehicles"].sum() == SMALL.fleet_size


def test_forecasts_do_not_look_past_the_origin(small: Dataset) -> None:
    """Corrupt everything from the origin on; a forecast made at that origin must not move."""
    y = small.demand_observed
    origin = forecast.origins(SMALL.history_end, 2)[0]
    tampered = y.copy()
    tampered.loc[origin:] = tampered.loc[origin:] * 10 + 500
    for fn in (forecast.seasonal_naive, forecast.ets):
        pd.testing.assert_frame_equal(fn(y, origin), fn(tampered, origin))
    xa = forecast.xgboost_forecaster(y)(y, origin)
    xb = forecast.xgboost_forecaster(tampered)(tampered, origin)
    np.testing.assert_allclose(xa.to_numpy(), xb.to_numpy(), rtol=1e-5)


def test_learned_forecast_beats_last_week(small: Dataset) -> None:
    fc = forecast.backtest(small.demand_observed, SMALL.history_end, 4, models=("seasonal_naive", "xgboost"))
    s = forecast.score(fc, small.demand_true).set_index(["model", "period"])["wape"]
    assert s["xgboost", "all"] < s["seasonal_naive", "all"]
