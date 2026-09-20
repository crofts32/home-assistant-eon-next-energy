#!/usr/bin/env python3
"""Read-only E.ON Next probe for half-hour smart-meter consumption data.

Credentials and API tokens remain in memory. The probe writes no files and prints
no account numbers, meter serials, meter-point identifiers, or tokens.
"""

from __future__ import annotations

import asyncio
import getpass
from collections import Counter
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import httpx


API_URL = "https://api.eonnext-kraken.energy/v1/graphql/"
LONDON = ZoneInfo("Europe/London")


LOGIN = """
mutation Login($input: ObtainJSONWebTokenInput!) {
  obtainKrakenToken(input: $input) { token }
}
"""

ACCOUNTS = """
query Accounts {
  viewer { accounts { ... on AccountType { number } } }
}
"""

METERS = """
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


def consumption_query(fuel: str) -> str:
    agreement = "electricityAgreements" if fuel == "electricity" else "gasAgreements"
    return f"""
query Consumption($accountNumber: String!, $startAt: DateTime!, $after: String) {{
  account(accountNumber: $accountNumber) {{
    {agreement}(active: true) {{
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


async def graphql(
    client: httpx.AsyncClient,
    operation: str,
    query: str,
    variables: dict,
    token: str | None = None,
) -> dict:
    headers = {"authorization": f"JWT {token}"} if token else {}
    response = await client.post(
        API_URL,
        json={"operationName": operation, "query": query, "variables": variables},
        headers=headers,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        message = body["errors"][0].get("message", "GraphQL request failed")
        raise RuntimeError(message)
    return body["data"]


async def fetch_intervals(client, token, account, meter_id, fuel, start, end):
    cursor = None
    records: list[dict] = []
    agreement = "electricityAgreements" if fuel == "electricity" else "gasAgreements"

    while True:
        variables = {"accountNumber": account, "startAt": start.isoformat()}
        if cursor:
            variables["after"] = cursor
        data = await graphql(
            client, "Consumption", consumption_query(fuel), variables, token
        )
        connections = []
        account_data = data.get("account") or {}
        for item in (account_data.get(agreement) or []):
            for meter in (item.get("meterPoint") or {}).get("meters", []):
                if (
                    meter
                    and meter.get("id") == meter_id
                    and meter.get("consumptionUnits") == "kWh"
                    and meter.get("consumption")
                ):
                    connections.append(meter["consumption"])

        if not connections:
            return records

        connection = connections[0]
        reached_end = False
        for edge in connection.get("edges", []):
            node = (edge or {}).get("node")
            if not node or not node.get("startAt"):
                continue
            stamp = datetime.fromisoformat(node["startAt"].replace("Z", "+00:00"))
            if stamp >= end:
                reached_end = True
                break
            records.append(node)

        page = connection.get("pageInfo") or {}
        cursor = page.get("endCursor")
        if reached_end or not page.get("hasNextPage") or not cursor:
            return records


def report(fuel: str, ordinal: int, records: list[dict], dates: list):
    label = f"{fuel.title()} meter {ordinal}"
    if not records:
        print(f"\n{label}: NO HALF-HOURLY DATA RETURNED")
        return False

    counts = Counter()
    totals = Counter()
    valid = 0
    for row in records:
        stamp = datetime.fromisoformat(row["startAt"].replace("Z", "+00:00"))
        day = stamp.astimezone(LONDON).date()
        counts[day] += 1
        try:
            totals[day] += Decimal(str(row["value"]))
            valid += 1
        except (InvalidOperation, TypeError):
            pass

    print(f"\n{label}: {len(records)} intervals returned")
    for day in dates:
        status = "complete" if counts[day] in (46, 48, 50) else "incomplete/missing"
        total = f", {totals[day]:.3f} kWh" if counts[day] else ""
        print(f"  {day.isoformat()}: {counts[day]} intervals ({status}){total}")
    print(f"  Numeric readings: {valid}/{len(records)}")
    return bool(records)


async def main() -> int:
    print("E.ON half-hour data probe (read-only; nothing is saved)")
    username = input("E.ON Next email: ").strip()
    password = getpass.getpass("E.ON Next password: ")
    if not username or not password:
        print("Email and password are required.")
        return 2

    today = datetime.now(LONDON).date()
    dates = [today - timedelta(days=n) for n in range(7, 0, -1)]
    start = datetime.combine(dates[0], time.min, LONDON)
    end = datetime.combine(today, time.min, LONDON)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            auth = await graphql(
                client,
                "Login",
                LOGIN,
                {"input": {"email": username, "password": password}},
            )
            token = (auth.get("obtainKrakenToken") or {}).get("token")
            password = ""
            if not token:
                raise RuntimeError("Authentication did not return a token")

            account_data = await graphql(client, "Accounts", ACCOUNTS, {}, token)
            viewer = account_data.get("viewer") or {}
            accounts = [
                item.get("number")
                for item in (viewer.get("accounts") or [])
                if item and item.get("number")
            ]
            if not accounts:
                raise RuntimeError("No E.ON accounts were returned")

            meters: list[tuple[str, str, str]] = []
            for account in accounts:
                meter_data = await graphql(
                    client, "Meters", METERS, {"accountNumber": account}, token
                )
                for prop in (meter_data.get("properties") or []):
                    for point in (prop.get("electricityMeterPoints") or []):
                        if point.get("direction") == "EXPORT":
                            continue
                        for meter in (point.get("meters") or []):
                            if meter and meter.get("id"):
                                if meter.get("consumptionUnits") != "kWh":
                                    raise RuntimeError("Unsupported electricity unit")
                                meters.append((account, meter["id"], "electricity"))
                    for point in (prop.get("gasMeterPoints") or []):
                        for meter in (point.get("meters") or []):
                            if meter and meter.get("id"):
                                if meter.get("consumptionUnits") != "kWh":
                                    raise RuntimeError("Unsupported gas unit")
                                meters.append((account, meter["id"], "gas"))

            if not meters:
                raise RuntimeError("No active meters were returned")

            print(f"\nFound {len(meters)} active meter(s); identifiers are redacted.")
            seen = Counter()
            any_data = False
            for account, meter_id, fuel in meters:
                seen[fuel] += 1
                records = await fetch_intervals(
                    client, token, account, meter_id, fuel, start, end
                )
                any_data |= report(fuel, seen[fuel], records, dates)

            print("\nRESULT:", "HALF-HOURLY DATA AVAILABLE" if any_data else "NO DATA FOUND")
            return 0 if any_data else 1
    except httpx.HTTPError as exc:
        print(f"\nProbe failed: network/API error ({type(exc).__name__}).")
        return 2
    except Exception:
        print("\nProbe failed: E.ON returned an unexpected response.")
        return 2
    finally:
        password = ""


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
