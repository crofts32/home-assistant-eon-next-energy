"""Static package checks that do not require a Home Assistant runtime."""

import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
INTEGRATION = ROOT / "custom_components" / "eon_next_energy"


class PackageMetadataTests(unittest.TestCase):
    def test_manifest_declares_cloud_polling_without_dependencies(self):
        manifest = json.loads((INTEGRATION / "manifest.json").read_text())
        self.assertEqual(manifest["domain"], "eon_next_energy")
        self.assertTrue(manifest["config_flow"])
        self.assertEqual(manifest["iot_class"], "cloud_polling")
        self.assertEqual(manifest["requirements"], [])
        for key in ("codeowners", "documentation", "issue_tracker", "version"):
            self.assertIn(key, manifest)

    def test_hacs_metadata_targets_supported_home_assistant(self):
        metadata = json.loads((ROOT / "hacs.json").read_text())
        self.assertEqual(metadata["country"], "GB")
        self.assertEqual(metadata["homeassistant"], "2026.9.3")
        self.assertTrue(metadata["render_readme"])

    def test_translations_are_valid_json(self):
        json.loads((INTEGRATION / "strings.json").read_text())
        json.loads((INTEGRATION / "translations" / "en.json").read_text())

    def test_graphql_operations_are_strictly_read_only_except_login(self):
        source = (INTEGRATION / "api.py").read_text().lower()
        operations = re.findall(
            r"^\s*(?:query|mutation)\s+(login|accounts|meters|consumption)\b",
            source,
            re.MULTILINE,
        )
        self.assertEqual(set(operations), {"login", "accounts", "meters", "consumption"})
        mutations = re.findall(
            r"^\s*mutation\s+(\w+)", source, re.MULTILINE
        )
        self.assertEqual(mutations, ["login"])


if __name__ == "__main__":
    unittest.main()
