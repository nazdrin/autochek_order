import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from supplier4_run_order import (  # noqa: E402
    _classify_exact_dropdown_candidates,
    _product_page_sku_match_sources,
)


def candidate(*, text="", href="", metadata=None, is_product_link=True):
    return {
        "text": text,
        "href": href,
        "metadata": metadata or {},
        "is_product_link": is_product_link,
    }


class Supplier4ProductIdentityTests(unittest.TestCase):
    def test_single_irrelevant_result_is_rejected(self):
        outcome, selected = _classify_exact_dropdown_candidates(
            "NOW-00105",
            [candidate(text="NOW L-Lysine 500 mg 100 таблеток", href="/l-lysine")],
        )
        self.assertEqual("no_exact", outcome)
        self.assertIsNone(selected)

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

    def test_partial_skus_do_not_match(self):
        for value in ("NOW-0010", "NOW-00100"):
            with self.subTest(value=value):
                outcome, selected = _classify_exact_dropdown_candidates(
                    "NOW-00105", [candidate(text=f"Product {value}", href=f"/product/{value}")]
                )
                self.assertEqual("no_exact", outcome)
                self.assertIsNone(selected)

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

    def test_dropdown_title_cannot_validate_product_page(self):
        sources = _product_page_sku_match_sources(
            "NOW-00105",
            title="NOW L-Lysine 500 mg",
            body_text="NOW L-Lysine 500 mg 100 таблеток",
            url="https://monsterlab.com.ua/l-lysine/",
        )
        self.assertEqual([], sources)

    def test_product_page_exact_sku_is_accepted(self):
        sources = _product_page_sku_match_sources(
            "NOW-00105",
            title="NOW Vitamin C",
            body_text="Артикул: NOW-00105",
            url="https://monsterlab.com.ua/vitamin-c/",
            metadata={"data-sku": "NOW-00105"},
        )
        self.assertIn("page_text", sources)
        self.assertIn("metadata", sources)


if __name__ == "__main__":
    unittest.main()
