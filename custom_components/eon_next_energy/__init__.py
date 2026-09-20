"""E.ON Next Energy Data integration."""

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import EonConfigEntry, EonNextCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EonConfigEntry) -> bool:
    """Set up E.ON Next Energy Data from a config entry."""
    if not hass.data.get("eon_next_energy_static_registered"):
        await hass.http.async_register_static_paths([
            StaticPathConfig("/eon-next-energy", str(Path(__file__).parent / "www"), False)
        ])
        hass.data["eon_next_energy_static_registered"] = True
    coordinator = EonNextCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EonConfigEntry) -> bool:
    """Unload the integration."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
