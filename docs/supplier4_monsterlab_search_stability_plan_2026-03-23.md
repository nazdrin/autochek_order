# Supplier 4 Monsterlab Search Stability Plan

Date: 2026-03-23
Project: `/Users/dmitrijnazdrin/Projects/rpa_biotus`
Target file: `/Users/dmitrijnazdrin/Projects/rpa_biotus/scripts/supplier4_run_order.py`

## Context

Recurring flaky failures on supplier 4 (`monsterlab.com.ua`) during `add_items`.

Observed failure types:
- `Search dropdown did not appear for sku=NOW-00470`
- `Search dropdown did not appear for sku=NOW-01294`
- `Search dropdown did not appear for sku=SOL-52392`
- `Buy button not found for sku=LEX-16039`

Current hypothesis:
- Main instability is in the search flow: page opens, script types SKU too early, and assumes JS autocomplete is already initialized.
- Secondary instability is weak result validation: the script can open the wrong product and only fail later on missing `Buy`.
- Search state may persist between attempts, so stale dropdown/input state can contaminate the next SKU search.

## Priority 1: Must Do First

1. Strengthen `_open_search_and_fill()`.
   - After opening search, wait for the real search widget to become ready instead of relying on a fixed `120ms`.
   - Prefer polling for `multi-input` / search widget readiness for 1-3 seconds.
   - Do not fall back to `head_search` too early.

2. Add a guaranteed search state reset before typing a new SKU.
   - Close any visible old dropdown/results.
   - Clear old input value.
   - Verify the old dropdown is gone.
   - Only then type the new SKU.

3. Verify the typed SKU after `type(sku)`.
   - Read `target.input_value()` immediately after typing.
   - Compare with expected SKU.
   - If mismatch: clear field, refocus, and retry typing up to 2-3 times.
   - If still mismatch: raise a dedicated error, for example `SEARCH_INPUT_VALUE_MISMATCH`.

4. Add a local retry for "dropdown did not appear".
   - Retry only the current SKU flow, not the whole order.
   - Sequence:
     - clear search field
     - reopen search
     - retype SKU
     - wait for dropdown again
   - Controlled last retry: reload home/catalog page once for the same SKU, then repeat the search flow once.

5. Distinguish search states before failing.
   - Differentiate:
     - widget not ready
     - input value mismatch
     - dropdown did not open
     - dropdown container opened but is empty
     - no exact SKU in results
   - Avoid collapsing everything into one generic dropdown timeout.

## Priority 2: High Value Next

5. Tighten `_open_product_from_dropdown()`.
   - Do not accept any generic `href` on `monsterlab.com.ua` as a valid match.
   - Prefer exact SKU match in dropdown text or dedicated product metadata.
   - If exact match is absent, do not click the first arbitrary result.

6. Verify the opened product page before searching for `Buy`.
   - After dropdown click, confirm the product page contains the expected SKU/article.
   - If product page SKU does not match, fail with a dedicated error like `PRODUCT_PAGE_SKU_MISMATCH`.

7. Strengthen `_click_buy_on_product()`.
   - Wait for the product card to finish rendering before concluding that `Buy` is absent.
   - Check for:
     - out-of-stock state
     - disabled buy control
     - variant/packaging selector blocking purchase
   - Fail with more specific diagnostics instead of a single `Buy button not found`.

## Priority 3: Diagnostics and Hardening

8. Save artifacts on the two flaky points.
   - On search failure:
     - screenshot
     - page HTML
     - active element info
     - selected input value
   - On missing buy button:
     - screenshot
     - page HTML
     - URL
     - detected product title / SKU text

9. Reduce overuse of `force=True` around search interactions.
   - First try normal click where possible.
   - Use forced click only as fallback.
   - This helps expose overlay/interception problems instead of masking them.

10. Improve popup/overlay handling before search.
   - Extend `_best_effort_close_popups()`.
   - Add explicit wait for overlay invisibility if detected.
   - Re-run popup cleanup before retrying search.

11. Add structured logging per SKU.
   - Which input was used: `multi` or `head_search`
   - Input value after typing
   - Dropdown item count
   - Current URL after dropdown click
   - Whether opened page SKU matched expected SKU

## Proposed Execution Order

Phase 1:
- implement search state reset
- implement input value verification
- implement widget readiness wait
- implement local search retry
- distinguish `dropdown not opened` vs `dropdown empty`

Phase 2:
- implement exact result validation
- implement product page SKU verification
- improve buy button detection and classification

Phase 3:
- add screenshots/HTML dumps
- improve popup handling
- refine logging and error codes

## Expected Outcome

If only Phase 1 is done, search-related flakiness should drop noticeably because the script will stop assuming that typing always succeeded and that autocomplete is always ready immediately.

If Phase 2 is also done, the remaining failures should become much more interpretable:
- either exact SKU was not found
- or the wrong product page opened
- or the product page had no purchasable state

## Notes

Supplier 2 is more stable because its flow is deterministic:
- enter SKU
- submit explicit search
- wait for exact row
- then click buy

Supplier 4 currently relies on JS autocomplete and optimistic assumptions, which makes it sensitive to timing differences across machines.
