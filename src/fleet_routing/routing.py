"""One depot, one dispatch day: build routes and replay them.

Two planners share the same instance and the same replay, so their numbers are
comparable line for line:

- `sweep_plan`: the naive dispatcher. Sort stops by angle around the depot, fill a
  truck until it is full or its shift would overrun, order each truck's stops by
  nearest neighbour. Time windows are ignored while planning, so it can be late.
- `ortools_plan`: capacitated VRP with hard time windows, solved by OR-Tools with a
  bounded time limit. Stops it cannot fit are dropped (they roll to the next day) at a
  penalty that grows with how long the order has already waited. Optionally, each
  stop also pays for every position it sits down the route, weighted by its predicted
  misroute risk.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from fleet_routing import config

# Integer units the solver works in: distance in 100 m steps, time in minutes.
DIST_UNITS_PER_KM = 10
# A dropped stop costs this many km, scaled by (1 + days already waited); far more than
# any detour, so the solver only drops stops it cannot fit.
DROP_PENALTY_KM = 1_000
# A truck that leaves the depot costs this many km-equivalents (driver, tractor day rate).
VEHICLE_FIXED_KM = 40


@dataclass
class Instance:
    depot_xy: tuple[float, float]
    stops: pd.DataFrame  # x, y, pallets, tw_start, tw_end, service_min, age_days, risk_slope
    capacities: list[int]

    def __post_init__(self) -> None:
        xy = np.vstack([np.array(self.depot_xy)[None, :], self.stops[["x", "y"]].to_numpy()])
        d = np.linalg.norm(xy[:, None, :] - xy[None, :, :], axis=2) * config.CIRCUITY
        self.km = d
        self.travel_min = np.ceil(d / config.SPEED_KMH * 60).astype(int)

    @property
    def n(self) -> int:
        return len(self.stops)


@dataclass
class Plan:
    routes: list[list[int]]  # stop indices (0-based into instance.stops), in visiting order
    solve_seconds: float = 0.0
    meta: dict[str, float] = field(default_factory=dict)


def replay(inst: Instance, plan: Plan) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drive each route from 06:00, waiting when early. Returns (per-stop, per-route) frames.

    Every metric is computed here, from the routes alone, so a planner cannot report its own
    numbers.
    """
    s = inst.stops
    stop_rows = []
    route_rows = []
    for v, route in enumerate(plan.routes):
        if not route:
            continue
        t = config.SHIFT_START
        prev = 0
        km = 0.0
        load = 0
        for pos, i in enumerate(route, start=1):
            node = i + 1
            t += int(inst.travel_min[prev, node])
            km += float(inst.km[prev, node])
            start = max(t, int(s.at[i, "tw_start"]))
            stop_rows.append(
                {
                    "stop": i,
                    "vehicle": v,
                    "position": pos,
                    "arrive": t,
                    "service_start": start,
                    "late": start > int(s.at[i, "tw_end"]),
                }
            )
            t = start + int(s.at[i, "service_min"])
            load += int(s.at[i, "pallets"])
            prev = node
        t += int(inst.travel_min[prev, 0])
        km += float(inst.km[prev, 0])
        route_rows.append(
            {
                "vehicle": v,
                "stops": len(route),
                "km": km,
                "return": t,
                "load": load,
                "capacity": inst.capacities[v],
                "overtime": t > config.SHIFT_END,
            }
        )
    stops = pd.DataFrame(
        stop_rows, columns=["stop", "vehicle", "position", "arrive", "service_start", "late"]
    )
    routes = pd.DataFrame(
        route_rows, columns=["vehicle", "stops", "km", "return", "load", "capacity", "overtime"]
    )
    return stops, routes


def _nearest_neighbour(inst: Instance, members: list[int]) -> list[int]:
    left = list(members)
    order: list[int] = []
    cur = 0
    while left:
        nxt = min(left, key=lambda i: inst.km[cur, i + 1])
        order.append(nxt)
        left.remove(nxt)
        cur = nxt + 1
    return order


def _route_end(inst: Instance, route: list[int]) -> int:
    s = inst.stops
    t = config.SHIFT_START
    prev = 0
    for i in route:
        t = max(t + int(inst.travel_min[prev, i + 1]), int(s.at[i, "tw_start"])) + int(s.at[i, "service_min"])
        prev = i + 1
    return t + int(inst.travel_min[prev, 0])


