"""The three-way match: order, receipt, voucher — and where they disagree.

"Why is this voucher stuck?" is answered by a comparison nobody performs by
hand past the third voucher: what was agreed (the PO schedule), what
arrived (the receipt shipment line), what was billed (the voucher line).
The sample stages one order per way the comparison can fail, so every
branch below is exercised against a break that is visibly wrong, not
rounding noise:

    PO2001  clean and paid          PO2004  vouchered, nothing received
    PO2002  price break (+750)      PO2005  received, never invoiced
    PO2003  qty break (50 vs 30)    PO2006  canceled — the trap
                                    PO2007  genuinely awaiting receipt

What is pinned hardest is the pair PO2006/PO2007: identical in every column
the awaiting-receipt check reads except PO_STATUS. Counting a canceled
order as late is how a workbench trains people to ignore it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pstb.config import Config  # noqa: E402
from pstb.db import Database, DbError  # noqa: E402
from pstb.engine import TBEngine  # noqa: E402
from pstb.modules import ModuleError, ModulePacks  # noqa: E402
from pstb.procurement import Procurement  # noqa: E402

BU = "US001"
ASOF = "2026-08-14"


def _proc(db_cls=Database) -> Procurement:
    cfg = Config.sample(ROOT)
    return Procurement(ModulePacks(TBEngine(db_cls(cfg), cfg)))


class MatchExceptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = _proc().match_exceptions(business_unit=BU,
                                           as_of_date=ASOF)

    def _only(self, kind: str) -> dict:
        rows = self.out["exceptions"][kind]
        self.assertEqual(len(rows), 1, f"{kind}: {rows}")
        return rows[0]

    def test_each_staged_break_is_found_once(self) -> None:
        self.assertEqual(self.out["counts"], {
            "over_order": 1, "not_received": 1, "no_receipt": 1,
            "never_invoiced": 1, "awaiting_receipt": 1})

    def test_the_price_break_carries_both_figures(self) -> None:
        x = self._only("over_order")
        self.assertEqual((x["po_id"], x["voucher_id"]),
                         ("PO2002", "VCHR2002"))
        self.assertEqual(x["vouchered_amt"], 5750.0)
        self.assertEqual(x["ordered_amt"], 5000.0)
        self.assertEqual(x["over_by"], 750.0)

    def test_the_qty_break_prices_what_never_arrived(self) -> None:
        x = self._only("not_received")
        self.assertEqual(x["po_id"], "PO2003")
        self.assertEqual(x["vouchered_qty"], 50.0)
        self.assertEqual(x["received_qty"], 30.0)
        # 20 undelivered units at the vouchered 120.00
        self.assertEqual(x["not_received_amt"], 2400.0)

    def test_a_voucher_with_no_receipt_is_its_own_kind(self) -> None:
        x = self._only("no_receipt")
        self.assertEqual((x["po_id"], x["voucher_id"]),
                         ("PO2004", "VCHR2004"))
        self.assertEqual(x["vouchered_amt"], 12000.0)

    def test_a_receipt_never_invoiced_is_aged(self) -> None:
        x = self._only("never_invoiced")
        self.assertEqual(x["po_id"], "PO2005")
        self.assertEqual(x["received_amt"], 2400.0)
        # received 2026-06-28, asked as of 2026-08-14
        self.assertEqual(x["days_since_receipt"], 47)

    def test_a_canceled_order_is_never_awaiting_receipt(self) -> None:
        x = self._only("awaiting_receipt")
        self.assertEqual(x["po_id"], "PO2007",
                         "PO2006 differs from PO2007 only by PO_STATUS=X — "
                         "reporting it as late is the trap this feature "
                         "exists to avoid")

    def test_the_clean_chain_raises_nothing(self) -> None:
        every = [x.get("po_id") for rows in self.out["exceptions"].values()
                 for x in rows]
        self.assertNotIn("PO2001", every)

    def test_the_two_verdicts_are_reported_side_by_side(self) -> None:
        flags = {f["status"]: f["vouchers"]
                 for f in self.out["system_match_flags"]["counts"]}
        self.assertEqual(flags, {"E": 3, "T": 1})
        self.assertIn("recomputed", self.out["system_match_flags"]["note"])
        x = self._only("over_order")
        self.assertEqual(x["system_match_status"], "E",
                         "each exception also shows the system's own flag")

    def test_totals_are_per_currency_and_per_kind(self) -> None:
        t = self.out["totals"]
        self.assertEqual(t["over_order"], [{"currency": "USD",
                                            "amount": 750.0}])
        self.assertEqual(t["never_invoiced"], [{"currency": "USD",
                                                "amount": 2400.0}])

    def test_the_population_is_stated(self) -> None:
        self.assertIn("no PO reference are outside the match",
                      self.out["population"])


class ChainTests(unittest.TestCase):
    def test_by_po_the_chain_ties_out_per_stage(self) -> None:
        out = _proc().procurement_chain(reference="PO2001",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["resolved"]["kind"], "po")
        self.assertEqual(out["breaks"], [], "the clean chain has no break")
        t = out["chain_totals"][0]
        self.assertEqual((t["ordered"], t["received"],
                          t["vouchered"], t["paid"]),
                         (8500.0, 8500.0, 8500.0, 8500.0))
        self.assertTrue(t.get("sum_only"),
                        "stage totals are sums; the guard must not demand "
                        "a row for them")

    def test_by_voucher_resolves_to_its_order(self) -> None:
        out = _proc().procurement_chain(reference="VCHR2003",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["resolved"], {"kind": "voucher",
                                           "id": "VCHR2003"})
        self.assertEqual([o["po_id"] for o in out["orders"]], ["PO2003"])
        self.assertEqual([b["kind"] for b in out["breaks"]],
                         ["not_received"])

    def test_by_receipt_resolves_to_its_order(self) -> None:
        out = _proc().procurement_chain(reference="RECV3004",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["resolved"]["kind"], "receipt")
        self.assertEqual([o["po_id"] for o in out["orders"]], ["PO2005"])
        self.assertEqual([b["kind"] for b in out["breaks"]],
                         ["never_invoiced"])

    def test_by_supplier_name_resolves_and_discloses(self) -> None:
        out = _proc().procurement_chain(reference="Summit Machining",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["resolved"]["id"], "V1009")
        self.assertEqual(len(out["orders"]), 7)
        self.assertTrue(any("Read 'Summit Machining' as supplier" in n
                            for n in out["record_notes"]))
        self.assertIn("get_vendor_payables_network", out["next_steps"][0])

    def test_the_canceled_order_is_labeled_in_the_chain(self) -> None:
        out = _proc().procurement_chain(reference="PO2006",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertIn("CANCELED", out["orders"][0]["note"])
        self.assertEqual(out["breaks"], [])

    def test_an_unknown_reference_is_no_data_not_zero(self) -> None:
        out = _proc().procurement_chain(reference="PO9999",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["scope_status"], "reference_not_found")
        self.assertIn("NO DATA", out["detail"])

    def test_a_blank_reference_asks_for_one(self) -> None:
        out = _proc().procurement_chain(business_unit=BU)
        self.assertEqual(out["scope_status"], "reference_required")

    def test_an_ambiguous_supplier_asks_which(self) -> None:
        out = _proc().procurement_chain(reference="Ridgeline",
                                        business_unit=BU, as_of_date=ASOF)
        self.assertEqual(out["scope_status"], "ambiguous_supplier")
        self.assertGreater(len(out["multiple_matches"]), 1)


class DegradeTests(unittest.TestCase):
    """Sites differ; each missing record costs its own layer, with a note."""

    def test_no_voucher_lines_means_UNKNOWN_never_no_exceptions(self) -> None:
        class NoVoucherLines(Database):
            def columns(self, table):
                return (set() if table.upper() == "PS_VOUCHER_LINE"
                        else super().columns(table))

        out = _proc(NoVoucherLines).match_exceptions(business_unit=BU,
                                                     as_of_date=ASOF)
        self.assertFalse(out["supported"])
        self.assertIn("UNKNOWN", out["detail"])
        self.assertNotIn("exceptions", out)

    def test_no_receipts_degrades_to_two_way_with_a_note(self) -> None:
        class NoReceipts(Database):
            def columns(self, table):
                return (set() if table.upper().startswith("PS_RECV")
                        else super().columns(table))

        out = _proc(NoReceipts).match_exceptions(business_unit=BU,
                                                 as_of_date=ASOF)
        self.assertTrue(out["supported"])
        # The price check still runs; every receipt-dependent kind is empty
        # rather than exploding into false no-receipt findings.
        self.assertEqual(out["counts"]["over_order"], 1)
        self.assertEqual(out["counts"]["no_receipt"], 0)
        self.assertEqual(out["counts"]["never_invoiced"], 0)
        self.assertTrue(any("two-way" in n for n in out["record_notes"]))

    def test_no_schedules_leaves_ordered_amounts_unknown(self) -> None:
        class NoScheds(Database):
            def columns(self, table):
                return (set() if table.upper() == "PS_PO_LINE_SHIP"
                        else super().columns(table))

        out = _proc(NoScheds).match_exceptions(business_unit=BU,
                                               as_of_date=ASOF)
        self.assertEqual(out["counts"]["over_order"], 0,
                         "missing order amounts must not read as zero and "
                         "flag every voucher as over the order")
        self.assertTrue(any("ordered amounts are unknown" in n
                            for n in out["record_notes"]))

    def test_no_po_at_all_is_a_module_refusal(self) -> None:
        class NoPO(Database):
            def columns(self, table):
                return (set() if table.upper() == "PS_PO_HDR"
                        else super().columns(table))

        with self.assertRaises(ModuleError):
            _proc(NoPO).match_exceptions(business_unit=BU)


class SeqSpellingTests(unittest.TestCase):
    def test_the_deferred_column_question_is_probed_not_assumed(self) -> None:
        # RECV_SHIP_SEQ_NBR vs RECV_SHP_SEQ_NBR deferred this feature once.
        pr = _proc()
        self.assertEqual(pr._seq_col(), "RECV_SHIP_SEQ_NBR")

        class Abbreviated(Database):
            def columns(self, table):
                cols = super().columns(table)
                if table.upper() == "PS_RECV_LN_SHIP":
                    cols = {("RECV_SHP_SEQ_NBR" if c == "RECV_SHIP_SEQ_NBR"
                             else c) for c in cols}
                return cols

        self.assertEqual(_proc(Abbreviated)._seq_col(), "RECV_SHP_SEQ_NBR")


class SuggestionTests(unittest.TestCase):
    def test_the_biggest_break_points_at_its_chain(self) -> None:
        from pstb.suggest import suggestions_for
        payload = _proc().match_exceptions(business_unit=BU,
                                           as_of_date=ASOF)
        out = suggestions_for([("get_match_exceptions", payload)])
        self.assertTrue(out)
        s = out[0]
        self.assertEqual(s["answered_by"], "get_procurement_chain")
        # no_receipt's 12,000 vouchered outranks the 750 price break only in
        # amount; over_order outranks it in KIND — money already out the
        # door beats money merely unaccrued.
        self.assertIn("PO2002", s["question"])
        self.assertIn("750", s["because"].replace(",", ""))

    def test_a_refusal_payload_suggests_nothing(self) -> None:
        from pstb.suggest import suggestions_for
        out = suggestions_for([("get_match_exceptions",
                                {"supported": False, "detail": "x"})])
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
