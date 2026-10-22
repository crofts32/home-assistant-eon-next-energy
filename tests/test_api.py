"""Focused tests for E.ON API error handling and meter discovery."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "eon_next_energy"


class ClientError(Exception):
    pass


class ClientResponseError(ClientError):
    def __init__(self, *_args, status=0, **_kwargs):
        super().__init__()
        self.status = status


class ContentTypeError(ClientResponseError):
    pass


class ClientTimeout:
    def __init__(self, **_kwargs):
        pass


aiohttp = types.ModuleType("aiohttp")
aiohttp.ClientError = ClientError
aiohttp.ClientResponseError = ClientResponseError
aiohttp.ClientSession = object
aiohttp.ClientTimeout = ClientTimeout
aiohttp.ContentTypeError = ContentTypeError
sys.modules.setdefault("aiohttp", aiohttp)

package = types.ModuleType("eon_next_energy")
package.__path__ = [str(PACKAGE)]
sys.modules.setdefault("eon_next_energy", package)
for name in ("const", "api"):
    spec = importlib.util.spec_from_file_location(
        f"eon_next_energy.{name}", PACKAGE / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

api = sys.modules["eon_next_energy.api"]


NOT_JSON = object()


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        if self._body is NOT_JSON:
            raise ContentTypeError()
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, *_args, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_502_is_connection_error(self):
        client = api.EonNextClient(FakeSession([FakeResponse(502, {})]), "sk_live_x")
        with self.assertRaises(api.EonNextConnectionError):
            await client.async_authenticate()

    async def test_login_401_is_authentication_error(self):
        client = api.EonNextClient(FakeSession([FakeResponse(401, {})]), "sk_live_x")
        with self.assertRaises(api.EonNextAuthenticationError):
            await client.async_authenticate()

    async def test_non_json_error_body_is_still_classified(self):
        client = api.EonNextClient(FakeSession([FakeResponse(503, NOT_JSON)]), "sk_live_x")
        with self.assertRaises(api.EonNextConnectionError):
            await client.async_authenticate()

    async def test_missing_api_key_is_authentication_error(self):
        client = api.EonNextClient(FakeSession([]), "")
        with self.assertRaises(api.EonNextAuthenticationError):
            await client.async_authenticate()

    async def test_login_sends_api_key_and_stores_access_token(self):
        session = FakeSession([FakeResponse(200, {"data": {"obtainKrakenToken": {"token": "access"}}})])
        client = api.EonNextClient(session, "sk_live_x")
        await client.async_authenticate()
        self.assertEqual(client._access_token, "access")
        self.assertEqual(session.calls[0]["json"]["variables"], {"input": {"APIKey": "sk_live_x"}})
        self.assertNotIn("Authorization", session.calls[0]["headers"])

    async def test_http_400_logs_codes_but_not_messages(self):
        response = FakeResponse(
            400,
            {"errors": [{"message": "echo sk_live_secret", "extensions": {"errorCode": "KT-CT-9999"}}]},
        )
        client = api.EonNextClient(FakeSession([response]), "sk_live_secret")
        with self.assertLogs("eon_next_energy.api", level="WARNING") as logs:
            with self.assertRaises(api.EonNextApiError):
                await client.async_authenticate()
        self.assertIn("HTTP 400", logs.output[0])
        self.assertIn("KT-CT-9999", logs.output[0])
        self.assertNotIn("sk_live_secret", "".join(logs.output))

    async def test_http_400_schema_error_is_unclassified(self):
        response = FakeResponse(400, {"errors": [{"message": "Field 'email' is not defined"}]})
        client = api.EonNextClient(FakeSession([response]), "sk_live_x")
        with self.assertLogs("eon_next_energy.api", level="WARNING") as logs:
            with self.assertRaises(api.EonNextApiError):
                await client.async_authenticate()
        self.assertIn("unclassified", logs.output[0])

    async def test_unknown_graphql_error_is_not_authentication_error(self):
        response = FakeResponse(200, {"errors": [{"extensions": {"code": "OTHER"}}]})
        client = api.EonNextClient(FakeSession([response]), "sk_live_x")
        with self.assertLogs("eon_next_energy.api", level="WARNING") as logs:
            with self.assertRaises(api.EonNextApiError):
                await client.async_authenticate()
        self.assertIn("Login", logs.output[0])
        self.assertIn("OTHER", logs.output[0])

    async def test_rejected_api_key_is_an_authentication_error(self):
        for status in (200, 400):
            response = FakeResponse(
                status,
                {"errors": [{"extensions": {"errorCode": "KT-CT-1139"}}]},
            )
            client = api.EonNextClient(FakeSession([response]), "sk_live_wrong")
            with self.assertRaises(api.EonNextAuthenticationError):
                with self.assertNoLogs("eon_next_energy.api", level="ERROR"):
                    await client.async_authenticate()

    async def test_data_query_401_does_not_force_reauth(self):
        client = api.EonNextClient(FakeSession([FakeResponse(401, NOT_JSON)]), "sk_live_x")
        client._access_token = "access"
        with self.assertLogs("eon_next_energy.api", level="WARNING"):
            with self.assertRaises(api.EonNextApiError):
                await client.async_get_meters()

    async def test_api_key_service_outage_does_not_force_reauth(self):
        response = FakeResponse(200, {"errors": [{"extensions": {"errorCode": "KT-CT-11100"}}]})
        client = api.EonNextClient(FakeSession([response]), "sk_live_x")
        with self.assertLogs("eon_next_energy.api", level="WARNING"):
            with self.assertRaises(api.EonNextApiError):
                await client.async_authenticate()

    async def test_viewer_email_is_normalised(self):
        responses = [
            FakeResponse(200, {"data": {"viewer": {"email": " User@Example.Invalid ", "accounts": []}}}),
        ]
        client = api.EonNextClient(FakeSession(responses), "sk_live_x")
        client._access_token = "access"
        self.assertEqual(await client.async_get_meters(), [])
        self.assertEqual(client.viewer_email, "user@example.invalid")

    async def test_null_points_and_export_meter_are_skipped(self):
        responses = [
            FakeResponse(200, {"data": {"viewer": {"accounts": [{"number": "A"}]}}}),
            FakeResponse(
                200,
                {
                    "data": {
                        "properties": [
                            {
                                "electricityMeterPoints": [
                                    {"direction": "EXPORT", "meters": [{"id": "X", "consumptionUnits": "kWh"}]}
                                ],
                                "gasMeterPoints": None,
                            }
                        ]
                    }
                },
            ),
        ]
        client = api.EonNextClient(FakeSession(responses), "sk_live_x")
        client._access_token = "access"
        self.assertEqual(await client.async_get_meters(), [])

    async def test_unknown_meter_unit_is_skipped(self):
        responses = [
            FakeResponse(200, {"data": {"viewer": {"accounts": [{"number": "A"}]}}}),
            FakeResponse(200, {"data": {"properties": [{"gasMeterPoints": [{"meters": [{"id": "G", "consumptionUnits": "unknown"}]}]}]}}),
        ]
        client = api.EonNextClient(FakeSession(responses), "sk_live_x")
        client._access_token = "access"
        with self.assertLogs("eon_next_energy.api", level="WARNING") as logs:
            self.assertEqual(await client.async_get_meters(), [])
        self.assertIn("gas", logs.output[0])
        self.assertIn("unknown", logs.output[0])

    async def test_gas_volume_is_supported(self):
        for unit in ("m3", "m³"):
            responses = [
                FakeResponse(200, {"data": {"viewer": {"accounts": [{"number": "A"}]}}}),
                FakeResponse(200, {"data": {"properties": [{"gasMeterPoints": [{"meters": [{"id": "G", "consumptionUnits": unit}]}]}]}}),
            ]
            client = api.EonNextClient(FakeSession(responses), "sk_live_x")
            client._access_token = "access"
            self.assertEqual(await client.async_get_meters(), [api.EonMeter("A", "G", "gas", unit)])

    async def test_supported_meter_survives_unsupported_meter(self):
        responses = [
            FakeResponse(200, {"data": {"viewer": {"accounts": [{"number": "A"}]}}}),
            FakeResponse(
                200,
                {
                    "data": {
                        "properties": [
                            {
                                "electricityMeterPoints": [
                                    {
                                        "direction": "IMPORT",
                                        "meters": [
                                            {"id": "E", "consumptionUnits": "kWh"}
                                        ],
                                    }
                                ],
                                "gasMeterPoints": [
                                    {
                                        "meters": [
                                            {"id": "G", "consumptionUnits": "unknown"}
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                },
            ),
        ]
        client = api.EonNextClient(FakeSession(responses), "sk_live_x")
        client._access_token = "access"
        with self.assertLogs("eon_next_energy.api", level="WARNING"):
            meters = await client.async_get_meters()
        self.assertEqual(meters, [api.EonMeter("A", "E", "electricity", "kWh")])


if __name__ == "__main__":
    unittest.main()
