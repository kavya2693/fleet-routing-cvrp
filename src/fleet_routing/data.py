"""Seeded generator for the network, zone demand, shipment history and dispatch orders.

The production data is proprietary, so everything here is produced from a seed and
sized to the real problem. The patterns that matter for the decisions are built in on
purpose: weekly and annual seasonality, a pre-holiday peak, public holidays with
catch-up the next working day, bulk-order outliers, and gaps in the order feed.

The misroute hazard lives here too, as the ground truth the classifier has to recover.
It has one term the classifier never sees: the stop's position in the route. That term
is what the routing step can act on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from fleet_routing import config
from fleet_routing.config import Profile

RISK_FEATURES = [
    "pallets",
    "weight_kg",
    "address_quality",
    "postcode_mismatch",
    "new_consignee",
    "relabelled",
    "handwritten_label",
    "hazmat",
    "express",
    "dist_to_depot_km",
    "boundary_ratio",
    "zone_density",
    "is_peak",
    "dow",
]


@dataclass
class Network:
    depots: pd.DataFrame  # depot, x, y, vehicles
    zones: pd.DataFrame  # zone, x, y, depot, dist_to_depot_km, boundary_ratio, density, share, peak_amp


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def public_holidays(years: range) -> set[date]:
    days: set[date] = set()
    for y in years:
        days |= {
            date(y, 1, 1),
            _last_weekday(y, 5, 0),
            date(y, 7, 4),
            _nth_weekday(y, 9, 0, 1),
            _nth_weekday(y, 11, 3, 4),
            date(y, 12, 25),
            date(y, 12, 26),
        }
    return days


def peak_factor(d: pd.DatetimeIndex) -> np.ndarray:
    """0 outside the pre-holiday peak, rising to 1 in mid-December, 0 again after the 23rd."""
    doy = d.dayofyear.to_numpy()
    start = pd.to_datetime([f"{y}-11-15" for y in d.year]).dayofyear.to_numpy()
    top = pd.to_datetime([f"{y}-12-12" for y in d.year]).dayofyear.to_numpy()
    end = pd.to_datetime([f"{y}-12-23" for y in d.year]).dayofyear.to_numpy()
    rise = np.clip((doy - start) / (top - start), 0, 1)
    fall = np.clip((end - doy) / (end - top), 0, 1)
    out = np.minimum(rise, fall)
    out[(doy < start) | (doy > end)] = 0.0
    return out


def is_peak(d: pd.DatetimeIndex) -> np.ndarray:
    return peak_factor(d) > 0


def build_network(p: Profile, rng: np.random.Generator) -> Network:
    depots = pd.DataFrame(
        {
            "depot": range(len(p.depots_km)),
            "x": [xy[0] for xy in p.depots_km],
            "y": [xy[1] for xy in p.depots_km],
            "vehicles": 0,
        }
    )
    w, h = p.region_km
    # Two thirds of zones cluster around depots (the towns depots were sited for); the rest
    # are spread across the rural part of the region.
    n_town = round(p.n_zones * 2 / 3)
    anchor = rng.integers(0, len(depots), n_town)
    town = depots[["x", "y"]].to_numpy()[anchor] + rng.normal(0, 18, (n_town, 2))
    rural = rng.uniform([8, 8], [w - 8, h - 8], (p.n_zones - n_town, 2))
    xy = np.clip(np.vstack([town, rural]), [4, 4], [w - 4, h - 4])
    dxy = np.linalg.norm(xy[:, None, :] - depots[["x", "y"]].to_numpy()[None, :, :], axis=2)
    order = np.argsort(dxy, axis=1)
    nearest = dxy[np.arange(len(xy)), order[:, 0]]
    if len(depots) > 1:
        second = dxy[np.arange(len(xy)), order[:, 1]]
        boundary = nearest / second
    else:
        boundary = nearest / float(np.hypot(w, h))
    density = 1 + 2 * np.exp(-nearest / 35)
    share = rng.lognormal(0, 0.45, len(xy)) * density
    zones = pd.DataFrame(
        {
            "zone": range(len(xy)),
            "x": xy[:, 0],
            "y": xy[:, 1],
            "depot": order[:, 0],
            "dist_to_depot_km": nearest,
            "boundary_ratio": boundary,
            "density": density,
            "share": share / share.sum(),
            # Some zones are retail-heavy and feel the holiday peak much more than others.
            "peak_amp": rng.uniform(0.15, 0.55, len(xy)),
            "spread_km": rng.uniform(3.5, 9.0, len(xy)) / np.sqrt(density),
        }
    )
    # The fleet was sized to demand: each depot gets vehicles in proportion to its zones' share.
    dshare = zones.groupby("depot")["share"].sum().reindex(depots["depot"], fill_value=0).to_numpy()
    alloc = np.floor(dshare * p.fleet_size).astype(int)
    alloc[np.argsort(-(dshare * p.fleet_size - alloc))[: p.fleet_size - alloc.sum()]] += 1
    depots["vehicles"] = alloc
    return Network(depots=depots, zones=zones)


def zone_demand(p: Profile, net: Network, rng: np.random.Generator) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Daily order counts per zone. Returns (true counts, observed counts with feed gaps)."""
    days = pd.date_range(p.history_start, p.history_end, freq="D")
    hols = public_holidays(range(p.history_start.year, p.history_end.year + 1))
    dow_mult = np.array([1.08, 1.02, 1.0, 1.0, 1.05, 0.42, 0.08])
    t = np.arange(len(days)) / 365.0
    annual = 1 + 0.07 * np.sin(2 * np.pi * (days.dayofyear.to_numpy() - 100) / 365.25)
    trend = 1 + 0.06 * t
    hol = np.ones(len(days))
    is_hol = np.array([d.date() in hols for d in days])
    hol[is_hol] = 0.12
    # The next working day catches up most of what the holiday did not ship.
    for i in np.flatnonzero(is_hol):
        j = i + 1
        while j < len(days) and (is_hol[j] or days[j].dayofweek >= 5):
            j += 1
        if j < len(days):
            hol[j] *= 1.45
    pk = peak_factor(days)
    z = net.zones
    base = p.weekday_orders / dow_mult[:5].mean() * z["share"].to_numpy()
    mu = (
        base[None, :]
        * dow_mult[days.dayofweek.to_numpy()][:, None]
        * (annual * trend * hol)[:, None]
        * (1 + pk[:, None] * z["peak_amp"].to_numpy()[None, :])
    )
    # Negative binomial: variance grows faster than the mean, as order counts do.
    k = 25.0
    counts = rng.negative_binomial(k, k / (k + mu)).astype(float)
    bulk = rng.random(counts.shape) < 0.003
    counts[bulk] = np.round(counts[bulk] * rng.uniform(2.0, 3.5, bulk.sum()))
    true = pd.DataFrame(counts, index=days, columns=z["zone"].to_numpy())
    observed = true.copy()
    gaps = rng.random(counts.shape) < 0.012
    observed[gaps] = np.nan
    # One multi-day feed outage on a couple of zones, the kind that happens after an EDI change.
    out_start = int(rng.integers(60, len(days) - 120))
    for zone in rng.choice(z["zone"].to_numpy(), size=min(2, len(z)), replace=False):
        observed.iloc[out_start : out_start + 4, int(zone)] = np.nan
    true.index.name = observed.index.name = "date"
    return true, observed


