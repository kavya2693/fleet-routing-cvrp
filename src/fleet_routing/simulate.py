"""Replay order days through a dispatcher, carrying unserved orders forward.

Each order day's orders are dispatched the next day together with anything left over.
Drivers per depot were booked from the forecast a week earlier. After the last order day
the depots keep dispatching with the full fleet for up to `CLEAR_OUT_DAYS` days so every
order gets a lead time. Misroutes are scored with the generator's ground-truth hazard
at the position the stop actually ended up in, so the router cannot mark its own homework.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from fleet_routing import config
from fleet_routing.data import is_peak, misroute_probability
from fleet_routing.routing import Instance, ortools_plan, replay, sweep_plan


@dataclass(frozen=True)
class Arm:
    name: str
    planner: str  # "sweep" or "ortools"
    misroute_cost_km: float = 0.0
    roster_model: str = "xgboost"


def fleet_caps(fleet: int) -> list[int]:
    n_rigid = round(fleet * config.RIGID_SHARE)
    return [config.TRAILER_PALLETS] * (fleet - n_rigid) + [config.RIGID_PALLETS] * n_rigid


def roster_size(forecast_orders: float, fleet: int) -> int:
    caps = fleet_caps(fleet)
    mean_cap = float(np.mean(caps))
    need = (
        forecast_orders
        * config.MEAN_PALLETS_PER_STOP
        * config.ROSTER_HEADROOM
        / (mean_cap * config.ROSTER_FILL)
    )
    return int(min(fleet, max(1, math.ceil(need))))


def roster_caps(k: int, fleet: int) -> list[int]:
    # Tractors go out first; rigids are called in once the tractors are booked.
    return fleet_caps(fleet)[:k]


def depot_forecast(fc: pd.DataFrame, zones: pd.DataFrame, model: str) -> pd.DataFrame:
    """Forecast orders per (date, depot) for one model."""
    f = fc[fc["model"] == model].merge(zones[["zone", "depot"]], on="zone")
    return f.groupby(["date", "depot"])["forecast"].sum().unstack(fill_value=0.0)


def run_window(
    arm: Arm,
    days: pd.DatetimeIndex,
    orders: pd.DataFrame,
    depots: pd.DataFrame,
    depot_fc: pd.DataFrame,
    risk_weight_per_km: float,
    solver_seconds: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (one row per delivered order, one row per route driven)."""
    backlog = orders.iloc[0:0].assign(age_days=0)
    delivered: list[pd.DataFrame] = []
    driven: list[pd.DataFrame] = []
    schedule = [(d, False) for d in days] + [
        (days[-1] + pd.Timedelta(days=k), True) for k in range(1, config.CLEAR_OUT_DAYS + 1)
    ]
    for order_day, clear_out in schedule:
        new = orders.iloc[0:0] if clear_out else orders[orders["date"] == order_day]
        backlog = backlog.assign(age_days=backlog["age_days"] + 1)
        pool = pd.concat([backlog, new.assign(age_days=0)], ignore_index=True)
        if pool.empty:
            break
        dispatch_day = order_day + pd.Timedelta(days=1)
        carry: list[pd.DataFrame] = []
        for _, dep in depots.iterrows():
            stops = pool[pool["depot"] == dep["depot"]].reset_index(drop=True)
            if stops.empty:
                continue
            fleet = int(dep["vehicles"])
            k = fleet if clear_out else roster_size(float(depot_fc.at[order_day, dep["depot"]]), fleet)
            inst = Instance((float(dep["x"]), float(dep["y"])), stops, roster_caps(k, fleet))
            if arm.planner == "sweep":
                plan = sweep_plan(inst)
            else:
                weight = arm.misroute_cost_km * risk_weight_per_km
                plan = ortools_plan(inst, solver_seconds, risk_weight=weight)
            st, rt = replay(inst, plan)
            served = stops.loc[st["stop"].to_numpy()].reset_index(drop=True)
            served["position"] = st["position"].to_numpy()
            served["late"] = st["late"].to_numpy()
            served["vehicle"] = st["vehicle"].to_numpy()
            served["dispatch_date"] = dispatch_day
            served["p_true"] = misroute_probability(served, served["position"].to_numpy())
            delivered.append(served)
            carry.append(stops.drop(index=st["stop"].to_numpy()))
            driven.append(
                rt.assign(
                    depot=int(dep["depot"]),
                    dispatch_date=dispatch_day,
                    rostered=k,
                    solve_seconds=plan.solve_seconds,
                )
            )
        backlog = pd.concat(carry, ignore_index=True) if carry else backlog.iloc[0:0]
    out = pd.concat(delivered, ignore_index=True) if delivered else pd.DataFrame()
    if not out.empty:
        out["days"] = (out["dispatch_date"] - out["date"]).dt.days
        out["lead_days"] = out["days"] + out["p_true"] * config.MISROUTE_DELAY_DAYS
    routes = pd.concat(driven, ignore_index=True) if driven else pd.DataFrame()
    stranded = len(backlog)
    out.attrs["stranded"] = stranded
    return out, routes


