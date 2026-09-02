import os
import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orchestrator  # noqa: E402


class NovaPoshtaOrganizationConfigTests(unittest.TestCase):
    ENV_KEYS = (
        "BIOTUS_NP_API_KEY",
        "NP_API_KEY",
        "BIOTUS_NP_API_KEY_ORG_2",
        "NP_API_KEY_ORG_2",
        "BIOTUS_NP_API_KEY_ORG_3",
        "NP_API_KEY_ORG_3",
    )

    def setUp(self):
        self.previous = {key: os.environ.get(key) for key in self.ENV_KEYS}
        os.environ["BIOTUS_NP_API_KEY"] = "org1-key"
        os.environ["BIOTUS_NP_API_KEY_ORG_2"] = "org2-key"
        os.environ["BIOTUS_NP_API_KEY_ORG_3"] = "org3-key"
        for key in ("NP_API_KEY", "NP_API_KEY_ORG_2", "NP_API_KEY_ORG_3"):
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_selects_explicit_key_for_each_configured_organization(self):
        for organization_id, expected_key, expected_source in (
            (1, "org1-key", "BIOTUS_NP_API_KEY"),
            (2, "org2-key", "BIOTUS_NP_API_KEY_ORG_2"),
            (3, "org3-key", "BIOTUS_NP_API_KEY_ORG_3"),
        ):
            with self.subTest(organization_id=organization_id):
                key, source = orchestrator.resolve_np_api_key_for_order({"organizationId": organization_id})
                self.assertEqual(expected_key, key)
                self.assertEqual(expected_source, source)

    def test_org3_without_its_key_does_not_fall_back_to_org1(self):
        os.environ.pop("BIOTUS_NP_API_KEY_ORG_3")
        with self.assertRaisesRegex(RuntimeError, "organizationId=3 requires BIOTUS_NP_API_KEY_ORG_3"):
            orchestrator.resolve_np_api_key_for_order({"organizationId": 3})

    def test_org3_accepts_its_np_api_key_alias(self):
        os.environ.pop("BIOTUS_NP_API_KEY_ORG_3")
        os.environ["NP_API_KEY_ORG_3"] = "org3-alias-key"
        key, source = orchestrator.resolve_np_api_key_for_order({"organizationId": 3})
        self.assertEqual("org3-alias-key", key)
        self.assertEqual("BIOTUS_NP_API_KEY_ORG_3", source)

    def test_unknown_and_invalid_organization_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Unsupported organizationId=4"):
            orchestrator.resolve_np_api_key_for_order({"organizationId": 4})
        with self.assertRaisesRegex(RuntimeError, "Invalid organizationId='abc'"):
            orchestrator.resolve_np_api_key_for_order({"organizationId": "abc"})

    def test_injection_overwrites_every_supplier_np_alias_with_org3_key(self):
        env = {}
        orchestrator.inject_np_api_key_env(env, {"id": 26192, "organizationId": 3})
        for key_name in orchestrator.NP_API_KEY_ENV_KEYS:
            self.assertEqual("org3-key", env[key_name])


if __name__ == "__main__":
    unittest.main()
