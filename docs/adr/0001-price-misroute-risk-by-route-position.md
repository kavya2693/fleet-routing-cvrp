# ADR-0001: Price misroute risk through route position, inside the routing objective

Status: accepted

## Context

The brief asks for a misroute-risk classifier whose scores change how the vehicle-routing
optimizer treats a shipment ("penalties or priority"). There are several ways to wire a
probability into a CVRP, and most of them produce a number that looks like an improvement
without any account of why routes should change.

Two facts constrain the choice:

1. Most of what makes a shipment risky (a mismatched postcode, a poor address, a zone halfway
   between two depots) is fixed before the truck is loaded. Routing cannot change it.
2. The part routing can change is where the shipment sits on the route. History records a
   route position for each shipment, so its effect on misroutes can be estimated directly.

## Decision

- The classifier sees only pre-dispatch facts. Route position is kept out of it, because the
  router decides position and a model trained on it would be scoring the router's own choice.
- The position effect is estimated separately: a logistic regression on the history with
  position as a covariate gives the extra log-odds per stop (`beta`).
- The router charges each stop `misroute_cost_km * beta * p * (1 - p)` per position past the
  first, using OR-Tools' soft upper bound on a stop-count dimension. That is the first-order
  change in expected misroute cost from moving the stop one place later, in the same unit
  (km) as the rest of the objective.
- `misroute_cost_km` is an assumption, so the run sweeps it and reports the km it buys.
- Every reported number comes from one replay function that drives the finished routes and
  scores misroutes with the generator's ground-truth hazard at the position the stop actually
  got. Neither planner reports its own metrics.

## Rejected

- **Hard "risky stops first" rule.** Easy to explain, but the price is invisible: it forces
  detours for shipments whose risk is barely above the cut.
- **Dedicated trucks for risky shipments.** Adds trucks for a 4% event; worse on every cost.
- **Using the classifier only as a drop priority.** Changes which shipments roll over, not how
  often they go wrong once loaded. Kept separate: drop penalty grows with days already waited.
- **LSTM for zone demand.** The brief made it optional "only if cheap". With two years of daily
  history per zone, a global gradient-boosted model on lag features already beats the
  per-zone classical models, and an LSTM would add a framework dependency to CI for no
  expected gain at this series length.

## Consequences

Reordering stops inside a route moves which shipments sit deep in the trailer; it does not
make routes shorter. Expected gains from routing alone are therefore bounded and second order,
and the README reports them as measured. The larger lever the classifier offers, deciding which
consignees the office phones before loading, is evaluated separately with the routes held fixed.