def summarize(arm: str, window: str, n_orders: int, served: pd.DataFrame, routes: pd.DataFrame) -> dict:
    # Expected values: each delivered order contributes its misroute probability, which is far
    # less noisy than drawing one coin per order and lets small differences show.
    p = served["p_true"].to_numpy()
    days = served["days"].to_numpy()
    return {
        "arm": arm,
        "window": window,
        "orders": n_orders,
        "delivered": len(served),
        "stranded": n_orders - len(served),
        "total_km": float(routes["km"].sum()),
        "vehicle_days": len(routes),
        "stops_per_route": float(routes["stops"].mean()),
        "on_time_pct": 100 * float(1 - served["late"].mean()),
        "misroute_pct": 100 * float(p.mean()),
        "lead_days_mean": float(served["lead_days"].mean()),
        # Next-day means dispatched on the first day and not misrouted.
        "next_day_pct": 100 * float(((days == 1) * (1 - p)).sum() / n_orders),
        "within_2_days_pct": 100 * float(((days == 1) + (days == 2) * (1 - p)).sum() / n_orders),
        "solve_seconds": float(routes.groupby(["dispatch_date", "depot"])["solve_seconds"].first().sum()),
    }


def roster_scores(
    fc: pd.DataFrame, actual: pd.DataFrame, zones: pd.DataFrame, depots: pd.DataFrame
) -> pd.DataFrame:
    """How each forecast would have booked drivers over the backtest, against what was needed.

    Shortfall (too few trucks booked) and idle trucks pull in opposite directions: a forecast
    biased high looks good on the first and bad on the second.
    """
    act = actual.stack().rename("actual").reset_index()
    act.columns = ["date", "zone", "actual"]
    act["zone"] = act["zone"].astype(int)
    act = act.merge(zones[["zone", "depot"]], on="zone").groupby(["date", "depot"])["actual"].sum()
    fleet = depots.set_index("depot")["vehicles"]
    rows = []
    for model in fc["model"].unique():
        dfc = depot_forecast(fc, zones, model).stack().rename("forecast")
        df = pd.concat([dfc, act], axis=1, join="inner").reset_index()
        df = df[pd.DatetimeIndex(df["date"]).dayofweek < 5]
        booked = np.array(
            [roster_size(f, int(fleet[d])) for f, d in zip(df["forecast"], df["depot"], strict=True)]
        )
        mean_cap = df["depot"].map(lambda d: float(np.mean(fleet_caps(int(fleet[d]))))).to_numpy()
        need = np.ceil(
            df["actual"].to_numpy() * config.MEAN_PALLETS_PER_STOP / (mean_cap * config.ROSTER_FILL)
        )
        need = np.minimum(need, df["depot"].map(fleet).to_numpy())
        peak = is_peak(pd.DatetimeIndex(df["date"]))
        for period, mask in (("all", np.ones(len(df), bool)), ("peak", peak), ("non_peak", ~peak)):
            rows.append(
                {
                    "model": model,
                    "period": period,
                    "short_depot_days_pct": 100 * float((booked[mask] < need[mask]).mean()),
                    "trucks_short": float(np.clip(need[mask] - booked[mask], 0, None).sum()),
                    "trucks_idle": float(np.clip(booked[mask] - need[mask], 0, None).sum()),
                    "depot_days": int(mask.sum()),
                }
            )
    return pd.DataFrame(rows)
