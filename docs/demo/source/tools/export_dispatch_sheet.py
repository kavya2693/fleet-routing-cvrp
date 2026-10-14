"""Re-run the repo's own pipeline for ONE depot-day and export tomorrow's dispatch sheet.

Same profile ("full"), seed, data, forecaster (XGBoost roster), risk model and OR-Tools
settings (risk-priced, 150 km, 6 s) as `make run`, restricted to the first peak dispatch
day (2025-12-09) at the busiest depot (3) -- the day/depot shown in outputs/full/route_map.png.
The window starts with an empty backlog, so day 1 does not depend on earlier days.
OR-Tools stops on wall-clock time, so km can differ slightly from the published map.

Run:  cd <repo> && PYTHONPATH=src .venv/bin/python <this file> <out.json>
"""

import json
import sys
from pathlib import Path

import pandas as pd

from fleet_routing import config, forecast, risk, simulate
from fleet_routing.config import PROFILES
from fleet_routing.data import load
from fleet_routing.routing import Instance, ortools_plan, replay

p = PROFILES["full"]
ds = load(Path("data/full"))
fc = forecast.backtest(ds.demand_observed, p.history_end, p.backtest_weeks, models=("xgboost",))
model, _ = risk.fit(ds.history, seed=p.seed)

orders = ds.orders.copy()
orders["p_pred"] = model.predict(orders)
orders["risk_slope"] = risk.position_slope(orders["p_pred"].to_numpy())

peak = next(w for w in p.eval_windows if w[0].month == 12)
order_day = pd.Timestamp(peak[0])
dispatch_day = order_day + pd.Timedelta(days=1)
dep = ds.depots.sort_values("vehicles").iloc[-1]
depot = int(dep["depot"])
fleet = int(dep["vehicles"])

dfc = simulate.depot_forecast(fc, ds.zones, "xgboost")
k = simulate.roster_size(float(dfc.at[order_day, depot]), fleet)
stops = (
    orders[(orders["date"] == order_day) & (orders["depot"] == depot)]
    .assign(age_days=0)
    .reset_index(drop=True)
)
inst = Instance((float(dep["x"]), float(dep["y"])), stops, simulate.roster_caps(k, fleet))
plan = ortools_plan(inst, p.solver_seconds, risk_weight=config.MISROUTE_COST_KM * model.beta_position)
st, rt = replay(inst, plan)


def hhmm(m: float) -> str:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


trucks = []
for v, route in enumerate(plan.routes):
    if not route:
        continue
    r = rt[rt["vehicle"] == v].iloc[0]
    s = st[st["vehicle"] == v].sort_values("position")
    seq = [
        {
            "order_id": stops.at[int(i), "order_id"],
            "x": float(stops.at[int(i), "x"]),
            "y": float(stops.at[int(i), "y"]),
            "pallets": int(stops.at[int(i), "pallets"]),
            "eta": hhmm(row.service_start),
            "window": f"{hhmm(stops.at[int(i), 'tw_start'])}-{hhmm(stops.at[int(i), 'tw_end'])}",
            "high_risk": bool(stops.at[int(i), "p_pred"] >= orders["p_pred"].quantile(0.9)),
        }
        for i, row in zip(s["stop"], s.itertuples(), strict=True)
    ]
    trucks.append(
        {
            "vehicle": int(v),
            "depart": hhmm(config.SHIFT_START),
            "stops": int(r["stops"]),
            "km": round(float(r["km"]), 1),
            "return": hhmm(r["return"]),
            "load": int(r["load"]),
            "capacity": int(r["capacity"]),
            "first_eta": seq[0]["eta"],
            "last_eta": seq[-1]["eta"],
            "sequence": seq,
        }
    )

out = {
    "depot": depot,
    "depot_xy": [float(dep["x"]), float(dep["y"])],
    "order_day": str(order_day.date()),
    "dispatch_day": str(dispatch_day.date()),
    "rostered_trucks": k,
    "trucks_used": len(trucks),
    "orders": len(stops),
    "delivered": len(st),
    "total_km": round(float(rt["km"].sum()), 1),
    "on_time": int((~st["late"]).sum()),
    "solve_seconds": round(plan.solve_seconds, 1),
    "trucks": trucks,
}
Path(sys.argv[1]).write_text(json.dumps(out, indent=1))
print({k_: v_ for k_, v_ in out.items() if k_ != "trucks"})
