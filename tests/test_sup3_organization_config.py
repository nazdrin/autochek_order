import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import orchestrator  # noqa: E402


class Supplier3OrganizationConfigTests(unittest.TestCase):
    def test_org4_reuses_the_primary_dsn_account(self):
        primary = orchestrator.resolve_sup3_account_config({"organizationId": 1})
        org4 = orchestrator.resolve_sup3_account_config({"organizationId": 4})

        self.assertEqual("4", org4["organization_id"])
        self.assertEqual(primary["storage_state_file"], org4["storage_state_file"])
        self.assertEqual(primary["use_cdp"], org4["use_cdp"])
        self.assertEqual("", org4["login_email"])
        self.assertEqual("", org4["login_password"])

    def test_unsupported_org_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Unsupported SUP3 organizationId=5"):
            orchestrator.resolve_sup3_account_config({"organizationId": 5})

    def test_targeted_order_filter_cannot_include_another_queue_item(self):
        orders = [{"id": 30115}, {"id": 30116}, {"id": "30117"}]
        self.assertEqual([{"id": 30115}], orchestrator.filter_orders_by_id(orders, 30115))


if __name__ == "__main__":
    unittest.main()
