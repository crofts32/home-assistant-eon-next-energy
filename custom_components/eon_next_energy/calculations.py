"""Pure calculations for tariff cost and hourly statistics."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


class IntervalLike(Protocol):
    """The interval fields used by the pure calculation layer."""

    start: datetime
    end: datetime
    value: Decimal


@dataclass(frozen=True, slots=True)
class Tariff:
    """VAT-inclusive rates in pounds."""

    electricity_offpeak_rate: Decimal
    electricity_peak_rate: Decimal
    electricity_standing_charge: Decimal
    gas_rate: Decimal
    gas_standing_charge: Decimal


@dataclass(frozen=True, slots=True)
class HourlyUsage:
    """Complete UTC hour of consumption and calculated cost."""

    start: datetime
    consumption: Decimal
    cost_gbp: Decimal | None


def electricity_rate_at(timestamp: datetime, tariff: Tariff) -> Decimal:
    """Return the electricity unit rate for an interval start."""
    local = timestamp.astimezone(LONDON)
    if 0 <= local.hour < 7:
        return tariff.electricity_offpeak_rate
    return tariff.electricity_peak_rate


def aggregate_complete_hours(
    intervals: list[IntervalLike], fuel: str, tariff: Tariff, unit: str = "kWh"
) -> list[HourlyUsage]:
    """Combine two 30-minute readings into recorder-compatible UTC hours."""
    volume = fuel == "gas" and unit in {"m3", "m³"}
    if not volume and unit.lower() != "kwh":
        raise ValueError("Unsupported consumption unit")
    grouped: dict[datetime, list[IntervalLike]] = defaultdict(list)
    for item in intervals:
        start_utc = item.start.astimezone(UTC)
        end_utc = item.end.astimezone(UTC)
        if end_utc - start_utc != timedelta(minutes=30):
            continue
        hour = start_utc.replace(minute=0, second=0, microsecond=0)
        grouped[hour].append(item)

    result: list[HourlyUsage] = []
    charged_dates: set[object] = set()
    for hour in sorted(grouped):
        readings = sorted(grouped[hour], key=lambda item: item.start)
        if len(readings) != 2:
            continue
        starts = {item.start.astimezone(UTC).minute for item in readings}
        if starts != {0, 30}:
            continue

        consumption = sum((item.value for item in readings), Decimal("0"))
        local_date = hour.astimezone(LONDON).date()
        if volume:
            cost = None
        elif fuel == "electricity":
            cost = sum(
                (
                    item.value * electricity_rate_at(item.start, tariff)
                    for item in readings
                ),
                Decimal("0"),
            )
            if local_date not in charged_dates:
                cost += tariff.electricity_standing_charge
        else:
            cost = consumption * tariff.gas_rate
            if local_date not in charged_dates:
                cost += tariff.gas_standing_charge
        charged_dates.add(local_date)
        result.append(HourlyUsage(hour, consumption, cost))
    return result


def gas_kwh_from_volume(volume: Decimal, calorific_value: Decimal) -> Decimal:
    """Estimate gas energy using the UK metric gas-billing formula."""
    if not calorific_value.is_finite() or not Decimal("37") <= calorific_value <= Decimal("43"):
        raise ValueError("Calorific value must be between 37 and 43 MJ/m³")
    return volume * Decimal("1.02264") * calorific_value / Decimal("3.6")