def sweep_plan(inst: Instance) -> Plan:
    t0 = time.perf_counter()
    s = inst.stops
    dx = s["x"].to_numpy() - inst.depot_xy[0]
    dy = s["y"].to_numpy() - inst.depot_xy[1]
    angle_order = list(np.argsort(np.arctan2(dy, dx), kind="stable"))
    routes: list[list[int]] = []
    v = 0
    cur: list[int] = []
    load = 0
    for i in angle_order:
        i = int(i)
        if v >= len(inst.capacities):
            break
        p = int(s.at[i, "pallets"])
        trial = _nearest_neighbour(inst, [*cur, i])
        if load + p <= inst.capacities[v] and _route_end(inst, trial) <= config.SHIFT_END:
            cur, load = trial, load + p
            continue
        routes.append(cur)
        v += 1
        cur, load = [], 0
        if v < len(inst.capacities) and _route_end(inst, [i]) <= config.SHIFT_END:
            cur, load = [i], p
    if cur and v < len(inst.capacities):
        routes.append(cur)
    return Plan(routes=routes, solve_seconds=time.perf_counter() - t0)


def ortools_plan(
    inst: Instance,
    time_limit_s: float,
    risk_weight: float = 0.0,
) -> Plan:
    """CVRP with time windows. With `risk_weight` > 0, each stop pays
    risk_weight * risk_slope km for every position past the first (see `risk` for the
    derivation); 0 turns the risk term off."""
    t0 = time.perf_counter()
    n, n_veh = inst.n, len(inst.capacities)
    s = inst.stops
    manager = pywrapcp.RoutingIndexManager(n + 1, n_veh, 0)
    routing = pywrapcp.RoutingModel(manager)

    dist = np.round(inst.km * DIST_UNITS_PER_KM).astype(int).tolist()
    service = [0, *s["service_min"].astype(int).tolist()]
    travel = inst.travel_min.tolist()
    demand = [0, *s["pallets"].astype(int).tolist()]

    def dist_cb(a: int, b: int) -> int:
        return int(dist[manager.IndexToNode(a)][manager.IndexToNode(b)])

    def time_cb(a: int, b: int) -> int:
        i, j = manager.IndexToNode(a), manager.IndexToNode(b)
        return int(service[i] + travel[i][j])

    def demand_cb(a: int) -> int:
        return int(demand[manager.IndexToNode(a)])

    dist_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(dist_idx)
    routing.SetFixedCostOfAllVehicles(VEHICLE_FIXED_KM * DIST_UNITS_PER_KM)

    cap_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(cap_idx, 0, inst.capacities, True, "load")

    time_idx = routing.RegisterTransitCallback(time_cb)
    routing.AddDimension(time_idx, 600, config.SHIFT_END, False, "time")
    tdim = routing.GetDimensionOrDie("time")
    for i in range(n):
        idx = manager.NodeToIndex(i + 1)
        tdim.CumulVar(idx).SetRange(int(s.at[i, "tw_start"]), int(s.at[i, "tw_end"]))
    for v in range(n_veh):
        tdim.CumulVar(routing.Start(v)).SetRange(config.SHIFT_START, config.SHIFT_END)
        tdim.CumulVar(routing.End(v)).SetRange(config.SHIFT_START, config.SHIFT_END)

    if risk_weight > 0:
        routing.AddConstantDimension(1, n + 1, True, "position")
        pdim = routing.GetDimensionOrDie("position")
        for i in range(n):
            coef = round(float(s.at[i, "risk_slope"]) * risk_weight * DIST_UNITS_PER_KM)
            if coef > 0:
                # Cumul at a stop is its 1-based position; anything past the first stop pays.
                pdim.SetCumulVarSoftUpperBound(manager.NodeToIndex(i + 1), 1, coef)

    age = s["age_days"].to_numpy()
    for i in range(n):
        penalty = int(DROP_PENALTY_KM * DIST_UNITS_PER_KM * (1 + age[i]))
        routing.AddDisjunction([manager.NodeToIndex(i + 1)], penalty)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromMilliseconds(int(time_limit_s * 1000))
    sol = routing.SolveWithParameters(params)
    routes: list[list[int]] = []
    if sol is not None:
        for v in range(n_veh):
            idx = routing.Start(v)
            route = []
            idx = sol.Value(routing.NextVar(idx))
            while not routing.IsEnd(idx):
                route.append(manager.IndexToNode(idx) - 1)
                idx = sol.Value(routing.NextVar(idx))
            routes.append(route)
    meta = {"objective": float(sol.ObjectiveValue()) if sol else math.nan}
    return Plan(routes=routes, solve_seconds=time.perf_counter() - t0, meta=meta)
