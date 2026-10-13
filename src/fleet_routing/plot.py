"""Route map: the same depot and dispatch day under two planners, side by side."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from fleet_routing import config


def route_map(
    panels: list[tuple[str, pd.DataFrame]],
    depot_xy: tuple[float, float],
    risk_cut: float,
    out: Path,
    title: str,
) -> None:
    """Each panel: (label, delivered stops for one depot-day with x, y, vehicle, position, p_pred)."""
    fig, axes = plt.subplots(1, len(panels), figsize=(6.2 * len(panels), 6.0), sharex=True, sharey=True)
    axes = np.atleast_1d(axes)
    cmap = plt.get_cmap("tab20")
    for ax, (label, df) in zip(axes, panels, strict=True):
        for k, (_, r) in enumerate(df.sort_values("position").groupby("vehicle")):
            xs = [depot_xy[0], *r["x"], depot_xy[0]]
            ys = [depot_xy[1], *r["y"], depot_xy[1]]
            ax.plot(xs, ys, lw=0.8, color=cmap(k % 20), alpha=0.8)
        ax.scatter(df["x"], df["y"], s=7, color="#555555", zorder=3)
        risky = df[df["p_pred"] >= risk_cut]
        ax.scatter(risky["x"], risky["y"], s=28, facecolors="none", edgecolors="#c0392b", lw=1.1, zorder=4,
                   label=f"high predicted misroute risk ({len(risky)})")
        ax.scatter([depot_xy[0]], [depot_xy[1]], marker="s", s=80, color="black", zorder=5, label="depot")
        km = sum(
            float(np.hypot(np.diff([depot_xy[0], *g["x"], depot_xy[0]]), np.diff([depot_xy[1], *g["y"], depot_xy[1]])).sum())
            for _, g in df.sort_values("position").groupby("vehicle")
        )
        mean_risky_pos = float(risky["position"].mean()) if len(risky) else float("nan")
        ax.set_title(
            f"{label}\n{df['vehicle'].nunique()} trucks, {km * config.CIRCUITY:,.0f} road km, "
            f"risky stops at mean position {mean_risky_pos:.1f}",
            fontsize=10,
        )
        ax.set_xlabel("km")
        ax.set_aspect("equal")
        ax.grid(alpha=0.2)
    axes[0].set_ylabel("km")
    axes[0].legend(loc="lower left", fontsize=8)
    fig.suptitle(title, fontsize=11)
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)
