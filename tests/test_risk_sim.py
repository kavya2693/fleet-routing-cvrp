from __future__ import annotations

import numpy as np
import pandas as pd

from fleet_routing import config, risk, simulate
from fleet_routing.config import SMALL
from fleet_routing.data import Dataset, misroute_probability


def test_position_effect_is_recovered_from_history(small: Dataset) -> None:
    # The generator uses 0.09 log-odds per stop; the router prices what history says, so
    # history has to say roughly that.
    model, _ = risk.fit(small.history)
    assert 0.05 < model.beta_position < 0.14


def test_classifier_ranks_better_than_chance_and_the_rule(small: Dataset) -> None:
    _, table = risk.fit(small.history)
    t = table.set_index(["model", "period"])
    assert t.at[("xgboost", "all"), "roc_auc"] > 0.6
    assert t.at[("xgboost", "all"), "roc_auc"] > t.at[("rule", "all"), "roc_auc"]
    assert t.at[("ceiling", "all"), "roc_auc"] >= t.at[("xgboost", "all"), "roc_auc"] - 0.02


def test_address_check_never_raises_risk(small: Dataset) -> None:
    o = small.orders
    pos = np.full(len(o), 5)
    assert (misroute_probability(risk.address_check(o), pos) <= misroute_probability(o, pos) + 1e-12).all()


def test_roster_is_bounded_by_the_fleet() -> None:
    assert simulate.roster_size(0, 50) == 1
    assert simulate.roster_size(1e6, 50) == 50
    assert simulate.roster_caps(5, 10) == [config.TRAILER_PALLETS] * 5


def test_replayed_window_accounts_for_every_order(small: Dataset) -> None:
    model, _ = risk.fit(small.history)
    orders = small.orders.copy()
    orders["p_pred"] = model.predict(orders)
    orders["risk_slope"] = risk.position_slope(orders["p_pred"].to_numpy())
    days = pd.date_range(*SMALL.eval_windows[0], freq="D")
    win = orders[orders["date"].isin(days)]
    # Book one truck a day so the window is forced to roll orders over.
    dfc = pd.DataFrame(0.0, index=days, columns=small.depots["depot"])
    served, routes = simulate.run_window(
        simulate.Arm("sweep", "sweep"), days, win, small.depots, dfc, model.beta_position, 1.0
    )
    assert served["order_id"].is_unique
    assert len(served) + served.attrs["stranded"] == len(win)
    assert (served["days"] >= 1).all()
    assert served["days"].max() > 1  # the short roster really did push orders to later days
    s = simulate.summarize("sweep", "test", len(win), served, routes)
    assert 0 <= s["next_day_pct"] <= s["within_2_days_pct"] <= 100
