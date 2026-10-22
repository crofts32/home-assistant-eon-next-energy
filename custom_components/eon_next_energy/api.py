"""Minimal read-only client for the private E.ON Next Kraken API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging
from typing import Any

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
)

from .calculations import AgreementRates, RateWindow, daily_slot_rates
from .const import API_URL

_LOGGER = logging.getLogger(__name__)


class EonNextError(Exception):
    """Base E.ON Next client error."""


class EonNextAuthenticationError(EonNextError):
    """Authentication failed or the API key was rejected."""


class EonNextConnectionError(EonNextError):
    """The service could not be reached."""


class EonNextApiError(EonNextError):
    """The API returned an unexpected response."""


@dataclass(frozen=True, slots=True)
class EonMeter:
    """A meter required for consumption queries."""

    account_number: str
    meter_id: str
    fuel: str
    unit: str


@dataclass(frozen=True, slots=True)
class EonInterval:
    """A consumption interval returned by E.ON."""

    start: datetime
    end: datetime
    value: Decimal


LOGIN_MUTATION = """
mutation Login($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    token
  }
}
"""

ACCOUNTS_QUERY = """
query Accounts {
  viewer { email accounts { ... on AccountType { number } } }
}
"""

METERS_QUERY = """
query Meters($accountNumber: String!) {
  properties(accountNumber: $accountNumber) {
    electricityMeterPoints {
      direction
      meters(includeInactive: false) { id consumptionUnits }
    }
    gasMeterPoints {
      meters(includeInactive: false) { id consumptionUnits }
    }
  }
}
"""


TARIFFS_QUERY = """
query Tariffs($accountNumber: String!) {
  account(accountNumber: $accountNumber) {
    electricityAgreements {
      validFrom
      validTo
      isRevoked
      meterPoint { meters(includeInactive: true) { id } }
      tariff {
        __typename
        ... on StandardTariff { standingCharge unitRate isExport }
        ... on PrepayTariff { standingCharge unitRate isExport }
        ... on DayNightTariff { standingCharge dayRate nightRate isExport }
        ... on HalfHourlyTariff {
          standingCharge
          isExport
          unitRates { validFrom validTo value rateType }
        }
      }
    }
    gasAgreements {
      validFrom
      validTo
      isRevoked
      meterPoint { meters(includeInactive: true) { id } }
      tariff { standingCharge unitRate }
    }
  }
}
"""


def _pounds(value: Any) -> Decimal | None:
    """Convert E.ON's VAT-inclusive pence to pounds."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        pence = Decimal(str(value))
    except InvalidOperation:
        return None
    if not pence.is_finite() or pence < 0:
        return None
    return pence / 100


def _optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _parse_datetime(value)
    except (ValueError, EonNextApiError):
        return None


def _agreement_rates(agreement: dict[str, Any], fuel: str) -> AgreementRates | None:
    """Parse one agreement; None when it cannot price consumption."""
    if agreement.get("isRevoked"):
        return None
    valid_from = _optional_datetime(agreement.get("validFrom"))
    tariff = agreement.get("tariff")
    if valid_from is None or not isinstance(tariff, dict) or tariff.get("isExport"):
        return None
    valid_to = _optional_datetime(agreement.get("validTo"))
    standing = _pounds(tariff.get("standingCharge"))
    if fuel == "gas" or tariff.get("__typename") in ("StandardTariff", "PrepayTariff"):
        rates = AgreementRates(valid_from, valid_to, standing, flat_rate=_pounds(tariff.get("unitRate")))
    elif tariff.get("__typename") == "DayNightTariff":
        rates = AgreementRates(
            valid_from,
            valid_to,
            standing,
            day_rate=_pounds(tariff.get("dayRate")),
            night_rate=_pounds(tariff.get("nightRate")),
        )
    elif tariff.get("__typename") == "HalfHourlyTariff":
        windows: list[RateWindow] = []
        for item in tariff.get("unitRates") or []:
            if not isinstance(item, dict) or item.get("rateType") not in (None, "STANDARD"):
                continue
            start = _optional_datetime(item.get("validFrom"))
            rate = _pounds(item.get("value"))
            if start is not None and rate is not None:
                windows.append(RateWindow(start, _optional_datetime(item.get("validTo")), rate))
        ordered = tuple(sorted(windows, key=lambda item: item.start))
        rates = AgreementRates(
            valid_from, valid_to, standing, windows=ordered, slot_rates=daily_slot_rates(ordered)
        )
    else:
        _LOGGER.warning(
            "E.ON tariff type %s is not supported; configured rates are used",
            tariff.get("__typename") or "unknown",
        )
        rates = AgreementRates(valid_from, valid_to, None)
    return rates


def _consumption_query(fuel: str) -> str:
    agreements = (
        "electricityAgreements" if fuel == "electricity" else "gasAgreements"
    )
    return f"""
query Consumption($accountNumber: String!, $startAt: DateTime!, $after: String) {{
  account(accountNumber: $accountNumber) {{
    {agreements}(active: true) {{
      meterPoint {{
        meters(includeInactive: false) {{
          id
          consumptionUnits
          consumption(
            startAt: $startAt
            grouping: HALF_HOUR
            timezone: "Europe/London"
            first: 100
            after: $after
          ) {{
            edges {{ node {{ startAt endAt value }} }}
            pageInfo {{ hasNextPage endCursor }}
          }}
        }}
      }}
    }}
  }}
}}
"""

