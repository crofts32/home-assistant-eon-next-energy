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

    def electricity_rate(self, timestamp: datetime) -> Decimal:
        local = timestamp.astimezone(LONDON)
        if 0 <= local.hour < 7:
            return self.electricity_offpeak_rate
        return self.electricity_peak_rate

    def electricity_standing(self, _timestamp: datetime) -> Decimal:
        return self.electricity_standing_charge

    def gas_unit_rate(self, _timestamp: datetime) -> Decimal:
        return self.gas_rate

    def gas_standing(self, _timestamp: datetime) -> Decimal:
        return self.gas_standing_charge


@dataclass(frozen=True, slots=True)
class RateWindow:
    """A VAT-inclusive unit rate in pounds for a fixed period."""

    start: datetime
    end: datetime | None
    rate: Decimal


@dataclass(frozen=True, slots=True)
class AgreementRates:
    """VAT-inclusive rates in pounds published by E.ON for one agreement."""

    valid_from: datetime
    valid_to: datetime | None
    standing_charge: Decimal | None
    flat_rate: Decimal | None = None
    day_rate: Decimal | None = None
    night_rate: Decimal | None = None
    windows: tuple[RateWindow, ...] = ()
    slot_rates: tuple[tuple[tuple[int, int], Decimal], ...] = ()

    def covers(self, timestamp: datetime) -> bool:
        return self.valid_from <= timestamp and (
            self.valid_to is None or timestamp < self.valid_to
        )

    def unit_rate(self, timestamp: datetime) -> Decimal | None:
        for window in self.windows:
            if window.start <= timestamp and (window.end is None or timestamp < window.end):
                return window.rate
        if self.slot_rates:
            local = timestamp.astimezone(LONDON)
            slot = (local.hour, 30 if local.minute >= 30 else 0)
            return dict(self.slot_rates).get(slot)
        if self.day_rate is not None and self.night_rate is not None:
            local = timestamp.astimezone(LONDON)
            return self.night_rate if 0 <= local.hour < 7 else self.day_rate
        return self.flat_rate


def daily_slot_rates(
    windows: tuple[RateWindow, ...],
) -> tuple[tuple[tuple[int, int], Decimal], ...]:
    """Learn a repeating London half-hour pattern from published rate windows.

    Slots seen with more than one price are left out, so a variable tariff is
    never extrapolated beyond the windows E.ON published.
    """
    seen: dict[tuple[int, int], set[Decimal]] = defaultdict(set)
    for window in windows:
        if window.end is None:
            continue
        cursor = window.start
        for _ in range(48):
            if cursor >= window.end:
                break
            local = cursor.astimezone(LONDON)
            seen[(local.hour, 30 if local.minute >= 30 else 0)].add(window.rate)
            cursor += timedelta(minutes=30)
    return tuple(
        sorted((slot, rates.pop()) for slot, rates in seen.items() if len(rates) == 1)
    )


@dataclass(frozen=True, slots=True)
class LiveTariff:
    """E.ON's published rates, falling back to the configured tariff."""

    fallback: Tariff
    electricity: tuple[AgreementRates, ...] = ()
    gas: tuple[AgreementRates, ...] = ()

    @staticmethod
    def _agreement(
        agreements: tuple[AgreementRates, ...], timestamp: datetime
    ) -> AgreementRates | None:
        return next((item for item in agreements if item.covers(timestamp)), None)

    def electricity_rate(self, timestamp: datetime) -> Decimal:
        agreement = self._agreement(self.electricity, timestamp)
        rate = agreement.unit_rate(timestamp) if agreement else None
        return rate if rate is not None else self.fallback.electricity_rate(timestamp)

    def electricity_standing(self, timestamp: datetime) -> Decimal:
        agreement = self._agreement(self.electricity, timestamp)
        if agreement and agreement.standing_charge is not None:
            return agreement.standing_charge
        return self.fallback.electricity_standing(timestamp)

    def gas_unit_rate(self, timestamp: datetime) -> Decimal:
        agreement = self._agreement(self.gas, timestamp)
        rate = agreement.unit_rate(timestamp) if agreement else None
        return rate if rate is not None else self.fallback.gas_unit_rate(timestamp)

    def gas_standing(self, timestamp: datetime) -> Decimal:
        agreement = self._agreement(self.gas, timestamp)
        if agreement and agreement.standing_charge is not None:
            return agreement.standing_charge
        return self.fallback.gas_standing(timestamp)


class Pricing(Protocol):
    """Rates used to cost consumption."""

    def electricity_rate(self, timestamp: datetime) -> Decimal: ...
    def electricity_standing(self, timestamp: datetime) -> Decimal: ...
    def gas_unit_rate(self, timestamp: datetime) -> Decimal: ...
    def gas_standing(self, timestamp: datetime) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class HourlyUsage:
    """Complete UTC hour of consumption and calculated cost."""

    start: datetime
    consumption: Decimal
    cost_gbp: Decimal | None


def electricity_rate_at(timestamp: datetime, tariff: Pricing) -> Decimal:
    """Return the electricity unit rate for an interval start."""
    return tariff.electricity_rate(timestamp)


def aggregate_complete_hours(
    intervals: list[IntervalLike], fuel: str, tariff: Pricing, unit: str = "kWh"
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
                cost += tariff.electricity_standing(hour)
        else:
            cost = sum(
                (item.value * tariff.gas_unit_rate(item.start) for item in readings),
                Decimal("0"),
            )
            if local_date not in charged_dates:
                cost += tariff.gas_standing(hour)
        charged_dates.add(local_date)
        result.append(HourlyUsage(hour, consumption, cost))
    return result


def gas_kwh_from_volume(volume: Decimal, calorific_value: Decimal) -> Decimal:
    """Estimate gas energy using the UK metric gas-billing formula."""
    if not calorific_value.is_finite() or not Decimal("37") <= calorific_value <= Decimal("43"):
        raise ValueError("Calorific value must be between 37 and 43 MJ/m³")
    return volume * Decimal("1.02264") * calorific_value / Decimal("3.6")
