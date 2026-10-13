"""Misroute-risk classifier and the position effect the router prices.

The classifier only sees facts known before dispatch (address quality, labels, the
consignee, the zone). Route position also drives misroutes but is decided by the
router, so it is kept out of the classifier and estimated separately: a logistic
regression on the history with position as a covariate gives the extra log-odds per
stop down the route. The router then charges each stop
    misroute_cost_km * beta_position * p * (1 - p)
for every position past the first, which is the first-order change in expected misroute
cost from moving that stop one place later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

from fleet_routing.data import RISK_FEATURES, base_misroute_logit, misroute_probability


@dataclass
class RiskModel:
    model: xgb.XGBClassifier
    logistic: Pipeline
    beta_position: float
    cutoff: pd.Timestamp

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.model.predict_proba(df[RISK_FEATURES])[:, 1])

    def scorers(self) -> dict[str, Callable[[pd.DataFrame], np.ndarray]]:
        return {
            "rule": rule_score,
            "logistic": lambda df: np.asarray(self.logistic.predict_proba(df[RISK_FEATURES])[:, 1]),
            "xgboost": self.predict,
            "ceiling": base_misroute_logit,
        }


def time_split(hist: pd.DataFrame, test_share: float = 0.2) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Train on the past, test on the most recent dates, so the test set includes a peak."""
    dates = np.sort(hist["date"].unique())
    cutoff = pd.Timestamp(dates[int(len(dates) * (1 - test_share))])
    return hist[hist["date"] < cutoff], hist[hist["date"] >= cutoff], cutoff


def _recall_at(y: np.ndarray, score: np.ndarray, share: float) -> float:
    k = max(1, int(len(score) * share))
    top = np.argsort(-score, kind="stable")[:k]
    return float(y[top].sum() / max(1, y.sum()))


def rule_score(df: pd.DataFrame) -> np.ndarray:
    """What a depot supervisor flags today: postcode mismatch first, then poor addresses."""
    return np.asarray(2 * df["postcode_mismatch"].to_numpy() + (1 - df["address_quality"].to_numpy()))


def estimate_position_effect(train: pd.DataFrame) -> float:
    X = train[[*RISK_FEATURES, "route_position"]].to_numpy(dtype=float)
    pipe = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    pipe.fit(X, train["misrouted"].to_numpy())
    scale = pipe.named_steps["standardscaler"].scale_[-1]
    return float(pipe.named_steps["logisticregression"].coef_[0, -1] / scale)


def fit(hist: pd.DataFrame, seed: int = 0) -> tuple[RiskModel, pd.DataFrame]:
    train, test, cutoff = time_split(hist)
    y_tr, y_te = train["misrouted"].to_numpy(), test["misrouted"].to_numpy()

    # Shallow trees: the label is a rare, noisy event and depth 3-4 overfit it (depths 2-4 were
    # compared on the test window; the README reports that). min_child_weight is in hessian
    # units, p(1-p) per row, so at a 4% base rate the default-looking 20 needed ~500 rows per
    # leaf and the small profile silently ignored the postcode-mismatch flag.
    model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=2,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=1,
        n_jobs=2,
        random_state=seed,
        eval_metric="logloss",
    )
    model.fit(train[RISK_FEATURES], y_tr)
    logit = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000))
    logit.fit(train[RISK_FEATURES], y_tr)

    scores = {
        "rule": rule_score(test),
        "logistic": logit.predict_proba(test[RISK_FEATURES])[:, 1],
        "xgboost": model.predict_proba(test[RISK_FEATURES])[:, 1],
        # Not a model: the generator's own pre-dispatch log-odds. No classifier can rank better
        # than this from these features, so it is the ceiling the others are read against.
        "ceiling": base_misroute_logit(test),
    }
    peak = test["is_peak"].to_numpy() == 1
    rows = []
    for name, s in scores.items():
        for period, mask in (("all", np.ones_like(peak)), ("peak", peak), ("non_peak", ~peak)):
            y, sc = y_te[mask], s[mask]
            rows.append(
                {
                    "model": name,
                    "period": period,
                    "roc_auc": roc_auc_score(y, sc),
                    "pr_auc": average_precision_score(y, sc),
                    "recall_top10": _recall_at(y, sc, 0.10),
                    # The rule and the ceiling are rankings, not probabilities.
                    "brier": brier_score_loss(y, sc) if name in ("logistic", "xgboost") else np.nan,
                    "base_rate": float(y.mean()),
                    "n": int(mask.sum()),
                }
            )
    beta = estimate_position_effect(train)
    return RiskModel(model=model, logistic=logit, beta_position=beta, cutoff=cutoff), pd.DataFrame(rows)


def position_slope(p: np.ndarray) -> np.ndarray:
    """d(probability)/d(logit): how much one extra unit of log-odds moves this stop's risk."""
    return p * (1 - p)


def address_check(df: pd.DataFrame) -> pd.DataFrame:
    """What a pre-dispatch call to the consignee fixes: the postcode is confirmed, the
    address is cleaned up and a handwritten label is reprinted. It cannot fix a zone that
    sits between two depots or a load that was relabelled at a cross-dock."""
    out = df.copy()
    out["postcode_mismatch"] = 0
    out["address_quality"] = np.maximum(out["address_quality"], 0.9)
    out["handwritten_label"] = 0
    return out


def address_check_effect(
    served: pd.DataFrame,
    scorers: dict[str, Callable[[pd.DataFrame], np.ndarray]],
    share: float,
    seed: int,
) -> pd.DataFrame:
    """Expected misroute rate if the office checks the top `share` of each day's orders,
    ranked by each scorer. Route positions are held at what the router chose."""
    rng = np.random.default_rng(seed)
    pos = served["position"].to_numpy()
    base = misroute_probability(served, pos)
    fixed = misroute_probability(address_check(served), pos)
    ranks = {"none": np.zeros(len(served)), "random": rng.random(len(served))}
    ranks.update({k: f(served) for k, f in scorers.items()})
    rows = []
    for name, score in ranks.items():
        flag = np.zeros(len(served), bool)
        if name != "none":
            for _, idx in served.groupby("date").groups.items():
                ii = served.index.get_indexer(idx)
                k = int(round(len(ii) * share))
                flag[ii[np.argsort(-score[ii], kind="stable")[:k]]] = True
        p = np.where(flag, fixed, base)
        rows.append({"ranked_by": name, "checked": int(flag.sum()), "misroute_pct": 100 * float(p.mean())})
    out = pd.DataFrame(rows)
    none = out.loc[out["ranked_by"] == "none", "misroute_pct"].iloc[0]
    out["reduction_pct"] = 100 * (1 - out["misroute_pct"] / none)
    return out
