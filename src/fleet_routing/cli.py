"""One command, end to end: data, forecasts, risk model, routing replay, report.

fleet-routing --profile small      # CI-sized, about a minute
fleet-routing --profile full       # 300 trucks, four depots; 20-30 minutes on a laptop
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

from fleet_routing import config, forecast, plot, risk, simulate
from fleet_routing.config import PROFILES, Profile
from fleet_routing.data import Dataset, generate, load


def _log(msg: str, t0: float) -> None:
    print(f"[{time.perf_counter() - t0:7.1f}s] {msg}", flush=True)


def _arms(p: Profile) -> tuple[list[simulate.Arm], list[simulate.Arm]]:
    main = [
        simulate.Arm("sweep", "sweep"),
        simulate.Arm("ortools", "ortools"),
        simulate.Arm("ortools_risk", "ortools", misroute_cost_km=config.MISROUTE_COST_KM),
    ]
    # Peak window only: what the router gives up as misroutes are priced higher, and how much
    # of the lead-time result comes from the forecast that booked the drivers.
    extra = [
        simulate.Arm(f"ortools_risk_{int(c)}km", "ortools", misroute_cost_km=c)
        for c in config.MISROUTE_COST_SWEEP_KM
    ] + [
        simulate.Arm(
            "ortools_risk_naive_roster",
            "ortools",
            misroute_cost_km=config.MISROUTE_COST_KM,
            roster_model="seasonal_naive",
        )
    ]
    return main, extra


def run(p: Profile, data_dir: Path, out: Path, refresh: bool) -> dict:
    t0 = time.perf_counter()
    if refresh or not (data_dir / "manifest.json").exists():
        _log(f"generating {p.name} data -> {data_dir}", t0)
        generate(p, data_dir)
    ds: Dataset = load(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    _log(
        f"{len(ds.zones)} zones, {int(ds.depots['vehicles'].sum())} trucks, {len(ds.orders)} dispatch orders",
        t0,
    )

    fc = forecast.backtest(ds.demand_observed, p.history_end, p.backtest_weeks)
    fscores = forecast.score(fc, ds.demand_true)
    rscores = simulate.roster_scores(fc, ds.demand_true, ds.zones, ds.depots)
    fscores.to_csv(out / "forecast_scores.csv", index=False)
    rscores.to_csv(out / "roster_scores.csv", index=False)
    _log("forecast backtest done\n" + fscores[fscores["period"] == "all"].round(3).to_string(index=False), t0)

    model, risk_table = risk.fit(ds.history, seed=p.seed)
    risk_table.to_csv(out / "risk_scores.csv", index=False)
    _log(
        f"risk model done, position effect {model.beta_position:.3f} log-odds per stop\n"
        + risk_table[risk_table["period"] == "all"].round(3).to_string(index=False),
        t0,
    )

    orders = ds.orders.copy()
    orders["p_pred"] = model.predict(orders)
    orders["risk_slope"] = risk.position_slope(orders["p_pred"].to_numpy())
    risk_cut = float(np.quantile(orders["p_pred"], 0.9))

    main, extra = _arms(p)
    summaries, delivered = [], {}
    for w_i, (w_start, w_end) in enumerate(p.eval_windows):
        days = pd.date_range(w_start, w_end, freq="D")
        window = "peak" if days[0].month == 12 else "quiet"
        win_orders = orders[orders["date"].isin(days)]
        arms = main + (extra if w_i == len(p.eval_windows) - 1 else [])
        for arm in arms:
            dfc = simulate.depot_forecast(fc, ds.zones, arm.roster_model)
            served, routes = simulate.run_window(
                arm, days, win_orders, ds.depots, dfc, model.beta_position, p.solver_seconds
            )
            s = simulate.summarize(arm.name, window, len(win_orders), served, routes)
            summaries.append(s)
            delivered[(arm.name, window)] = served
            _log(
                f"{window:5s} {arm.name:26s} km={s['total_km']:9.0f} trucks={s['vehicle_days']:4d} "
                f"on_time={s['on_time_pct']:5.1f}% misroute={s['misroute_pct']:.2f}% "
                f"lead={s['lead_days_mean']:.2f}d next_day={s['next_day_pct']:.1f}%",
                t0,
            )
    summary = pd.DataFrame(summaries)
    summary.to_csv(out / "routing_summary.csv", index=False)

    checks = []
    for (arm_name, window), served in delivered.items():
        if arm_name == "ortools_risk":
            eff = risk.address_check_effect(served, model.scorers(), config.ADDRESS_CHECK_SHARE, p.seed)
            checks.append(eff.assign(window=window))
    check_table = pd.concat(checks, ignore_index=True)
    check_table.to_csv(out / "address_checks.csv", index=False)
    _log("address checks on top 5% by each ranking\n" + check_table.round(3).to_string(index=False), t0)

    window = "peak" if any(w[0].month == 12 for w in p.eval_windows) else "quiet"
    first_dispatch = delivered[("sweep", window)]["dispatch_date"].min()
    busiest = int(ds.depots.sort_values("vehicles").iloc[-1]["depot"])
    dep = ds.depots.set_index("depot").loc[busiest]
    panels = []
    for arm_name, label in (
        ("sweep", "Sweep + nearest neighbour"),
        ("ortools_risk", "OR-Tools CVRPTW, risk-priced"),
    ):
        d = delivered[(arm_name, window)]
        panels.append((label, d[(d["dispatch_date"] == first_dispatch) & (d["depot"] == busiest)]))
    plot.route_map(
        panels,
        (float(dep["x"]), float(dep["y"])),
        risk_cut,
        out / "route_map.png",
        f"Depot {busiest}, dispatch {first_dispatch.date()} ({p.name} profile)",
    )

    results = {
        "profile": p.name,
        "trucks": int(ds.depots["vehicles"].sum()),
        "depots": len(ds.depots),
        "zones": len(ds.zones),
        "dispatch_orders": len(ds.orders),
        "solver_seconds_per_depot_day": p.solver_seconds,
        "position_effect_logodds_per_stop": model.beta_position,
        "misroute_cost_km": config.MISROUTE_COST_KM,
        "runtime_seconds": round(time.perf_counter() - t0, 1),
    }
    (out / "results.json").write_text(json.dumps(results, indent=2))
    _write_report(out, results, fscores, rscores, risk_table, summary, check_table)
    _log(f"wrote {out}", t0)
    return results


def _write_report(
    out: Path,
    results: dict,
    fscores: pd.DataFrame,
    rscores: pd.DataFrame,
    risk_table: pd.DataFrame,
    summary: pd.DataFrame,
    check_table: pd.DataFrame,
) -> None:
    def table(df: pd.DataFrame) -> str:
        cols = list(df.columns)
        lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for _, r in df.iterrows():
            cells = [f"{v:,.3f}" if isinstance(v, float) else str(v) for v in r]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)

    parts = [
        f"# Run report ({results['profile']} profile)",
        "```\n" + json.dumps(results, indent=2) + "\n```",
        "## Routing replay",
        table(summary.drop(columns=["solve_seconds"])),
        "## Zone demand forecast (rolling weekly origin)",
        table(fscores.sort_values(["period", "wape"])),
        "## Driver booking from each forecast",
        table(rscores.sort_values(["period", "model"])),
        "## Misroute classifier (time split, most recent 20% of dates held out)",
        table(risk_table),
        f"## Pre-dispatch address checks on {config.ADDRESS_CHECK_SHARE:.0%} of orders, ortools_risk routes",
        table(check_table),
    ]
    (out / "report.md").write_text("\n\n".join(parts) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(prog="fleet-routing")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="small")
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--refresh", action="store_true", help="regenerate the dataset first")
    args = ap.parse_args()
    p = PROFILES[args.profile]
    run(p, args.data or Path("data") / p.name, args.out or Path("outputs") / p.name, args.refresh)


if __name__ == "__main__":
    main()