def _shipment_features(
    n: int, zone_ids: np.ndarray, dates: pd.DatetimeIndex, net: Network, rng: np.random.Generator
) -> pd.DataFrame:
    z = net.zones.set_index("zone").loc[zone_ids]
    pallets = np.minimum(1 + rng.poisson(1.1, n), 10)
    new = rng.random(n) < 0.15
    df = pd.DataFrame(
        {
            "zone": zone_ids,
            "date": dates,
            "pallets": pallets,
            "weight_kg": np.round(pallets * rng.lognormal(np.log(380), 0.35, n)),
            # New consignees have worse address data; that correlation is real in freight.
            "address_quality": np.clip(rng.beta(8, 1.6, n) - 0.12 * new, 0, 1),
            "postcode_mismatch": (rng.random(n) < 0.035).astype(int),
            "new_consignee": new.astype(int),
            "relabelled": (rng.random(n) < 0.10).astype(int),
            "handwritten_label": (rng.random(n) < 0.05).astype(int),
            "hazmat": (rng.random(n) < 0.03).astype(int),
            "express": (rng.random(n) < 0.18).astype(int),
            "dist_to_depot_km": z["dist_to_depot_km"].to_numpy(),
            "boundary_ratio": z["boundary_ratio"].to_numpy(),
            "zone_density": z["density"].to_numpy(),
            "is_peak": is_peak(dates).astype(int),
            "dow": dates.dayofweek.to_numpy(),
        }
    )
    return df


def base_misroute_logit(df: pd.DataFrame) -> np.ndarray:
    """Ground-truth risk from pre-dispatch facts. Non-linear on purpose (interactions, thresholds)."""
    aq = df["address_quality"].to_numpy()
    mismatch = df["postcode_mismatch"].to_numpy()
    peak = df["is_peak"].to_numpy()
    return np.asarray(
        -4.3
        + 1.7 * mismatch
        + 3.2 * (aq < 0.55) * (0.55 - aq) / 0.55
        + 0.5 * df["new_consignee"].to_numpy()
        + 0.75 * df["relabelled"].to_numpy()
        + 0.45 * df["handwritten_label"].to_numpy()
        + 0.35 * peak
        + 0.6 * mismatch * peak
        # Zones nearly equidistant from two depots get sorted to the wrong one.
        + 1.1 * np.clip((df["boundary_ratio"].to_numpy() - 0.75) / 0.25, 0, 1)
        - 0.2 * df["express"].to_numpy()
    )


def position_logit(position: np.ndarray) -> np.ndarray:
    """Later stops on a multi-drop route are pulled from deeper in the trailer, past more
    look-alike pallets, and go wrong more often. Capped: past ~15 stops it stops growing."""
    return 0.09 * np.minimum(np.maximum(position - 1, 0), 15)


