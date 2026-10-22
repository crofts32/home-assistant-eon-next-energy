"""Tests for pricing consumption with E.ON's published rates."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import unittest

from test_api import FakeResponse, FakeSession, api

calculations = __import__("sys").modules["eon_next_energy.calculations"]

FALLBACK = calculations.Tariff(*map(Decimal, ["0.069", "0.3367", "0.60", "0.0812", "0.2916"]))
JAN = datetime(2026, 1, 10, tzinfo=UTC)


def agreement(**kwargs):
    defaults = dict(valid_from=JAN, valid_to=JAN + timedelta(days=10), standing_charge=Decimal("0.55"))
    return calculations.AgreementRates(**{**defaults, **kwargs})


class LiveTariffTests(unittest.TestCase):
    def test_flat_rate_and_standing_charge_apply_inside_agreement_only(self):
        tariff = calculations.LiveTariff(FALLBACK, electricity=(agreement(flat_rate=Decimal("0.25")),))
        inside = JAN + timedelta(days=1, hours=12)
        self.assertEqual(tariff.electricity_rate(inside), Decimal("0.25"))
        self.assertEqual(tariff.electricity_standing(inside), Decimal("0.55"))
        before = JAN - timedelta(hours=12)
        self.assertEqual(tariff.electricity_rate(before), FALLBACK.electricity_peak_rate)
        self.assertEqual(tariff.electricity_standing(before), FALLBACK.electricity_standing_charge)

    def test_day_night_maps_onto_london_night_window(self):
        tariff = calculations.LiveTariff(
            FALLBACK,
            electricity=(agreement(day_rate=Decimal("0.30"), night_rate=Decimal("0.10")),),
        )
        self.assertEqual(tariff.electricity_rate(JAN + timedelta(days=1, hours=3)), Decimal("0.10"))
        self.assertEqual(tariff.electricity_rate(JAN + timedelta(days=1, hours=8)), Decimal("0.30"))

    def test_half_hourly_windows_repeat_as_a_daily_pattern(self):
        day = JAN + timedelta(days=5)
        windows = (
            calculations.RateWindow(day, day + timedelta(hours=7), Decimal("0.07")),
            calculations.RateWindow(day + timedelta(hours=7), day + timedelta(hours=24), Decimal("0.33")),
        )
        rates = agreement(windows=windows, slot_rates=calculations.daily_slot_rates(windows))
        tariff = calculations.LiveTariff(FALLBACK, electricity=(rates,))
        self.assertEqual(tariff.electricity_rate(day + timedelta(hours=1)), Decimal("0.07"))
        earlier = JAN + timedelta(days=1)
        self.assertEqual(tariff.electricity_rate(earlier + timedelta(hours=6, minutes=30)), Decimal("0.07"))
        self.assertEqual(tariff.electricity_rate(earlier + timedelta(hours=7)), Decimal("0.33"))

    def test_variable_half_hourly_prices_are_not_extrapolated(self):
        day = JAN + timedelta(days=5)
        windows = (
            calculations.RateWindow(day, day + timedelta(minutes=30), Decimal("0.10")),
            calculations.RateWindow(day + timedelta(days=1), day + timedelta(days=1, minutes=30), Decimal("0.20")),
        )
        rates = agreement(windows=windows, slot_rates=calculations.daily_slot_rates(windows))
        tariff = calculations.LiveTariff(FALLBACK, electricity=(rates,))
        self.assertEqual(tariff.electricity_rate(day), Decimal("0.10"))
        self.assertEqual(tariff.electricity_rate(day + timedelta(days=2)), FALLBACK.electricity_offpeak_rate)

    def test_unsupported_tariff_falls_back_entirely(self):
        tariff = calculations.LiveTariff(FALLBACK, electricity=(agreement(standing_charge=None),))
        moment = JAN + timedelta(days=1, hours=12)
        self.assertEqual(tariff.electricity_rate(moment), FALLBACK.electricity_peak_rate)
        self.assertEqual(tariff.electricity_standing(moment), FALLBACK.electricity_standing_charge)

    def test_gas_costs_use_live_rates(self):
        tariff = calculations.LiveTariff(FALLBACK, gas=(agreement(flat_rate=Decimal("0.06")),))
        start = JAN + timedelta(days=1)
        rows = calculations.aggregate_complete_hours(
            [
                type("I", (), dict(start=start, end=start + timedelta(minutes=30), value=Decimal("2")))(),
                type("I", (), dict(start=start + timedelta(minutes=30), end=start + timedelta(hours=1), value=Decimal("1")))(),
            ],
            "gas",
            tariff,
        )
        self.assertEqual(rows[0].cost_gbp, Decimal("3") * Decimal("0.06") + Decimal("0.55"))


def tariffs_response():
    return {
        "data": {
            "account": {
                "electricityAgreements": [
                    {
                        "validFrom": "2026-01-01T00:00:00+00:00",
                        "validTo": None,
                        "isRevoked": False,
                        "meterPoint": {"meters": [{"id": "E"}]},
                        "tariff": {
                            "__typename": "HalfHourlyTariff",
                            "standingCharge": 60.5,
                            "isExport": False,
                            "unitRates": [
                                {"validFrom": "2026-01-10T00:00:00+00:00", "validTo": "2026-01-10T07:00:00+00:00", "value": 6.9, "rateType": "STANDARD"},
                                {"validFrom": "2026-01-10T00:00:00+00:00", "validTo": "2026-01-11T00:00:00+00:00", "value": 1.0, "rateType": "EV_DEVICE"},
                                {"validFrom": "2026-01-10T07:00:00+00:00", "validTo": "2026-01-11T00:00:00+00:00", "value": 33.67, "rateType": None},
                            ],
                        },
                    },
                    {
                        "validFrom": "2025-01-01T00:00:00+00:00",
                        "validTo": "2026-01-01T00:00:00+00:00",
                        "isRevoked": True,
                        "meterPoint": {"meters": [{"id": "E"}]},
                        "tariff": {"__typename": "StandardTariff", "standingCharge": 1, "unitRate": 1},
                    },
                    {
                        "validFrom": "2026-01-01T00:00:00+00:00",
                        "validTo": None,
                        "meterPoint": {"meters": [{"id": "X"}]},
                        "tariff": {"__typename": "StandardTariff", "standingCharge": 0, "unitRate": 15, "isExport": True},
                    },
                ],
                "gasAgreements": [
                    {
                        "validFrom": "2026-01-01T00:00:00+00:00",
                        "validTo": None,
                        "meterPoint": {"meters": [{"id": "G"}]},
                        "tariff": {"standingCharge": 29.16, "unitRate": "bad"},
                    }
                ],
            }
        }
    }


class TariffParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_published_rates_are_converted_and_filtered(self):
        client = api.EonNextClient(FakeSession([FakeResponse(200, tariffs_response())]), "sk_live_x")
        client._access_token = "access"
        rates = await client.async_get_tariffs("A")
        self.assertEqual(set(rates), {("electricity", "E"), ("gas", "G")})
        (electricity,) = rates[("electricity", "E")]
        self.assertEqual(electricity.standing_charge, Decimal("0.605"))
        self.assertEqual([w.rate for w in electricity.windows], [Decimal("0.069"), Decimal("0.3367")])
        (gas,) = rates[("gas", "G")]
        self.assertIsNone(gas.flat_rate)
        self.assertEqual(gas.standing_charge, Decimal("0.2916"))

    async def test_unsupported_tariff_type_is_logged_and_kept_as_fallback(self):
        body = {"data": {"account": {"electricityAgreements": [{
            "validFrom": "2026-01-01T00:00:00+00:00",
            "meterPoint": {"meters": [{"id": "E"}]},
            "tariff": {"__typename": "FourRateEvTariff"},
        }], "gasAgreements": []}}}
        client = api.EonNextClient(FakeSession([FakeResponse(200, body)]), "sk_live_x")
        client._access_token = "access"
        with self.assertLogs("eon_next_energy.api", level="WARNING") as logs:
            rates = await client.async_get_tariffs("A")
        self.assertIn("FourRateEvTariff", logs.output[0])
        self.assertIsNone(rates[("electricity", "E")][0].standing_charge)


if __name__ == "__main__":
    unittest.main()
