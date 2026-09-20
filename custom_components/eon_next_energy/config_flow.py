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
)
from .const import (
    CONF_ELECTRICITY_OFFPEAK_RATE,
    CONF_ELECTRICITY_PEAK_RATE,
    CONF_ELECTRICITY_STANDING_CHARGE,
    CONF_EMAIL,
    CONF_GAS_CALORIFIC_VALUE,
    DEFAULT_GAS_CALORIFIC_VALUE,
    CONF_GAS_RATE,
    CONF_GAS_STANDING_CHARGE,
    CONF_HISTORY_DAYS,
    CONF_REFRESH_TOKEN,
    DEFAULT_ELECTRICITY_OFFPEAK_RATE,
    DEFAULT_ELECTRICITY_PEAK_RATE,
    DEFAULT_ELECTRICITY_STANDING_CHARGE,
    DEFAULT_GAS_RATE,
    DEFAULT_GAS_STANDING_CHARGE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
)

CONF_PASSWORD = "password"


class EonNextLoginResponseError(EonNextApiError):
    """E.ON rejected login without a recognized credential error."""


class EonNextMeterDiscoveryError(EonNextApiError):
    """Login succeeded but account or meter discovery failed."""


class EonNextNoMetersError(EonNextApiError):
    """Login succeeded but no active meters were returned."""


def _password_selector() -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
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


async def _authenticate(hass, email: str, password: str) -> str:
    """Authenticate and return only a refresh token."""
    client = EonNextClient(async_get_clientsession(hass))
    try:
        tokens = await client.async_login(email, password)
    except EonNextApiError as err:
        raise EonNextLoginResponseError from err
    try:
        meters = await client.async_get_meters()
    except EonNextApiError as err:
        raise EonNextMeterDiscoveryError from err
    if not meters:
        raise EonNextNoMetersError
    return tokens.refresh_token


class EonNextEnergyConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure E.ON Next Energy Data."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and exchange them for a refresh token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            password = user_input[CONF_PASSWORD]
            try:
                refresh_token = await _authenticate(self.hass, email, password)
            except EonNextAuthenticationError:
                errors["base"] = "invalid_auth"
            except EonNextConnectionError:
                errors["base"] = "cannot_connect"
            except EonNextLoginResponseError:
                errors["base"] = "login_response"
            except EonNextMeterDiscoveryError:
                errors["base"] = "meter_discovery"
            except EonNextNoMetersError:
                errors["base"] = "no_meters"
            except EonNextApiError:
                errors["base"] = "unknown"
            else:
                password = ""
                await self.async_set_unique_id(sha256(email.encode()).hexdigest())
                self._abort_if_unique_id_configured()
                self._credentials = {
                    CONF_EMAIL: email,
                    CONF_REFRESH_TOKEN: refresh_token,
                }
                return await self.async_step_tariff()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.EMAIL
                        )
                    ),
                    vol.Required(CONF_PASSWORD): _password_selector(),
                }
            ),
            errors=errors,
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
        """Start reauthentication after token expiry or password change."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Exchange a new password for a fresh refresh token."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            try:
                refresh_token = await _authenticate(
                    self.hass, entry.data[CONF_EMAIL], password
                )
            except EonNextAuthenticationError:
                errors["base"] = "invalid_auth"
            except EonNextConnectionError:
                errors["base"] = "cannot_connect"
            except EonNextLoginResponseError:
                errors["base"] = "login_response"
            except EonNextMeterDiscoveryError:
                errors["base"] = "meter_discovery"
            except EonNextNoMetersError:
                errors["base"] = "no_meters"
            except EonNextApiError:
                errors["base"] = "unknown"
            else:
                password = ""
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_REFRESH_TOKEN: refresh_token},
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_PASSWORD): _password_selector()}
            ),
            errors=errors,
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
