# Run report (full profile)

```
{
  "profile": "full",
  "trucks": 300,
  "depots": 4,
  "zones": 40,
  "dispatch_orders": 24394,
  "solver_seconds_per_depot_day": 6.0,
  "position_effect_logodds_per_stop": 0.09407839351102824,
  "misroute_cost_km": 150.0,
  "runtime_seconds": 1063.0
}
```

## Routing replay

| arm | window | orders | delivered | stranded | total_km | vehicle_days | stops_per_route | on_time_pct | misroute_pct | lead_days_mean | next_day_pct | within_2_days_pct |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| sweep | quiet | 10840 | 10840 | 0 | 105,489.717 | 1058 | 10.246 | 79.880 | 3.065 | 1.031 | 96.917 | 99.999 |
| ortools | quiet | 10840 | 10840 | 0 | 88,698.030 | 1018 | 10.648 | 100.000 | 3.163 | 1.032 | 96.837 | 100.000 |
| ortools_risk | quiet | 10840 | 10840 | 0 | 91,828.519 | 1059 | 10.236 | 100.000 | 2.979 | 1.030 | 97.021 | 100.000 |
| sweep | peak | 13554 | 13554 | 0 | 140,683.442 | 1457 | 9.303 | 81.636 | 4.410 | 1.123 | 91.418 | 97.692 |
| ortools | peak | 13554 | 13554 | 0 | 109,607.329 | 1339 | 10.122 | 100.000 | 4.581 | 1.052 | 94.882 | 99.912 |
| ortools_risk | peak | 13554 | 13554 | 0 | 113,937.688 | 1370 | 9.893 | 100.000 | 4.387 | 1.050 | 95.076 | 99.913 |
| ortools_risk_500km | peak | 13554 | 13554 | 0 | 118,014.390 | 1379 | 9.829 | 100.000 | 4.319 | 1.050 | 95.142 | 99.914 |
| ortools_risk_2000km | peak | 13554 | 13554 | 0 | 129,783.109 | 1429 | 9.485 | 100.000 | 4.332 | 1.050 | 95.129 | 99.914 |
| ortools_risk_naive_roster | peak | 13554 | 13554 | 0 | 114,132.301 | 1362 | 9.952 | 100.000 | 4.416 | 1.051 | 94.997 | 99.918 |

## Zone demand forecast (rolling weekly origin)

| model | period | wape | bias | zone_days |
|---|---|---|---|---|
| xgboost | all | 0.208 | -0.009 | 3360 |
| ets | all | 0.260 | -0.004 | 3360 |
| sarima | all | 0.262 | 0.011 | 3360 |
| seasonal_naive | all | 0.344 | 0.035 | 3360 |
| xgboost | non_peak | 0.211 | 0.010 | 1880 |
| ets | non_peak | 0.260 | 0.080 | 1880 |
| sarima | non_peak | 0.265 | 0.099 | 1880 |
| seasonal_naive | non_peak | 0.350 | 0.090 | 1880 |
| xgboost | peak | 0.204 | -0.031 | 1480 |
| sarima | peak | 0.259 | -0.085 | 1480 |
| ets | peak | 0.260 | -0.096 | 1480 |
| seasonal_naive | peak | 0.337 | -0.024 | 1480 |

## Driver booking from each forecast

| model | period | short_depot_days_pct | trucks_short | trucks_idle | depot_days |
|---|---|---|---|---|---|
| ets | all | 27.917 | 331.000 | 1,922.000 | 240 |
| sarima | all | 24.167 | 277.000 | 2,169.000 | 240 |
| seasonal_naive | all | 19.167 | 438.000 | 2,103.000 | 240 |
| xgboost | all | 8.333 | 68.000 | 1,304.000 | 240 |
| ets | non_peak | 8.088 | 37.000 | 1,464.000 | 136 |
| sarima | non_peak | 5.882 | 28.000 | 1,650.000 | 136 |
| seasonal_naive | non_peak | 13.235 | 73.000 | 1,480.000 | 136 |
| xgboost | non_peak | 8.088 | 44.000 | 893.000 | 136 |
| ets | peak | 53.846 | 294.000 | 458.000 | 104 |
| sarima | peak | 48.077 | 249.000 | 519.000 | 104 |
| seasonal_naive | peak | 26.923 | 365.000 | 623.000 | 104 |
| xgboost | peak | 8.654 | 24.000 | 411.000 | 104 |

## Misroute classifier (time split, most recent 20% of dates held out)

| model | period | roc_auc | pr_auc | recall_top10 | brier | base_rate | n |
|---|---|---|---|---|---|---|---|
| rule | all | 0.596 | 0.084 | 0.242 | nan | 0.041 | 42453 |
| rule | peak | 0.614 | 0.135 | 0.273 | nan | 0.053 | 11229 |
| rule | non_peak | 0.587 | 0.068 | 0.226 | nan | 0.037 | 31224 |
| logistic | all | 0.647 | 0.112 | 0.284 | 0.038 | 0.041 | 42453 |
| logistic | peak | 0.668 | 0.163 | 0.312 | 0.048 | 0.053 | 11229 |
| logistic | non_peak | 0.626 | 0.085 | 0.257 | 0.035 | 0.037 | 31224 |
| xgboost | all | 0.645 | 0.110 | 0.279 | 0.038 | 0.041 | 42453 |
| xgboost | peak | 0.665 | 0.161 | 0.295 | 0.048 | 0.053 | 11229 |
| xgboost | non_peak | 0.624 | 0.080 | 0.256 | 0.035 | 0.037 | 31224 |
| ceiling | all | 0.647 | 0.108 | 0.284 | nan | 0.041 | 42453 |
| ceiling | peak | 0.667 | 0.150 | 0.305 | nan | 0.053 | 11229 |
| ceiling | non_peak | 0.626 | 0.080 | 0.265 | nan | 0.037 | 31224 |

## Pre-dispatch address checks on 5% of orders, ortools_risk routes

| ranked_by | checked | misroute_pct | reduction_pct | window |
|---|---|---|---|---|
| none | 0 | 2.979 | 0.000 | quiet |
| random | 542 | 2.954 | 0.828 | quiet |
| rule | 542 | 2.629 | 11.721 | quiet |
| logistic | 542 | 2.658 | 10.765 | quiet |
| xgboost | 542 | 2.640 | 11.354 | quiet |
| ceiling | 542 | 2.643 | 11.264 | quiet |
| none | 0 | 4.387 | 0.000 | peak |
| random | 677 | 4.340 | 1.079 | peak |
| rule | 677 | 3.658 | 16.629 | peak |
| logistic | 677 | 3.694 | 15.806 | peak |
| xgboost | 677 | 3.668 | 16.384 | peak |
| ceiling | 677 | 3.681 | 16.100 | peak |
