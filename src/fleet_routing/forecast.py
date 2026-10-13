"""Zone-level daily order forecasts, scored by a weekly rolling-origin backtest.

Drivers are booked a week ahead, so every forecast is made on a Monday from data up to
the Sunday before and covers the next seven days. All features for the gradient-boosted
model are lags of at least seven days, which keeps the whole week predictable from the
origin without feeding predictions back in.

Models:
- `seasonal_naive`: same weekday last week. The bar everything else has to clear.
- `ets`: Holt-Winters, additive weekly season, fitted per zone on the last year.
- `sarima`: SARIMA(1,0,1)(0,1,1)_7 per zone on the last half year.
- `xgboost`: one global model across zones, predicting the ratio of the day's orders to
  the zone's recent level from calendar and lag features.
"""

from __future__ import annotations

import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd
import xgboost as xgb
from statsmodels.tools.sm_exceptions import ConvergenceWarning
from statsmodels.tsa.holtwinters import ExponentialSmoothing
from statsmodels.tsa.statespace.sarimax import SARIMAX

from fleet_routing.data import is_peak, public_holidays

MODELS = ("seasonal_naive", "ets", "sarima", "xgboost")
LAGS = (7, 14, 21, 28)


def origins(history_end: date, weeks: int) -> list[pd.Timestamp]:
    """Mondays such that the last forecast week ends on or before `history_end`."""
    last = history_end - timedelta(days=6)
    last -= timedelta(days=last.weekday())
    return [pd.Timestamp(last - timedelta(weeks=k)) for k in reversed(range(weeks))]


def _clean(series: pd.Series) -> np.ndarray:
    """Feed gaps are filled from the same weekday a week earlier, then interpolated."""
    s = series.copy()
    s = s.fillna(s.shift(7)).interpolate(limit_direction="both")
    return np.asarray(s.to_numpy(dtype=float))


def seasonal_naive(y: pd.DataFrame, origin: pd.Timestamp) -> pd.DataFrame:
    hist = y.loc[: origin - pd.Timedelta(days=1)]
    last_week = hist.iloc[-7:].copy()
    filler = hist.iloc[-14:-7].to_numpy()
    last_week = last_week.where(last_week.notna(), filler)
    idx = pd.date_range(origin, periods=7, freq="D")
    return pd.DataFrame(last_week.to_numpy(), index=idx, columns=y.columns)


def _per_zone(y: pd.DataFrame, origin: pd.Timestamp, window: int, fit) -> pd.DataFrame:  # type: ignore[no-untyped-def]
    hist = y.loc[: origin - pd.Timedelta(days=1)].iloc[-window:]
    idx = pd.date_range(origin, periods=7, freq="D")
    out = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        warnings.simplefilter("ignore", UserWarning)
        warnings.simplefilter("ignore", RuntimeWarning)
        for z in y.columns:
            out[z] = np.clip(fit(_clean(hist[z])), 0, None)
    return pd.DataFrame(out, index=idx)


def ets(y: pd.DataFrame, origin: pd.Timestamp) -> pd.DataFrame:
    def fit(v: np.ndarray) -> np.ndarray:
        m = ExponentialSmoothing(v, trend=None, seasonal="add", seasonal_periods=7).fit()
        return np.asarray(m.forecast(7))

    return _per_zone(y, origin, 364, fit)


def sarima(y: pd.DataFrame, origin: pd.Timestamp) -> pd.DataFrame:
    def fit(v: np.ndarray) -> np.ndarray:
        m = SARIMAX(v, order=(1, 0, 1), seasonal_order=(0, 1, 1, 7)).fit(disp=False, maxiter=50)
        return np.asarray(m.forecast(7))

    return _per_zone(y, origin, 182, fit)


def calendar_features(days: pd.DatetimeIndex) -> pd.DataFrame:
    hols = public_holidays(range(days.min().year - 1, days.max().year + 2))
    is_hol = np.array([d.date() in hols for d in days])
    prev_hol = np.array([(d - pd.Timedelta(days=1)).date() in hols for d in days])
    # Monday after a Friday holiday also catches up.
    prev_hol |= np.array([d.dayofweek == 0 and (d - pd.Timedelta(days=3)).date() in hols for d in days])
    xmas = pd.to_datetime([f"{d.year}-12-25" for d in days])
    return pd.DataFrame(
        {
            "dow": days.dayofweek,
            "doy_sin": np.sin(2 * np.pi * days.dayofyear / 365.25),
            "doy_cos": np.cos(2 * np.pi * days.dayofyear / 365.25),
            "days_to_xmas": np.clip((xmas - days).days, -10, 60),
            "holiday": is_hol.astype(int),
            "after_holiday": prev_hol.astype(int),
        },
        index=days,
    )


