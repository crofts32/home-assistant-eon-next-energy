"""Coordinator and external statistics importer for E.ON Next Energy Data."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

from .api import (
    EonMeter,
    EonNextApiError,
    EonNextAuthenticationError,
    EonNextClient,
    EonNextConnectionError,
)
from .calculations import LONDON, Tariff, aggregate_complete_hours, gas_kwh_from_volume
from .const import (
    CONF_ELECTRICITY_OFFPEAK_RATE,
    CONF_ELECTRICITY_PEAK_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_GAS_CALORIFIC_VALUE,
    DEFAULT_GAS_CALORIFIC_VALUE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_HISTORY_DAYS,
    CONF_REFRESH_TOKEN,
    CORRECTION_LOOKBACK_DAYS,
    DEFAULT_ELECTRICITY_OFFPEAK_RATE,
    DEFAULT_ELECTRICITY_PEAK_RATE,
    DEFAULT_ELECTRICITY_STANDING_CHARGE,
    DEFAULT_GAS_RATE,
    DEFAULT_GAS_STANDING_CHARGE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MeterImportResult:
    """Non-sensitive status for one imported meter."""

    fuel: str
    ordinal: int
    last_interval_end: datetime | None
    imported_hours: int
    consumption_statistic_id: str
    cost_statistic_id: str | None


@dataclass(slots=True)
class EonCoordinatorData:
    """Coordinator result exposed to diagnostic sensor entities."""

    last_successful_update: datetime
    meters: list[MeterImportResult] = field(default_factory=list)


EonConfigEntry = ConfigEntry["EonNextCoordinator"]


def _meter_key(meter: EonMeter) -> str:
    """Create a stable identifier without exposing supplier identifiers."""
    raw = f"{meter.fuel}:{meter.account_number}:{meter.meter_id}".encode()
    return sha256(raw).hexdigest()[:16]


def _statistic_ids(meter: EonMeter) -> tuple[str, str]:
    key = _meter_key(meter)
    prefix = f"{DOMAIN}:{meter.fuel}_{key}"
    if meter.fuel == "gas" and meter.unit in {"m3", "m³"}:
        return f"{prefix}_volume", f"{prefix}_cost"
    return f"{prefix}_consumption", f"{prefix}_cost"


class EonNextCoordinator(DataUpdateCoordinator[EonCoordinatorData]):
    """Fetch delayed E.ON data and insert recorder statistics."""

    config_entry: EonConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EonConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="E.ON Next Energy Data",
            update_interval=UPDATE_INTERVAL,
        )
        self.client = EonNextClient(
            async_get_clientsession(hass), entry.data[CONF_REFRESH_TOKEN]
        )

    def _setting(self, key: str, default: float | int) -> float | int:
        return self.config_entry.options.get(
            key, self.config_entry.data.get(key, default)
        )

    def _tariff(self) -> Tariff:
        return Tariff(
            electricity_offpeak_rate=Decimal(
                str(self._setting(CONF_ELECTRICITY_OFFPEAK_RATE, DEFAULT_ELECTRICITY_OFFPEAK_RATE))
            ),
            electricity_peak_rate=Decimal(
                str(self._setting(CONF_ELECTRICITY_PEAK_RATE, DEFAULT_ELECTRICITY_PEAK_RATE))
            ),
            electricity_standing_charge=Decimal(
                str(self._setting(CONF_ELECTRICITY_STANDING_CHARGE, DEFAULT_ELECTRICITY_STANDING_CHARGE))
            ),
            gas_rate=Decimal(str(self._setting(CONF_GAS_RATE, DEFAULT_GAS_RATE))),
            gas_standing_charge=Decimal(
                str(self._setting(CONF_GAS_STANDING_CHARGE, DEFAULT_GAS_STANDING_CHARGE))
            ),
        )

    async def _last_statistic(
        self, statistic_id: str
    ) -> tuple[datetime | None, float]:
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )
        records = result.get(statistic_id, [])
        if not records:
            return None, 0.0
        record = records[0]
        return datetime.fromtimestamp(record["start"], UTC), float(record.get("sum") or 0)

    async def _sum_before(
        self,
        statistic_id: str,
        before: datetime,
        last: tuple[datetime | None, float],
    ) -> float:
        """Return the cumulative sum immediately before a rewrite window."""
        last_start, last_sum = last
        if last_start is None:
            return 0.0
        if last_start < before:
            return last_sum

        for days in (31, 366, 3650, 36500):
            start = before - timedelta(days=days)
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                before,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
            records = result.get(statistic_id, [])
            if records:
                return float(records[-1].get("sum") or 0)
        return 0.0

    async def _import_meter(
        self, meter: EonMeter, ordinal: int, tariff: Tariff
    ) -> MeterImportResult:
        calorific_value = Decimal(str(self._setting(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE)))
        estimated = meter.fuel == "gas" and meter.unit in {"m3", "m³"} and calorific_value != 0
        consumption_id, cost_id = _statistic_ids(meter)
        if estimated:
            prefix = f"{DOMAIN}:gas_{_meter_key(meter)}_estimated"
            consumption_id, cost_id = f"{prefix}_consumption", f"{prefix}_cost"
        consumption_last = await self._last_statistic(consumption_id)
        volume = meter.fuel == "gas" and meter.unit in {"m3", "m³"} and not estimated
        cost_last = (None, 0.0) if volume else await self._last_statistic(cost_id)
        required = (consumption_last,) if volume else (consumption_last, cost_last)
        starts = [item[0] for item in required if item[0]]

        if estimated or len(starts) < len(required):
            history_days = int(
                self._setting(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
            )
            first_local_date = datetime.now(LONDON).date() - timedelta(
                days=history_days
            )
        else:
            first_local_date = min(starts).astimezone(LONDON).date() - timedelta(
                days=CORRECTION_LOOKBACK_DAYS
            )
        start = datetime.combine(first_local_date, time.min, LONDON).astimezone(UTC)

        consumption_sum = await self._sum_before(
            consumption_id, start, consumption_last
        )
        cost_sum = 0.0 if volume else await self._sum_before(cost_id, start, cost_last)

        intervals = await self.client.async_get_consumption(meter, start)
        if estimated:
            intervals = [replace(item, value=gas_kwh_from_volume(item.value, calorific_value)) for item in intervals]
        hourly = aggregate_complete_hours(intervals, meter.fuel, tariff, "kWh" if estimated else meter.unit)

        consumption_stats: list[StatisticData] = []
        cost_stats: list[StatisticData] = []
        for item in hourly:
            usage = float(item.consumption)
            consumption_sum += usage
            consumption_stats.append(
                StatisticData(start=item.start, state=usage, sum=consumption_sum)
            )
            if item.cost_gbp is not None:
                cost = float(item.cost_gbp)
                cost_sum += cost
                cost_stats.append(
                    StatisticData(start=item.start, state=cost, sum=cost_sum)
                )

        name_prefix = f"E.ON Next {meter.fuel.title()} {ordinal}" + (" Estimated" if estimated else "")
        consumption_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} Consumption",
            source=DOMAIN,
            statistic_id=consumption_id,
            unit_class=VolumeConverter.UNIT_CLASS if volume else EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS if volume else UnitOfEnergy.KILO_WATT_HOUR,
        )
        cost_metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"{name_prefix} Cost",
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement="GBP",
        )
        if consumption_stats:
            async_add_external_statistics(
                self.hass, consumption_metadata, consumption_stats
            )
            if cost_stats:
                async_add_external_statistics(self.hass, cost_metadata, cost_stats)

        last_end = max((item.end for item in intervals), default=None)
        return MeterImportResult(
            fuel=meter.fuel,
            ordinal=ordinal,
            last_interval_end=last_end,
            imported_hours=len(hourly),
            consumption_statistic_id=consumption_id,
            cost_statistic_id=None if volume else cost_id,
        )

    async def _async_update_data(self) -> EonCoordinatorData:
        try:
            tokens = await self.client.async_refresh_access_token()
            if tokens.refresh_token != self.config_entry.data[CONF_REFRESH_TOKEN]:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_REFRESH_TOKEN: tokens.refresh_token,
                    },
                )
            meters = await self.client.async_get_meters()
            if not meters:
                raise EonNextApiError("No active meters were returned")

            tariff = self._tariff()
            ordinals: dict[str, int] = {"electricity": 0, "gas": 0}
            results: list[MeterImportResult] = []
            for meter in sorted(
                meters, key=lambda item: (item.fuel, item.account_number, item.meter_id)
            ):
                ordinals[meter.fuel] += 1
                results.append(
                    await self._import_meter(meter, ordinals[meter.fuel], tariff)
                )
            return EonCoordinatorData(datetime.now(UTC), results)
        except EonNextAuthenticationError as err:
            raise ConfigEntryAuthFailed("E.ON credentials must be renewed") from err
        except (EonNextConnectionError, EonNextApiError) as err:
            raise UpdateFailed(str(err)) from err
