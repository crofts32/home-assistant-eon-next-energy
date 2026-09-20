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
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
)

from .const import API_URL

_LOGGER = logging.getLogger(__name__)


class EonNextError(Exception):
    """Base E.ON Next client error."""


class EonNextAuthenticationError(EonNextError):
    """Authentication or token refresh failed."""


class EonNextConnectionError(EonNextError):
    """The service could not be reached."""


class EonNextApiError(EonNextError):
    """The API returned an unexpected response."""


@dataclass(frozen=True, slots=True)
class EonTokens:
    """Short-lived access token and rotating refresh token."""

    access_token: str
    refresh_token: str
    access_expires_at: int | None
    refresh_expires_at: int | None


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
    value_kwh: Decimal


LOGIN_MUTATION = """
mutation Login($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) {
    payload
    refreshExpiresIn
    refreshToken
    token
  }
}
"""

ACCOUNTS_QUERY = """
query Accounts {
  viewer { accounts { ... on AccountType { number } } }
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

    def __init__(self, session: ClientSession, refresh_token: str | None = None) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._access_token: str | None = None

    @property
    def refresh_token(self) -> str | None:
        """Return the most recently issued refresh token."""
        return self._refresh_token

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
                response.raise_for_status()
                body = await response.json()
        except ContentTypeError as err:
            raise EonNextApiError("E.ON returned an invalid response") from err
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise EonNextAuthenticationError("E.ON authentication failed") from err
            if err.status == 429 or 500 <= err.status < 600:
                raise EonNextConnectionError("E.ON is temporarily unavailable") from err
            raise EonNextApiError(f"E.ON returned HTTP {err.status}") from err
        except (ClientError, TimeoutError) as err:
            raise EonNextConnectionError("Could not connect to E.ON") from err
        except (TypeError, ValueError) as err:
            raise EonNextApiError("E.ON returned an invalid response") from err

        if not isinstance(body, dict):
            raise EonNextApiError("E.ON returned an invalid response")
        errors = body.get("errors")
        if errors:
            error_codes = _graphql_error_codes(errors)
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

    async def async_login(self, email: str, password: str) -> EonTokens:
        """Exchange credentials for tokens. The caller must discard the password."""
        return await self._async_obtain_token({"email": email, "password": password})

    async def async_refresh_access_token(self) -> EonTokens:
        """Use the stored refresh token to obtain a new access token."""
        if not self._refresh_token:
            raise EonNextAuthenticationError("No E.ON refresh token is available")
        return await self._async_obtain_token({"refreshToken": self._refresh_token})

    async def _async_obtain_token(self, token_input: dict[str, str]) -> EonTokens:
        data = await self._request(
            "Login",
            LOGIN_MUTATION,
            {"input": token_input},
            authenticated=False,
            authentication_request=True,
        )
        result = data.get("obtainKrakenToken")
        if not isinstance(result, dict) or not result.get("token"):
            raise EonNextAuthenticationError("E.ON did not issue an access token")
        refresh_token = result.get("refreshToken") or self._refresh_token
        if not refresh_token:
            raise EonNextAuthenticationError("E.ON did not issue a refresh token")
        payload = result.get("payload") or {}
        tokens = EonTokens(
            access_token=result["token"],
            refresh_token=refresh_token,
            access_expires_at=payload.get("exp") if isinstance(payload, dict) else None,
            refresh_expires_at=result.get("refreshExpiresIn"),
        )
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token
        return tokens

    async def async_get_meters(self) -> list[EonMeter]:
        """Discover active electricity and gas meters without retaining identifiers."""
        accounts_data = await self._request("Accounts", ACCOUNTS_QUERY, {})
        viewer = accounts_data.get("viewer") or {}
        if not isinstance(viewer, dict):
            raise EonNextApiError("E.ON returned invalid account data")
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
                                if not isinstance(unit, str) or unit.lower() != "kwh":
                                    raise EonNextApiError(
                                        "E.ON returned an unsupported consumption unit"
                                    )
                                meters.append(
                                    EonMeter(account_number, meter["id"], fuel, unit)
                                )
        return meters

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
