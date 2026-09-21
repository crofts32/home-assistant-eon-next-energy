"""Diagnostic sensors for E.ON Next Energy Data."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ELECTRICITY_OFFPEAK_RATE,
    CONF_ELECTRICITY_PEAK_RATE,
    CONF_GAS_CALORIFIC_VALUE,
    DEFAULT_ELECTRICITY_OFFPEAK_RATE,
    DEFAULT_ELECTRICITY_PEAK_RATE,
    DEFAULT_GAS_CALORIFIC_VALUE,
    DOMAIN,
)
from .coordinator import EonCoordinatorData, EonNextCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry[EonNextCoordinator],
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one redacted freshness sensor per fuel."""
    fuels = sorted({item.fuel for item in entry.runtime_data.data.meters})
    async_add_entities(
        EonDataFreshnessSensor(entry.runtime_data, entry.entry_id, fuel)
        for fuel in fuels
    )


class EonDataFreshnessSensor(CoordinatorEntity[EonNextCoordinator], SensorEntity):
    """Latest interval timestamp without supplier identifiers."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: EonNextCoordinator, entry_id: str, fuel: str
    ) -> None:
        super().__init__(coordinator)
        self._fuel = fuel
        self._attr_name = f"Latest {fuel} data"
        self._attr_unique_id = f"{entry_id}_{fuel}_latest_data"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            manufacturer="E.ON Next",
            name="E.ON Next Energy Data",
            model="Cloud smart-meter data",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the latest end timestamp across this fuel's meters."""
        values = [
            item.last_interval_end
            for item in self.coordinator.data.meters
            if item.fuel == self._fuel and item.last_interval_end is not None
        ]
        return max(values, default=None)

    @property
    def extra_state_attributes(self) -> dict[str, int | str]:
        """Expose counts and statistic IDs, with supplier identifiers omitted."""
        meters = [item for item in self.coordinator.data.meters if item.fuel == self._fuel]
        return {
            "gas_calorific_value": self.coordinator._setting(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE),
            "electricity_offpeak_rate": self.coordinator._setting(
                CONF_ELECTRICITY_OFFPEAK_RATE, DEFAULT_ELECTRICITY_OFFPEAK_RATE
            ),
            "electricity_peak_rate": self.coordinator._setting(
                CONF_ELECTRICITY_PEAK_RATE, DEFAULT_ELECTRICITY_PEAK_RATE
            ),
            "meter_count": len(meters),
            "hours_imported_last_update": sum(item.imported_hours for item in meters),
            "consumption_statistics": ", ".join(
                item.consumption_statistic_id for item in meters
            ),
            "cost_statistics": ", ".join(item.cost_statistic_id for item in meters if item.cost_statistic_id),
        }
