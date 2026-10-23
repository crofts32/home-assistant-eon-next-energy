"""Focused tests for E.ON API error handling and meter discovery."""

import importlib.util
import base64
import json
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


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise ClientResponseError(None, (), status=self.status)

    async def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return next(self.responses)


def auth0_id_token(email="user@example.invalid", **overrides):
    claims = {"iss": "https://auth.eonnext.com/", "aud": api.AUTH0_CLIENT_ID,
              "email": email, "exp": 9999999999, **overrides}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth0_rotation_uses_id_token_for_meter_requests(self):
        identity = auth0_id_token()
        session = FakeSession([
            FakeResponse(200, {"id_token": identity, "access_token": "do-not-use-oauth-access",
                               "refresh_token": "rotated-browser-token"}),
            FakeResponse(200, {"data": {"viewer": {"accounts": []}}}),
        ])
        client = api.EonNextClient(session, "original-browser-token",
                                   auth_provider="auth0", expected_email="USER@example.invalid")
        tokens = await client.async_refresh_access_token()
        self.assertEqual(tokens.refresh_token, "rotated-browser-token")
        self.assertEqual(client.refresh_token, "rotated-browser-token")
        await client.async_get_meters()
        self.assertEqual(session.calls[0][0], (api.AUTH0_TOKEN_URL,))
        self.assertEqual(session.calls[0][1]["json"], {
            "grant_type": "refresh_token", "client_id": api.AUTH0_CLIENT_ID,
            "refresh_token": "original-browser-token",
        })
        self.assertEqual(session.calls[1][1]["headers"]["Authorization"], identity)

    async def test_auth0_without_rotation_keeps_existing_refresh_token(self):
        client = api.EonNextClient(FakeSession([
            FakeResponse(200, {"id_token": auth0_id_token()})]),
            "existing-browser-token", auth_provider="auth0")
        self.assertEqual((await client.async_refresh_access_token()).refresh_token,
                         "existing-browser-token")

    async def test_auth0_wrong_user_does_not_replace_saved_credentials(self):
        client = api.EonNextClient(FakeSession([
            FakeResponse(200, {"id_token": auth0_id_token("other@example.invalid"),
                               "refresh_token": "other-users-new-token"})]),
            "existing-token", auth_provider="auth0", expected_email="user@example.invalid")
        with self.assertRaises(api.EonNextAccountMismatchError) as raised:
            await client.async_refresh_access_token()
        self.assertEqual(client.refresh_token, "existing-token")
        self.assertNotIn("other@example", str(raised.exception))
        self.assertNotIn("other-users-new-token", str(raised.exception))

    async def test_auth0_http_failures_are_classified_without_secret_output(self):
        for status, error in [(400, api.EonNextAuthenticationError),
                              (401, api.EonNextAuthenticationError),
                              (403, api.EonNextAuthenticationError),
                              (429, api.EonNextConnectionError),
                              (502, api.EonNextConnectionError)]:
            with self.subTest(status=status):
                client = api.EonNextClient(FakeSession([
                    FakeResponse(status, {"error_description": "secret-provider-detail"})]),
                    "secret-refresh-value", auth_provider="auth0")
                with self.assertRaises(error) as raised:
                    await client.async_refresh_access_token()
                self.assertNotIn("secret", str(raised.exception))

    async def test_auth0_rejects_bad_identity_response(self):
        for body in [{}, {"access_token": "access-without-id"}, {"id_token": "bad"},
                     {"id_token": auth0_id_token(iss="https://unexpected.invalid/")},
                     {"id_token": auth0_id_token(aud="different-client")}]:
            with self.subTest(body=body):
                client = api.EonNextClient(FakeSession([FakeResponse(200, body)]),
                                           "refresh", auth_provider="auth0")
                with self.assertRaises(api.EonNextApiError):
                    await client.async_refresh_access_token()

    async def test_refresh_502_is_connection_error(self):
        client = api.EonNextClient(FakeSession([FakeResponse(502, {})]), "refresh")
        with self.assertRaises(api.EonNextConnectionError):
            await client.async_refresh_access_token()

    async def test_refresh_401_is_authentication_error(self):
        client = api.EonNextClient(FakeSession([FakeResponse(401, {})]), "refresh")
        with self.assertRaises(api.EonNextAuthenticationError):
            await client.async_refresh_access_token()

    async def test_unknown_graphql_error_is_not_authentication_error(self):
        response = FakeResponse(200, {"errors": [{"extensions": {"code": "OTHER"}}]})
        client = api.EonNextClient(FakeSession([response]), "refresh")
        with self.assertLogs("eon_next_energy.api", level="WARNING") as logs:
            with self.assertRaises(api.EonNextApiError):
                await client.async_refresh_access_token()
        self.assertIn("Login", logs.output[0])
        self.assertIn("OTHER", logs.output[0])

    async def test_rejected_credentials_are_an_authentication_error(self):
        response = FakeResponse(
            200,
            {"errors": [{"extensions": {"errorCode": "KT-CT-1138"}}]},
        )
        client = api.EonNextClient(FakeSession([response]))
        with self.assertRaises(api.EonNextAuthenticationError):
            await client.async_login("user@example.invalid", "wrong-password")

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
        client = api.EonNextClient(FakeSession(responses), "refresh")
        client._access_token = "access"
        self.assertEqual(await client.async_get_meters(), [])

    async def test_unknown_meter_unit_is_skipped(self):
        responses = [
            FakeResponse(200, {"data": {"viewer": {"accounts": [{"number": "A"}]}}}),
            FakeResponse(200, {"data": {"properties": [{"gasMeterPoints": [{"meters": [{"id": "G", "consumptionUnits": "unknown"}]}]}]}}),
        ]
        client = api.EonNextClient(FakeSession(responses), "refresh")
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
            client = api.EonNextClient(FakeSession(responses), "refresh")
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
        client = api.EonNextClient(FakeSession(responses), "refresh")
        client._access_token = "access"
        with self.assertLogs("eon_next_energy.api", level="WARNING"):
            meters = await client.async_get_meters()
        self.assertEqual(meters, [api.EonMeter("A", "E", "electricity", "kWh")])


if __name__ == "__main__":
    unittest.main()
