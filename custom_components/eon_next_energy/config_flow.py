"""Config flow for E.ON Next Energy Data."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    EonNextApiError,
    EonNextAuthenticationError,
    EonNextClient,
    EonNextConnectionError,
    EonNextError,
)
from .const import (
    CONF_ELECTRICITY_OFFPEAK_RATE,
    CONF_ELECTRICITY_PEAK_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_API_KEY,
    CONF_GAS_CALORIFIC_VALUE,
    DEFAULT_GAS_CALORIFIC_VALUE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_HISTORY_DAYS,
    LEGACY_CONF_KEYS,
    DEFAULT_ELECTRICITY_OFFPEAK_RATE,
    DEFAULT_ELECTRICITY_PEAK_RATE,
    DEFAULT_ELECTRICITY_STANDING_CHARGE,
    DEFAULT_GAS_RATE,
    DEFAULT_GAS_STANDING_CHARGE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
)

class EonNextLoginResponseError(EonNextApiError):
    """E.ON rejected login without a recognized credential error."""


class EonNextMeterDiscoveryError(EonNextApiError):
    """Login succeeded but account or meter discovery failed."""


class EonNextNoMetersError(EonNextApiError):
    """Login succeeded but no active meters were returned."""


def _api_key_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )
        }
    )


def _settings_schema(
    defaults: dict[str, Any], *, include_history: bool
) -> vol.Schema:
    non_negative = vol.All(vol.Coerce(float), vol.Range(min=0))
    fields: dict[vol.Marker, Any] = {}
    if include_history:
        fields[
            vol.Required(
                CONF_HISTORY_DAYS,
                default=defaults.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS),
            )
        ] = vol.All(vol.Coerce(int), vol.Range(min=7, max=365))
    fields[vol.Required(
        CONF_GAS_CALORIFIC_VALUE,
        default=defaults.get(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE),
    )] = vol.All(vol.Coerce(float), vol.Range(min=0, max=43))
    fields.update(
        {
            vol.Required(
                CONF_ELECTRICITY_OFFPEAK_RATE,
                default=defaults.get(
                    CONF_ELECTRICITY_OFFPEAK_RATE,
                    DEFAULT_ELECTRICITY_OFFPEAK_RATE,
                ),
            ): non_negative,
            vol.Required(
                CONF_ELECTRICITY_PEAK_RATE,
                default=defaults.get(
                    CONF_ELECTRICITY_PEAK_RATE, DEFAULT_ELECTRICITY_PEAK_RATE
                ),
            ): non_negative,
            vol.Required(
                CONF_ELECTRICITY_STANDING_CHARGE,
                default=defaults.get(
                    CONF_ELECTRICITY_STANDING_CHARGE,
                    DEFAULT_ELECTRICITY_STANDING_CHARGE,
                ),
            ): non_negative,
            vol.Required(
                CONF_GAS_RATE,
                default=defaults.get(CONF_GAS_RATE, DEFAULT_GAS_RATE),
            ): non_negative,
            vol.Required(
                CONF_GAS_STANDING_CHARGE,
                default=defaults.get(
                    CONF_GAS_STANDING_CHARGE, DEFAULT_GAS_STANDING_CHARGE
                ),
            ): non_negative,
        }
    )
    return vol.Schema(fields)


def _valid_calorific_value(settings: dict[str, Any]) -> bool:
    value = settings.get(CONF_GAS_CALORIFIC_VALUE, DEFAULT_GAS_CALORIFIC_VALUE)
    return value == 0 or 37 <= value <= 43


async def _authenticate(hass, api_key: str) -> str:
    """Validate the API key and return a stable, non-reversible account ID."""
    client = EonNextClient(async_get_clientsession(hass), api_key)
    try:
        await client.async_authenticate()
    except EonNextApiError as err:
        raise EonNextLoginResponseError from err
    try:
        meters = await client.async_get_meters()
    except EonNextApiError as err:
        raise EonNextMeterDiscoveryError from err
    if not meters:
        raise EonNextNoMetersError
    if not client.viewer_email:
        raise EonNextMeterDiscoveryError
    return sha256(client.viewer_email.encode()).hexdigest()


def _auth_error(err: Exception) -> str:
    if isinstance(err, EonNextAuthenticationError):
        return "invalid_auth"
    if isinstance(err, EonNextConnectionError):
        return "cannot_connect"
    if isinstance(err, EonNextLoginResponseError):
        return "login_response"
    if isinstance(err, EonNextMeterDiscoveryError):
        return "meter_discovery"
    if isinstance(err, EonNextNoMetersError):
        return "no_meters"
    return "unknown"


def _without_legacy_keys(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if key not in LEGACY_CONF_KEYS}


class EonNextEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure E.ON Next Energy Data."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the account API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                unique_id = await _authenticate(self.hass, api_key)
            except EonNextError as err:
                errors["base"] = _auth_error(err)
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                self._credentials = {CONF_API_KEY: api_key}
                return await self.async_step_tariff()

        return self.async_show_form(
            step_id="user", data_schema=_api_key_schema(), errors=errors
        )

    async def async_step_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect tariff values used for cost statistics."""
        errors = {}
        if user_input is not None and not _valid_calorific_value(user_input):
            errors["base"] = "invalid_calorific_value"
        elif user_input is not None:
            return self.async_create_entry(
                title="E.ON Next Energy Data",
                data={**self._credentials, **user_input},
            )
        return self.async_show_form(
            step_id="tariff",
            data_schema=_settings_schema(user_input or {}, include_history=True),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the API key is rejected or missing."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the stored API key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            try:
                unique_id = await _authenticate(self.hass, api_key)
            except EonNextError as err:
                errors["base"] = _auth_error(err)
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data={**_without_legacy_keys(dict(entry.data)), CONF_API_KEY: api_key},
                )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=_api_key_schema(), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the tariff options flow."""
        return EonNextEnergyOptionsFlow()


class EonNextEnergyOptionsFlow(config_entries.OptionsFlowWithReload):
    """Update tariff settings and reload the integration."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage tariff settings."""
        errors = {}
        if user_input is not None and not _valid_calorific_value(user_input):
            errors["base"] = "invalid_calorific_value"
        elif user_input is not None:
            return self.async_create_entry(data=user_input)
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(user_input or defaults, include_history=False),
            errors=errors,
        )
