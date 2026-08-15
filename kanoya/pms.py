"""自社 PMS 実績の読み込み。

この CSV があるかどうかで、このシステムの性格が変わる。
無ければ全部が仮定の上に建つ推定。有れば投稿率を実測でき、
推定値を自分の実績に対して後から検証できる。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path


class ActualsError(ValueError):
    pass


@dataclass(frozen=True)
class DayActual:
    day: date
    rooms_sold: int
    rooms_available: int

    @property
    def occupancy(self) -> float:
        if self.rooms_available <= 0:
            return 0.0
        return self.rooms_sold / self.rooms_available


def load(path: str | Path) -> dict[date, DayActual]:
    """date,rooms_sold,rooms_available の CSV を読む。"""
    path = Path(path)
    if not path.exists():
        return {}
    out: dict[date, DayActual] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"date", "rooms_sold", "rooms_available"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ActualsError(f"{path}: 列が足りない {sorted(missing)}")
        for lineno, row in enumerate(reader, 2):
            try:
                day = date.fromisoformat(row["date"].strip())
                sold = int(row["rooms_sold"])
                available = int(row["rooms_available"])
            except (ValueError, AttributeError) as exc:
                raise ActualsError(f"{path}:{lineno} を読めない: {exc}") from exc
            if sold > available:
                raise ActualsError(
                    f"{path}:{lineno} rooms_sold({sold}) > rooms_available({available})"
                )
            out[day] = DayActual(day=day, rooms_sold=sold, rooms_available=available)
    return out


def sold_between(actuals: dict[date, DayActual], start: date, end: date) -> int:
    total = 0
    day = start
    while day <= end:
        actual = actuals.get(day)
        if actual is not None:
            total += actual.rooms_sold
        day += timedelta(days=1)
    return total


def occupancy_between(
    actuals: dict[date, DayActual], start: date, end: date
) -> float | None:
    """[start, end] の実績稼働率。実績が 1 日も無ければ None。"""
    sold = 0
    available = 0
    day = start
    while day <= end:
        actual = actuals.get(day)
        if actual is not None:
            sold += actual.rooms_sold
            available += actual.rooms_available
        day += timedelta(days=1)
    if available == 0:
        return None
    return sold / available
