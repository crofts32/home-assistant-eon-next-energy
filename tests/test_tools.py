"""Tests for the API-key helper's token handling."""

import base64
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "eon_api_key", Path(__file__).parents[1] / "tools" / "eon_api_key.py"
)
assert spec and spec.loader
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def fake_jwt(iss):
    payload = base64.urlsafe_b64encode(json.dumps({"iss": iss}).encode()).decode().rstrip("=")
    return f"e30.{payload}.sig"


class NormaliseTokenTests(unittest.TestCase):
    def test_explicit_schemes_are_kept(self):
        self.assertEqual(tool.normalise_token("Bearer abc"), "Bearer abc")
        self.assertEqual(tool.normalise_token("jwt abc"), "JWT abc")
        self.assertEqual(tool.normalise_token("Authorization: Bearer abc"), "Bearer abc")

    def test_bare_auth0_token_uses_bearer(self):
        token = fake_jwt("https://auth.eonnext.com/")
        self.assertEqual(tool.normalise_token(f'"{token}"'), f"Bearer {token}")

    def test_bare_kraken_token_uses_jwt(self):
        token = fake_jwt("https://api.eonnext-kraken.energy/v1/graphql/")
        self.assertEqual(tool.normalise_token(token), f"JWT {token}")


if __name__ == "__main__":
    unittest.main()