def misroute_probability(df: pd.DataFrame, position: np.ndarray) -> np.ndarray:
    logit = base_misroute_logit(df) + position_logit(position)
    return np.asarray(1 / (1 + np.exp(-logit)))


def shipment_history(p: Profile, net: Network, true: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    flat = true.stack()
    weights = flat.to_numpy() / flat.sum()
    pick = rng.choice(len(flat), size=p.n_history_shipments, p=weights)
    idx = flat.index[pick]
    dates = pd.DatetimeIndex(idx.get_level_values(0))
    zone_ids = idx.get_level_values(1).to_numpy().astype(int)
    df = _shipment_features(p.n_history_shipments, zone_ids, dates, net, rng)
    # Historical routes were built by hand; position was close to arbitrary.
    position = rng.integers(1, 15, len(df))
    df["route_position"] = position
    df["misrouted"] = (rng.random(len(df)) < misroute_probability(df, position)).astype(int)
    return df.sort_values("date").reset_index(drop=True)


def dispatch_orders(p: Profile, net: Network, true: pd.DataFrame) -> pd.DataFrame:
    """Individual orders for every order day in the evaluation windows, one stop per order."""
    rows = []
    for w_start, w_end in p.eval_windows:
        for d in pd.date_range(w_start, w_end, freq="D"):
            # Each day has its own stream so adding a window never changes another window's orders.
            rng = np.random.default_rng([p.seed, d.toordinal()])
            counts = true.loc[d].astype(int)
            zone_ids = np.repeat(counts.index.to_numpy().astype(int), counts.to_numpy())
            n = len(zone_ids)
            if n == 0:
                continue
            df = _shipment_features(n, zone_ids, pd.DatetimeIndex([d] * n), net, rng)
            z = net.zones.set_index("zone").loc[zone_ids]
            w, h = p.region_km
            df["x"] = np.clip(z["x"].to_numpy() + rng.normal(0, 1, n) * z["spread_km"].to_numpy(), 1, w - 1)
            df["y"] = np.clip(z["y"].to_numpy() + rng.normal(0, 1, n) * z["spread_km"].to_numpy(), 1, h - 1)
            df["depot"] = z["depot"].to_numpy()
            kind = rng.choice(4, size=n, p=[0.45, 0.20, 0.20, 0.15])
            narrow_start = rng.integers(8, 16, n) * 60
            df["tw_start"] = np.select(
                [kind == 0, kind == 1, kind == 2], [8 * 60, 8 * 60, 12 * 60], narrow_start
            )
            df["tw_end"] = np.select(
                [kind == 0, kind == 1, kind == 2], [17 * 60, 12 * 60, 17 * 60], narrow_start + 120
            )
            df["service_min"] = config.SERVICE_BASE_MIN + config.SERVICE_PER_PALLET_MIN * df["pallets"]
            rows.append(df)
    orders = pd.concat(rows, ignore_index=True)
    orders.insert(0, "order_id", [f"S{i:06d}" for i in range(len(orders))])
    return orders


def generate(p: Profile, out: Path) -> dict[str, int]:
    rng = np.random.default_rng(p.seed)
    net = build_network(p, rng)
    true, observed = zone_demand(p, net, rng)
    hist = shipment_history(p, net, true, rng)
    orders = dispatch_orders(p, net, true)
    out.mkdir(parents=True, exist_ok=True)
    net.depots.to_csv(out / "depots.csv", index=False)
    net.zones.to_csv(out / "zones.csv", index=False)
    true.to_csv(out / "zone_demand_true.csv")
    observed.to_csv(out / "zone_demand.csv")
    hist.to_csv(out / "shipments_history.csv", index=False)
    orders.to_csv(out / "dispatch_orders.csv", index=False)
    summary = {
        "zones": len(net.zones),
        "depots": len(net.depots),
        "vehicles": int(net.depots["vehicles"].sum()),
        "history_days": len(true),
        "missing_zone_days": int(observed.isna().sum().sum()),
        "history_shipments": len(hist),
        "history_misroute_rate_bp": round(hist["misrouted"].mean() * 10_000),
        "dispatch_orders": len(orders),
    }
    (out / "manifest.json").write_text(json.dumps({"profile": p.name, "seed": p.seed, **summary}, indent=2))
    return summary


@dataclass
class Dataset:
    depots: pd.DataFrame
    zones: pd.DataFrame
    demand_true: pd.DataFrame
    demand_observed: pd.DataFrame
    history: pd.DataFrame
    orders: pd.DataFrame


def load(data_dir: Path) -> Dataset:
    def demand(name: str) -> pd.DataFrame:
        df = pd.read_csv(data_dir / name, index_col="date", parse_dates=["date"])
        df.columns = df.columns.astype(int)
        return df

    return Dataset(
        depots=pd.read_csv(data_dir / "depots.csv"),
        zones=pd.read_csv(data_dir / "zones.csv"),
        demand_true=demand("zone_demand_true.csv"),
        demand_observed=demand("zone_demand.csv"),
        history=pd.read_csv(data_dir / "shipments_history.csv", parse_dates=["date"]),
        orders=pd.read_csv(data_dir / "dispatch_orders.csv", parse_dates=["date"]),
    )