def _design(y: pd.DataFrame) -> pd.DataFrame:
    """Long frame, one row per zone-day, with lag features that only look back >= 7 days."""
    cal = calendar_features(pd.DatetimeIndex(y.index))
    frames = []
    for z in y.columns:
        s = y[z]
        f = cal.copy()
        f["zone"] = int(z)
        for lag in LAGS:
            f[f"lag{lag}"] = s.shift(lag)
        f["level"] = s.shift(7).rolling(28, min_periods=14).mean()
        f["same_dow_mean"] = pd.concat([s.shift(lag) for lag in LAGS], axis=1).mean(axis=1)
        f["y"] = s
        frames.append(f)
    df = pd.concat(frames)
    for lag in LAGS:
        df[f"lag{lag}_rel"] = df[f"lag{lag}"] / df["level"]
    df["same_dow_rel"] = df["same_dow_mean"] / df["level"]
    return df


XGB_FEATURES = [
    "dow",
    "doy_sin",
    "doy_cos",
    "days_to_xmas",
    "holiday",
    "after_holiday",
    "zone",
    "level",
    *[f"lag{lag}_rel" for lag in LAGS],
    "same_dow_rel",
]


def xgboost_forecaster(y: pd.DataFrame, seed: int = 0):  # type: ignore[no-untyped-def]
    design = _design(y)

    def predict(_: pd.DataFrame, origin: pd.Timestamp) -> pd.DataFrame:
        train = design[(design.index < origin) & design["y"].notna() & (design["level"] > 0)]
        test = design[(design.index >= origin) & (design.index < origin + pd.Timedelta(days=7))]
        model = xgb.XGBRegressor(
            n_estimators=400,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            # Absolute error on the ratio: bulk-order spikes should not drag the fit.
            objective="reg:absoluteerror",
            n_jobs=2,
            random_state=seed,
        )
        model.fit(train[XGB_FEATURES], train["y"] / train["level"])
        pred = np.clip(model.predict(test[XGB_FEATURES]), 0, None) * test["level"].to_numpy()
        out = pd.DataFrame({"date": test.index, "zone": test["zone"].to_numpy(), "f": pred})
        return out.pivot(index="date", columns="zone", values="f")

    return predict


def backtest(
    observed: pd.DataFrame, history_end: date, weeks: int, models: tuple[str, ...] = MODELS
) -> pd.DataFrame:
    """Long frame: date, zone, model, forecast. Forecasts use only `observed` before each origin."""
    fns = {"seasonal_naive": seasonal_naive, "ets": ets, "sarima": sarima}
    if "xgboost" in models:
        fns["xgboost"] = xgboost_forecaster(observed)
    rows = []
    for origin in origins(history_end, weeks):
        for name in models:
            f = fns[name](observed, origin)
            long = f.stack().rename("forecast").reset_index()
            long.columns = ["date", "zone", "forecast"]
            long["model"] = name
            rows.append(long)
    out = pd.concat(rows, ignore_index=True)
    out["zone"] = out["zone"].astype(int)
    return out


def score(fc: pd.DataFrame, actual: pd.DataFrame) -> pd.DataFrame:
    """WAPE and bias per model, split into peak and non-peak days. Scored against what was
    actually ordered, not the gappy feed."""
    act = actual.stack().rename("actual").reset_index()
    act.columns = ["date", "zone", "actual"]
    act["zone"] = act["zone"].astype(int)
    df = fc.merge(act, on=["date", "zone"])
    df["period"] = np.where(is_peak(pd.DatetimeIndex(df["date"])), "peak", "non_peak")
    rows = []
    for (model, period), g in pd.concat([df, df.assign(period="all")]).groupby(["model", "period"]):
        err = g["forecast"] - g["actual"]
        rows.append(
            {
                "model": model,
                "period": period,
                "wape": float(err.abs().sum() / g["actual"].sum()),
                "bias": float(err.sum() / g["actual"].sum()),
                "zone_days": len(g),
            }
        )
    return pd.DataFrame(rows)
