"""Tests for tariff calculations and hourly aggregation."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import importlib.util
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "eon_next_energy"
    / "calculations.py"
)
SPEC = importlib.util.spec_from_file_location("eon_calculations", MODULE_PATH)
assert SPEC and SPEC.loader
calculations = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = calculations
SPEC.loader.exec_module(calculations)

LONDON = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    value_kwh: Decimal


class TariffCalculationTests(unittest.TestCase):
    def setUp(self):
        self.tariff = calculations.Tariff(
            electricity_offpeak_rate=Decimal("0.069"),
            electricity_peak_rate=Decimal("0.3367"),
            electricity_standing_charge=Decimal("0.60"),
            gas_rate=Decimal("0.0812"),
            gas_standing_charge=Decimal("0.2916"),
        )

    def _hour(self, start: datetime, first: str = "1", second: str = "1"):
        return [
            Interval(start, start + timedelta(minutes=30), Decimal(first)),
            Interval(
                start + timedelta(minutes=30),
                start + timedelta(hours=1),
                Decimal(second),
            ),
        ]

    def test_electricity_midnight_adds_standing_charge_and_offpeak_rate(self):
        rows = calculations.aggregate_complete_hours(
            self._hour(datetime(2026, 9, 18, 0, tzinfo=LONDON)),
            "electricity",
            self.tariff,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].consumption_kwh, Decimal("2"))
        self.assertEqual(rows[0].cost_gbp, Decimal("0.738"))

    def test_electricity_seven_am_uses_peak_rate(self):
        intervals = self._hour(datetime(2026, 9, 18, 0, tzinfo=LONDON))
        intervals += self._hour(datetime(2026, 9, 18, 7, tzinfo=LONDON))
        rows = calculations.aggregate_complete_hours(
            intervals,
            "electricity",
            self.tariff,
        )
        self.assertEqual(rows[1].cost_gbp, Decimal("0.6734"))

    def test_missing_midnight_charges_first_complete_hour(self):
        start = datetime(2026, 9, 18, 0, tzinfo=LONDON)
        intervals = [
            Interval(start, start + timedelta(minutes=30), Decimal("1")),
            *self._hour(start + timedelta(hours=1)),
        ]
        rows = calculations.aggregate_complete_hours(
            intervals, "electricity", self.tariff
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].start.astimezone(LONDON).hour, 1)
        self.assertEqual(rows[0].cost_gbp, Decimal("0.738"))

    def test_gas_midnight_adds_standing_charge(self):
        rows = calculations.aggregate_complete_hours(
            self._hour(datetime(2026, 9, 18, 0, tzinfo=LONDON), "0.5", "0.5"),
            "gas",
            self.tariff,
        )
        self.assertEqual(rows[0].cost_gbp, Decimal("0.3728"))

    def test_incomplete_hour_is_not_imported(self):
        start = datetime(2026, 9, 18, 12, tzinfo=LONDON)
        rows = calculations.aggregate_complete_hours(
            [Interval(start, start + timedelta(minutes=30), Decimal("1"))],
            "electricity",
            self.tariff,
        )
        self.assertEqual(rows, [])

    def test_spring_dst_day_has_23_distinct_utc_hours(self):
        intervals = []
        start = datetime(2026, 3, 29, 0, tzinfo=LONDON)
        current = start
        while current.astimezone(LONDON).date() == start.date():
            end = (current.astimezone(ZoneInfo("UTC")) + timedelta(minutes=30)).astimezone(LONDON)
            intervals.append(
                Interval(current, end, Decimal("0.1"))
            )
            current = end
        rows = calculations.aggregate_complete_hours(
            intervals, "electricity", self.tariff
        )
        self.assertEqual(len(rows), 23)

    def test_autumn_dst_day_has_25_distinct_utc_hours(self):
        intervals = []
        start = datetime(2026, 10, 25, 0, tzinfo=LONDON)
        current = start
        while current.astimezone(LONDON).date() == start.date():
            end = (current.astimezone(ZoneInfo("UTC")) + timedelta(minutes=30)).astimezone(LONDON)
            intervals.append(
                Interval(current, end, Decimal("0.1"))
            )
            current = end
        rows = calculations.aggregate_complete_hours(
            intervals, "electricity", self.tariff
        )
        self.assertEqual(len(rows), 25)
        midnight_hours = [row for row in rows if row.start.astimezone(LONDON).hour == 0]
        self.assertEqual(len(midnight_hours), 1)


if __name__ == "__main__":
    unittest.main()