_AUTH_ERROR_CODES = {
    "KT-CT-1120",  # Token expired.
    "KT-CT-1121",  # Invalid token type.
    "KT-CT-1134",  # Invalid authentication input.
    "KT-CT-1135",  # Invalid authentication input.
    "KT-CT-1138",  # Credentials rejected.
    "KT-CT-1139",  # API key rejected.
    "KT-CT-11334",  # Account user API access is not enabled.
}


def _graphql_error_codes(errors: Any) -> set[str]:
    """Extract non-sensitive Kraken error codes without retaining messages."""
    codes: set[str] = set()
    if not isinstance(errors, list):
        return codes
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        if not isinstance(extensions, dict):
            continue
        for key in ("errorCode", "error_code", "code"):
            value = extensions.get(key)
            if isinstance(value, str):
                codes.add(value)
    return codes


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise EonNextApiError("E.ON returned a timestamp without a timezone")
    return parsed


class EonNextClient:
    """Read-only E.ON client. It intentionally exposes no mutation except login."""

    def __init__(self, session: ClientSession, api_key: str) -> None:
        self._session = session
        self._api_key = api_key
        self._access_token: str | None = None
        self.viewer_email: str | None = None

    async def _request(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any],
        *,
        authenticated: bool = True,
        authentication_request: bool = False,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if authenticated:
            if not self._access_token:
                raise EonNextAuthenticationError("No E.ON access token is available")
            headers["Authorization"] = f"JWT {self._access_token}"

        try:
            async with self._session.post(
                API_URL,
                json={
                    "operationName": operation,
                    "query": query,
                    "variables": variables,
                },
                headers=headers,
                timeout=ClientTimeout(total=30),
            ) as response:
                status = response.status
                try:
                    body = await response.json()
                except (ContentTypeError, TypeError, ValueError):
                    body = None
        except (ClientError, TimeoutError) as err:
            raise EonNextConnectionError("Could not connect to E.ON") from err

        errors = body.get("errors") if isinstance(body, dict) else None
        error_codes = _graphql_error_codes(errors)
        if status >= 400:
            _LOGGER.warning(
                "E.ON GraphQL operation %s returned HTTP %s with error codes: %s",
                operation,
                status,
                ", ".join(sorted(error_codes)) or "unclassified",
            )
            if authentication_request and (
                status in (401, 403) or (error_codes & _AUTH_ERROR_CODES)
            ):
                raise EonNextAuthenticationError("E.ON authentication failed")
            if status == 429 or 500 <= status < 600:
                raise EonNextConnectionError("E.ON is temporarily unavailable")
            raise EonNextApiError(f"E.ON returned HTTP {status}")

        if not isinstance(body, dict):
            raise EonNextApiError("E.ON returned an invalid response")
        if errors:
            if authentication_request and (error_codes & _AUTH_ERROR_CODES):
                raise EonNextAuthenticationError("E.ON authentication failed")
            _LOGGER.warning(
                "E.ON GraphQL operation %s failed with error codes: %s",
                operation,
                ", ".join(sorted(error_codes)) or "unclassified",
            )
            raise EonNextApiError("E.ON rejected a read-only data request")
        data = body.get("data")
        if not isinstance(data, dict):
            raise EonNextApiError("E.ON returned no data")
        return data

    async def async_authenticate(self) -> None:
        """Exchange the account API key for a short-lived access token."""
        if not self._api_key:
            raise EonNextAuthenticationError("No E.ON API key is available")
        data = await self._request(
            "Login",
            LOGIN_MUTATION,
            {"input": {"APIKey": self._api_key}},
            authenticated=False,
            authentication_request=True,
        )
        result = data.get("obtainKrakenToken")
        if not isinstance(result, dict) or not result.get("token"):
            raise EonNextAuthenticationError("E.ON did not issue an access token")
        self._access_token = result["token"]

    async def async_get_meters(self) -> list[EonMeter]:
        """Discover active electricity and gas meters without retaining identifiers."""
        accounts_data = await self._request("Accounts", ACCOUNTS_QUERY, {})
        viewer = accounts_data.get("viewer") or {}
        if not isinstance(viewer, dict):
            raise EonNextApiError("E.ON returned invalid account data")
        email = viewer.get("email")
        self.viewer_email = email.strip().lower() if isinstance(email, str) else None
        accounts = [
            item.get("number")
            for item in (viewer.get("accounts") or [])
            if isinstance(item, dict) and item.get("number")
        ]
        meters: list[EonMeter] = []
        for account_number in accounts:
            data = await self._request(
                "Meters", METERS_QUERY, {"accountNumber": account_number}
            )
            for property_data in (data.get("properties") or []):
                if not isinstance(property_data, dict):
                    continue
                for fuel, key in (
                    ("electricity", "electricityMeterPoints"),
                    ("gas", "gasMeterPoints"),
                ):
                    for point in (property_data.get(key) or []):
                        if not isinstance(point, dict):
                            continue
                        if fuel == "electricity" and point.get("direction") == "EXPORT":
                            continue
                        for meter in (point.get("meters") or []):
                            if isinstance(meter, dict) and meter.get("id"):
                                unit = meter.get("consumptionUnits")
                                if not isinstance(unit, str) or not (unit.lower() == "kwh" or (fuel == "gas" and unit in {"m3", "m³"})):
                                    _LOGGER.warning(
                                        "Skipping E.ON %s meter with unsupported "
                                        "consumption unit: %s",
                                        fuel,
                                        unit if isinstance(unit, str) else "missing",
                                    )
                                    continue
                                meters.append(
                                    EonMeter(account_number, meter["id"], fuel, unit)
                                )
        return meters

    async def async_get_tariffs(
        self, account_number: str
    ) -> dict[tuple[str, str], tuple[AgreementRates, ...]]:
        """Return published rates per (fuel, meter ID), oldest agreement first."""
        data = await self._request(
            "Tariffs", TARIFFS_QUERY, {"accountNumber": account_number}
        )
        account = data.get("account") or {}
        if not isinstance(account, dict):
            raise EonNextApiError("E.ON returned invalid tariff data")
        result: dict[tuple[str, str], list[AgreementRates]] = {}
        for fuel, key in (
            ("electricity", "electricityAgreements"),
            ("gas", "gasAgreements"),
        ):
            for agreement in account.get(key) or []:
                if not isinstance(agreement, dict):
                    continue
                rates = _agreement_rates(agreement, fuel)
                point = agreement.get("meterPoint") or {}
                if rates is None or not isinstance(point, dict):
                    continue
                for meter in point.get("meters") or []:
                    if isinstance(meter, dict) and isinstance(meter.get("id"), str):
                        result.setdefault((fuel, meter["id"]), []).append(rates)
        return {
            key: tuple(sorted(items, key=lambda item: item.valid_from))
            for key, items in result.items()
        }

    async def async_get_consumption(
        self, meter: EonMeter, start: datetime
    ) -> list[EonInterval]:
        """Return half-hour readings from start, with bounded pagination."""
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        try:
            async with asyncio.timeout(300):
                return await self._async_get_consumption_pages(meter, start)
        except TimeoutError as err:
            raise EonNextConnectionError("E.ON data download timed out") from err

    async def _async_get_consumption_pages(
        self, meter: EonMeter, start: datetime
    ) -> list[EonInterval]:
        """Fetch bounded pages for one meter."""
        agreements = (
            "electricityAgreements"
            if meter.fuel == "electricity"
            else "gasAgreements"
        )
        cursor: str | None = None
        intervals: list[EonInterval] = []
        seen: set[tuple[datetime, datetime]] = set()
        for page_number in range(500):
            variables: dict[str, Any] = {
                "accountNumber": meter.account_number,
                "startAt": start.isoformat(),
            }
            if cursor:
                variables["after"] = cursor
            data = await self._request(
                "Consumption", _consumption_query(meter.fuel), variables
            )
            connection: dict[str, Any] | None = None
            account = data.get("account") or {}
            if not isinstance(account, dict):
                raise EonNextApiError("E.ON returned invalid consumption data")
            for agreement in (account.get(agreements) or []):
                if not isinstance(agreement, dict):
                    continue
                point = agreement.get("meterPoint") or {}
                if not isinstance(point, dict):
                    continue
                for candidate in (point.get("meters") or []):
                    if (
                        isinstance(candidate, dict)
                        and candidate.get("id") == meter.meter_id
                    ):
                        if candidate.get("consumptionUnits") != meter.unit:
                            raise EonNextApiError(
                                "E.ON changed a meter's consumption unit"
                            )
                        candidate_connection = candidate.get("consumption")
                        if isinstance(candidate_connection, dict):
                            connection = candidate_connection
                        break
                if connection:
                    break
            if not connection:
                return sorted(intervals, key=lambda item: item.start)

            for edge in (connection.get("edges") or []):
                if not isinstance(edge, dict):
                    continue
                node = edge.get("node")
                if not isinstance(node, dict):
                    continue
                try:
                    interval_start = _parse_datetime(node["startAt"])
                    interval_end = _parse_datetime(node["endAt"])
                    value = Decimal(str(node["value"]))
                except (KeyError, InvalidOperation, TypeError) as err:
                    raise EonNextApiError("E.ON returned an invalid interval") from err
                key = (interval_start, interval_end)
                if key not in seen:
                    seen.add(key)
                    intervals.append(EonInterval(interval_start, interval_end, value))

            page_info = connection.get("pageInfo") or {}
            if not isinstance(page_info, dict):
                raise EonNextApiError("E.ON returned invalid pagination data")
            cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not cursor:
                return sorted(intervals, key=lambda item: item.start)
            await asyncio.sleep(0.05)
        raise EonNextApiError("E.ON pagination exceeded the safety limit")
