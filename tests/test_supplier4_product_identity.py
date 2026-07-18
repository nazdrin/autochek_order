import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from supplier4_run_order import (  # noqa: E402
    _article_matches_sku,
    _checkout_cart_entry_for_sku,
    _classify_exact_dropdown_candidates,
)


def candidate(*, text="", href="", metadata=None, is_product_link=True):
    return {
        "text": text,
        "href": href,
        "metadata": metadata or {},
        "is_product_link": is_product_link,
    }


class Supplier4ProductIdentityTests(unittest.TestCase):
    def test_single_result_is_opened_for_product_page_article_check(self):
        outcome, selected = _classify_exact_dropdown_candidates(
            "NOW-00105",
            [candidate(text="NOW L-Lysine 500 mg 100 таблеток", href="/l-lysine")],
        )
        self.assertEqual("single_result", outcome)
        self.assertEqual(["single_dropdown_result"], selected["sku_match_sources"])

    def test_exact_sku_in_dropdown_text_is_selected(self):
        outcome, selected = _classify_exact_dropdown_candidates(
            "NOW-00105",
            [candidate(text="NOW-00105 Vitamin C", href="/vitamin-c")],
        )
        self.assertEqual("exact", outcome)
        self.assertEqual(["dropdown_text"], selected["sku_match_sources"])

    def test_exact_sku_in_href_or_metadata_is_selected(self):
        for item, expected_source in (
            (candidate(text="Vitamin C", href="/product/now-00105"), "href"),
            (candidate(text="Vitamin C", href="/product/vitamin-c", metadata={"data-sku": "NOW-00105"}), "metadata"),
        ):
            with self.subTest(expected_source=expected_source):
                outcome, selected = _classify_exact_dropdown_candidates("NOW-00105", [item])
                self.assertEqual("exact", outcome)
                self.assertIn(expected_source, selected["sku_match_sources"])

    def test_partial_skus_are_not_accepted_without_product_page_check(self):
        for value in ("NOW-0010", "NOW-00100"):
            with self.subTest(value=value):
                outcome, selected = _classify_exact_dropdown_candidates(
                    "NOW-00105", [candidate(text=f"Product {value}", href=f"/product/{value}")]
                )
                self.assertEqual("single_result", outcome)
                self.assertEqual(["single_dropdown_result"], selected["sku_match_sources"])

    def test_two_exact_candidates_are_ambiguous(self):
        outcome, selected = _classify_exact_dropdown_candidates(
            "NOW-00105",
            [
                candidate(text="NOW-00105, 100 tablets", href="/product/a"),
                candidate(text="NOW-00105, 50 tablets", href="/product/b"),
            ],
        )
        self.assertEqual("ambiguous", outcome)
        self.assertIsNone(selected)

    def test_product_page_article_must_exactly_match_requested_sku(self):
        self.assertTrue(_article_matches_sku("CEN-27116", "CEN-27116"))
        self.assertFalse(_article_matches_sku("NOW-00105", "NOW-00100"))
        self.assertFalse(_article_matches_sku("NOW-00105", ""))

    def test_checkout_row_is_resolved_by_supplier_article_and_hash(self):
        entry = _checkout_cart_entry_for_sku(
            "21296",
            [
                {"article": "21296", "hash": "eb380eb8b03f5cb93cae5994f2cbd510", "quantity": 1},
                {"article": "SW957", "hash": "28f7d563232032ef3a49114e2480a3da", "quantity": 1},
            ],
        )
        self.assertEqual("21296", entry["article"])
        self.assertEqual("eb380eb8b03f5cb93cae5994f2cbd510", entry["hash"])

    def test_checkout_article_requires_one_valid_hash(self):
        self.assertIsNone(_checkout_cart_entry_for_sku("21296", [{"article": "21296", "hash": "bad hash"}]))
        self.assertIsNone(_checkout_cart_entry_for_sku("21296", [{"article": "21296", "hash": "a"}, {"article": "21296", "hash": "b"}]))


if __name__ == "__main__":
    unittest.main()
