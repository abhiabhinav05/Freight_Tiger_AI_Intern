"""Weekly cost per tonne-km per route, plus the two baselines defined in the brief."""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import HISTORY_WEEKS, OWN_THRESHOLD, PEER_THRESHOLD

REQUIRED = {"shipment_id", "origin", "destination", "route_type", "quantity_tonnes",
            "distance_km", "freight_cost_inr", "shipment_date"}


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@dataclass(frozen=True)
class RouteWeek:
    route: str
    route_type: str
    week: date
    rate: float                 # INR per tonne-km for the week
    shipments: int
    own_avg: float | None       # trailing 8-week average, strictly before `week`
    own_weeks: int              # how many prior weeks went into own_avg
    peer_avg: float | None      # same route_type, same week, this route excluded
    peer_routes: int

    @property
    def own_delta(self) -> float | None:
        return self.rate / self.own_avg - 1 if self.own_avg else None

    @property
    def peer_delta(self) -> float | None:
        return self.rate / self.peer_avg - 1 if self.peer_avg else None

    @property
    def rising(self) -> bool:
        return self.own_delta is not None and self.own_delta > 0

    @property
    def own_elevated(self) -> bool:
        return self.own_delta is not None and self.own_delta >= OWN_THRESHOLD

    @property
    def peer_elevated(self) -> bool:
        return self.rising and self.peer_delta is not None and self.peer_delta >= PEER_THRESHOLD

    @property
    def alert(self) -> bool:
        return self.own_elevated or self.peer_elevated


def load_shipments(path: Path) -> list[dict[str, str]]:
    rows = read_csv(path)
    if not rows or not REQUIRED.issubset(rows[0]):
        raise ValueError(f"{path.name} is empty or missing columns {sorted(REQUIRED)}")
    return rows


def route_weeks(shipments: list[dict[str, str]]) -> list[RouteWeek]:
    """Aggregate shipments to route-weeks and attach both baselines. Sorted by (week, route)."""
    cost: dict[tuple[str, date], float] = defaultdict(float)
    tonne_km: dict[tuple[str, date], float] = defaultdict(float)
    count: dict[tuple[str, date], int] = defaultdict(int)
    route_type: dict[str, str] = {}

    for row in shipments:
        route = f"{row['origin'].strip()}-{row['destination'].strip()}"
        qty, dist, inr = (float(row[k]) for k in ("quantity_tonnes", "distance_km", "freight_cost_inr"))
        if qty <= 0 or dist <= 0 or inr < 0:
            raise ValueError(f"Invalid quantity/distance/cost in shipment {row['shipment_id']}")
        rtype = row["route_type"].strip()
        if route_type.setdefault(route, rtype) != rtype:
            raise ValueError(f"Route {route} appears with two route types")
        key = (route, monday(date.fromisoformat(row["shipment_date"])))
        cost[key] += inr
        tonne_km[key] += qty * dist
        count[key] += 1

    # Ratio of sums, so heavy/long shipments weigh in proportion to the work done.
    rate = {key: cost[key] / tonne_km[key] for key in cost}

    result = []
    for route, week in sorted(rate, key=lambda k: (k[1], k[0])):
        history = [rate[(route, week - timedelta(weeks=i))] for i in range(1, HISTORY_WEEKS + 1)
                   if (route, week - timedelta(weeks=i)) in rate]
        peers = [value for (other, other_week), value in rate.items()
                 if other_week == week and other != route and route_type[other] == route_type[route]]
        result.append(RouteWeek(
            route=route, route_type=route_type[route], week=week, rate=rate[(route, week)],
            shipments=count[(route, week)],
            own_avg=sum(history) / len(history) if history else None, own_weeks=len(history),
            peer_avg=sum(peers) / len(peers) if peers else None, peer_routes=len(peers),
        ))
    return result


def format_delta(delta: float | None, label: str, missing: str) -> str:
    return f"{delta * 100:+.1f}% vs {label}" if delta is not None else f"N/A - {missing}"


def own_text(rw: RouteWeek) -> str:
    return format_delta(rw.own_delta, "this route's past average", "no prior weeks for this route")


def peer_text(rw: RouteWeek) -> str:
    return format_delta(rw.peer_delta, "similar-length routes this week",
                        "no other routes of this type this week")
