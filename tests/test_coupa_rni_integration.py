"""Cross-cutting contracts for the Coupa-authority RNI candidate path."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BUSINESS_TIMEZONE = "UTC"
CURRENT_DATE = dt.datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date().isoformat()

from pstb.client.chat import agent_turn  # noqa: E402
from pstb.client.llm_base import LLMResponse, ToolCall  # noqa: E402
from pstb.client.llm_gemini import routing_tool_names  # noqa: E402
from pstb.config import load_config  # noqa: E402
from pstb.guards import (  # noqa: E402
    FINANCIAL_EVIDENCE_TOOLS,
    _TOOL_DOMAINS,
    _TOOL_SCOPE_ARGS,
    financial_result_domains,
    financial_tool_is_relevant,
    question_financial_domains,
    tool_result_status,
)


def _coupa_payload(*, mode: str = "live") -> dict:
    return {
        "source": "coupa", "mode": mode,
        "status": "evaluated", "evaluated": True,
        "conclusion": "po_linked_candidates_present",
        "business_unit": "US001", "as_of_date": CURRENT_DATE,
        "min_amount": 0.0,
        "coverage": {
            "classification": "coupa_po_linked_event_review_only",
            "cutoff_classification": "current_date_only",
            "current_date": CURRENT_DATE,
            "business_timezone": BUSINESS_TIMEZONE,
            "current_date_basis": "configured_coupa_company_timezone",
            "all_grni_complete": False,
            "collection_complete": True,
            "point_in_time_complete": False,
            "business_unit_complete": True,
            "matching_precision": "order_line_aggregate",
            "invoice_scope_order_line_invariant": True,
            "coupa_business_unit": "US_CORP",
            "business_unit_mapping_basis": "configured_business_unit_map",
            "server_side_filters": {
                "receipts": "account[segment_1]",
                "invoices": "invoice_lines[account][segment_1]",
            },
        },
        "scope": {"business_unit": "US001",
                  "coupa_business_unit": "US_CORP",
                  "mapping_basis": "configured_business_unit_map",
                  "business_unit_path": "account.segment-1",
                  "business_timezone": BUSINESS_TIMEZONE},
        "candidate_basis": {
            "classification": "review_candidate_only",
            "eligible_invoice_statuses": ["approved"],
        },
        "booked_status": "not_evaluated",
        "snapshot": {"classification": "current_api_collection",
                     "complete": False, "collection_complete": True,
                     "atomic": False, "as_of": CURRENT_DATE,
                     "business_timezone": BUSINESS_TIMEZONE},
        "pagination": {
            "receipts": {"complete": True, "truncated": False,
                         "rows_returned": 1},
            "invoices": {"complete": True, "truncated": False,
                         "rows_returned": 1},
        },
        "population": {"complete": True, "truncated": False,
                       "totals_complete": True,
                       "candidate_count": 1,
                       "positive_candidate_count": 1,
                       "displayed_candidate_count": 1,
                       "display_truncated": False,
                       "display_row_cap": 200,
                       "receipt_events_in_scope": 1},
        "count": 1,
        "totals_by_currency": {"USD": 75.0},
        "all_positive_candidate_totals_by_currency": {"USD": 75.0},
        "lines": [{
            "business_unit": "US001", "coupa_business_unit": "US_CORP",
            "order_line_id": "901",
            "line_type": "OrderAmountLine",
            "currency": "USD", "matching_precision": "order_line_aggregate",
            "net_receipt_amount": 100.0,
            "net_receipt_value_at_receipt_valuation": 100.0,
            "net_receipt_face_amount": 100.0,
            "receipt_face_to_valuation_difference": 0.0,
            "net_receipt_valuation_basis": (
                "Coupa receiving-transaction face total"),
            "eligible_invoice_amount": 25.0,
            "rni_candidate_amount": 75.0,
            "receipt_transaction_count": 1,
            "first_receipt_date": CURRENT_DATE,
            "last_receipt_date": CURRENT_DATE,
            "receipt_transaction_ids": ["R-1"],
            "receipt_id_evidence_displayed_count": 1,
            "receipt_id_evidence_truncated": False,
            "eligible_invoice_line_count": 1,
            "eligible_invoice_lines": [{
                "invoice_id": "I-1",
                "invoice_line_id": "IL-1",
                "order_line_id": "901",
                "currency": "USD",
                "header_status": "approved",
                "canceled": False,
                "candidate_valuation_amount": 25.0,
                "created_at": CURRENT_DATE,
            }],
            "eligible_invoice_evidence_displayed_count": 1,
            "eligible_invoice_evidence_truncated": False,
        }],
        "exceptions": {
            "invoice_present_not_eligible": [],
            "over_invoiced": [],
            "net_credit_invoice_activity": [],
            "excluded_receiving_types": [],
            "counts": {"invoice_present_not_eligible": 0,
                       "over_invoiced": 0,
                       "net_credit_invoice_activity": 0,
                       "excluded_receiving_types": 0},
            "display_truncated": {
                "invoice_present_not_eligible": False,
                "over_invoiced": False,
                "net_credit_invoice_activity": False,
                "excluded_receiving_types": False,
            },
        },
        "observed": {
            "net_receipts_by_currency": {"USD": 100.0},
            "net_receipt_values_at_receipt_valuation_by_currency": {
                "USD": 100.0},
            "net_receipt_face_totals_by_currency": {"USD": 100.0},
            "receipt_face_to_valuation_differences_by_currency": {
                "USD": 0.0},
            "candidate_totals_by_currency": {"USD": 75.0},
        },
        "export_evidence": {
            "evaluated": True, "complete": True,
            "receipt_transaction_count": 1,
            "displayed_receipt_transaction_count": 1,
            "display_truncated": False,
            "exported_receipt_transactions": 1,
            "not_exported_receipt_transactions": 0,
            "unknown_export_receipt_transactions": 0,
            "invalid_last_exported_at_transactions": 0,
            "receipt_transactions": [{
                "receipt_transaction_id": "R-1",
                "order_line_id": "901",
                "type": "InventoryReceipt",
                "transaction_date": CURRENT_DATE,
                "exported": True,
                "last_exported_at": "2026-08-17T12:00:00Z",
                "last_exported_at_valid": True,
            }],
        },
    }


def _ps_payload() -> dict:
    return {
        "status": "evaluated", "evaluated": True,
        "conclusion": "po_linked_candidates_present",
        "coverage": {
            "classification": "po_linked_document_review_only",
            "all_grni_complete": False, "point_in_time_complete": True,
        },
        "candidate_basis": {"classification": "review_candidate_only"},
        "booked_status": "not_evaluated",
        "population": {"complete": True, "truncated": False,
                       "candidate_count": 1},
        "totals_by_currency": {"USD": 75.0},
        "lines": [{"candidate_amount": 75.0}],
    }


class CoupaRoutingTests(unittest.TestCase):
    def test_source_specific_domains_never_cross(self):
        coupa = (
            "Which current Coupa PO lines have net receipt activity above "
            "eligible invoice coverage?")
        ps = (
            "Which current PO-linked PeopleSoft received-not-invoiced items "
            "should we review today?")
        self.assertEqual(question_financial_domains(coupa),
                         {"coupa_rni_candidates"})
        self.assertEqual(question_financial_domains(ps),
                         {"po_grni_candidates"})
        self.assertTrue(financial_tool_is_relevant("get_coupa_rni", coupa))
        self.assertFalse(financial_tool_is_relevant(
            "get_po_grni_candidates", coupa))
        self.assertTrue(financial_tool_is_relevant(
            "get_po_grni_candidates", ps))
        self.assertFalse(financial_tool_is_relevant("get_coupa_rni", ps))

    def test_booked_broad_and_allocation_questions_are_not_covered(self):
        for question in (
            "What GRNI is booked today?",
            "Do we have any GRNI?",
            "What receipt-accrual liability posted to GL?",
        ):
            self.assertFalse(financial_tool_is_relevant(
                "get_coupa_rni", question), question)
        scoped = "Which Coupa RNI review candidates are for account 6000?"
        self.assertIn("rni_allocation", question_financial_domains(scoped))
        self.assertFalse(financial_tool_is_relevant("get_coupa_rni", scoped))

    def test_cross_system_and_booking_aliases_fail_closed(self):
        posting_questions = (
            "Did Coupa receipts interface to PeopleSoft?",
            "Were exported Coupa receipts received by PeopleSoft?",
            "Did all Coupa invoices make it to AP?",
            "Did everything approved in Coupa land in AP?",
            "Did every approved Coupa invoice through June become a "
            "PeopleSoft voucher?",
            "Did every approved Coupa invoice turn into a PeopleSoft voucher?",
            "Which Coupa invoices never became vouchers?",
            "Did every Coupa invoice become a voucher?",
            "Which approved Coupa invoices are missing vouchers?",
            "Did Coupa invoices get into AP?",
            "Did Coupa send receipts to PeopleSoft?",
            "Did Coupa receiving transactions reach PeopleSoft?",
            "Did exported Coupa receiving transactions arrive in PeopleSoft?",
            "Did Coupa receiving transactions make it into ERP?",
            "Were Coupa receipt events booked in Oracle?",
            "Did Coupa invoices interface to the finance system?",
            "Did Coupa receipts post to the accounting system?",
        )
        for question in posting_questions:
            self.assertEqual(question_financial_domains(question),
                             {"coupa_to_ps_posting"}, question)
            self.assertFalse(financial_tool_is_relevant(
                "coupa_to_ap_tie", question), question)
        for question in (
            "Are these Coupa receipt candidates booked?",
            "How much should we accrue for Coupa receipts not invoiced?",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"grni_booked"}, question)
            self.assertFalse(financial_tool_is_relevant(
                "get_coupa_rni", question), question)

    def test_current_coupa_tie_is_diagnostic_not_financial_evidence(self):
        self.assertNotIn("coupa_to_ap_tie", FINANCIAL_EVIDENCE_TOOLS)
        self.assertEqual(_TOOL_DOMAINS["coupa_to_ap_tie"], set())

    def test_scope_and_financial_domains_are_narrow(self):
        self.assertEqual(_TOOL_SCOPE_ARGS["get_coupa_rni"], {
            "business_unit": "business_unit", "as_of_date": "as_of_date"})
        self.assertEqual(_TOOL_DOMAINS["get_coupa_rni"], {
            "coupa_rni_candidates", "coupa_receipt_export_state"})

    def test_export_flag_state_is_distinct_from_erp_delivery(self):
        for question in (
            "Which Coupa receipts are flagged exported?",
            "Were Coupa receipts exported?",
            "Are Coupa receipts exported?",
            "How many Coupa receipts were exported?",
            "List unexported Coupa receipts",
            "Did Coupa export the receipts?",
            "Which Coupa receipt events are flagged exported?",
            "List Coupa return events not exported",
            "Which Coupa returns are not exported?",
            "Which Coupa voids are flagged exported?",
            "Which Coupa void events have export flags?",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"coupa_receipt_export_state"})
            self.assertTrue(financial_tool_is_relevant(
                "get_coupa_rni", question))
        self.assertEqual(question_financial_domains(
            "Did the Coupa receipt export succeed?"),
            {"coupa_export_delivery"})
        self.assertFalse(financial_tool_is_relevant(
            "get_coupa_rni", "Did the Coupa receipt export succeed?"))
        for question in (
            "Did Coupa successfully export receipts?",
            "Did Coupa export receipts successfully?",
            "Did the Coupa receiving export succeed?",
            "Did Coupa receipt event export fail?",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"coupa_export_delivery"}, question)
        self.assertEqual(question_financial_domains(
            "Which Coupa approved invoices are missing in AP?"),
            {"coupa_to_ps_posting"})
        for question in (
            "Which Coupa receiving transactions are exported?",
            "Are all Coupa receiving transactions exported?",
            "List Coupa receiving transactions not exported",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"coupa_receiving_export_population"}, question)
            self.assertFalse(financial_tool_is_relevant(
                "get_coupa_rni", question), question)
        for question in (
            "Was Coupa receipt R123 exported?",
            "When was R123 last exported?",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"coupa_receipt_export_detail"}, question)
            self.assertFalse(financial_tool_is_relevant(
                "get_coupa_rni", question), question)

    def test_common_coupa_candidate_and_booking_wording_is_not_generic_ap(self):
        for question in (
            "Show Coupa GRNI candidates",
            "How much received value lacks approved invoices in Coupa?",
            "Which Coupa PO lines are partially invoiced?",
            "Are all current Coupa PO lines fully invoiced?",
            "Which Coupa PO lines are not fully invoiced?",
            "Which Coupa PO lines have receipts not covered by invoices?",
            "Which Coupa purchase order lines have unmatched receipts?",
            "Which Coupa PO lines have received value above eligible invoice coverage?",
            "Show Coupa PO-line candidates",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"coupa_rni_candidates"}, question)
            self.assertTrue(financial_tool_is_relevant(
                "get_coupa_rni", question), question)
        for question in (
            "What should I accrue for Coupa receipts?",
            "Which Coupa receipts need accrual?",
            "How much do we need to accrue for receipts?",
            "Book the Coupa RNI candidates",
            "Prepare an accrual for Coupa receipts without invoices",
        ):
            # A booking DECISION is its own domain: no candidate control
            # authorizes it, and the refusal now names the missing evidence
            # rather than claiming the database returned nothing.
            self.assertEqual(question_financial_domains(question),
                             {"grni_booked"}, question)
            self.assertFalse(financial_tool_is_relevant(
                "get_coupa_rni", question), question)

    def test_receipt_level_invoice_attribution_requires_matching_allocations(self):
        for question in (
            "Have all Coupa receipts been invoiced?",
            "What Coupa receipts are received but not invoiced?",
            "What Coupa receipts are uninvoiced?",
            "Are Coupa receipts under-invoiced?",
            "Are Coupa receipts fully invoiced?",
            "Which Coupa receipts are not an invoice?",
            "Which Coupa receipts are missing an invoice?",
            "Which Coupa receipts have not been invoiced?",
            "Are any Coupa receipts unmatched to an invoice?",
            "Show Coupa receipts without invoices",
            "Do all Coupa receipts have an approved invoice?",
            "Did every Coupa receipt match an invoice?",
            "Show Coupa receipt-to-invoice matching",
            "Which current Coupa receipt events need accrual review because "
            "no eligible invoice event covers them?",
            "Which specific Coupa receipts are not covered by approved invoices?",
            "Which Coupa receipts are not covered by approved invoices?",
            "Which individual Coupa receipt is uninvoiced?",
            "List individual Coupa receipts with no invoice match",
            "For receipt R123, which invoice covers it?",
            "Is Coupa receipt R123 covered by an invoice?",
            "Is Coupa receipt R123 invoiced?",
            "Does Coupa receipt R123 have an invoice?",
            "Has Coupa receipt R123 been invoiced?",
            "Which invoice corresponds to Coupa receipt R123?",
            "What is the invoice status for Coupa receipt R123?",
            "Show invoice details for Coupa receipt R123",
            "Which invoice is for receipt 123?",
            "How much of receipt R123 is covered by invoices?",
            "What receipt events in Coupa are not matched to invoices?",
            "Are Coupa receipts matched to invoices?",
            "Are these Coupa receipts covered by approved invoices?",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"rni_receipt_matching"}, question)
            self.assertFalse(financial_tool_is_relevant(
                "get_coupa_rni", question), question)

    def test_currency_grouping_is_not_an_fx_conversion(self):
        for question in (
            "Show Coupa PO lines with net receipt activity above eligible "
            "invoice coverage at the selected period end, by currency.",
            "Show Coupa PO-line candidates separately for each currency",
        ):
            self.assertEqual(question_financial_domains(question),
                             {"coupa_rni_candidates"}, question)
            self.assertTrue(financial_tool_is_relevant(
                "get_coupa_rni", question), question)
        converted = "Convert current Coupa PO-line candidates to USD"
        self.assertEqual(question_financial_domains(converted),
                         {"coupa_rni_candidates", "fx"})
        self.assertFalse(financial_tool_is_relevant(
            "get_coupa_rni", converted))

    def test_gemini_shortlist_obeys_explicit_and_configured_authority(self):
        available = {
            "get_coupa_rni", "get_po_grni_candidates",
            "get_match_exceptions", "get_coupa_invoices", "run_playbook", "run_sql",
            "search_metadata", "get_metadata_context", "resolve_period",
            "list_financial_scopes", "list_business_units", "list_ledgers",
            "list_periods", "detect_transaction_anomalies",
        }
        explicit = set(routing_tool_names(
            "Which Coupa receipts are RNI review candidates today?", available,
            procurement_authority="peoplesoft"))
        self.assertIn("get_coupa_rni", explicit)
        self.assertNotIn("get_po_grni_candidates", explicit)
        generic = set(routing_tool_names(
            "Which current received-not-invoiced items should we review?",
            available, procurement_authority="coupa"))
        self.assertIn("get_coupa_rni", generic)
        self.assertNotIn("get_po_grni_candidates", generic)
        matching = set(routing_tool_names(
            "Which invoice corresponds to Coupa receipt R123?", available,
            procurement_authority="coupa"))
        for unsupported in (
                "get_coupa_rni", "get_po_grni_candidates",
                "get_coupa_invoices"):
            self.assertNotIn(unsupported, matching)
        aggregate = set(routing_tool_names(
            "Are all current Coupa PO lines fully invoiced?", available,
            procurement_authority="coupa"))
        self.assertIn("get_coupa_rni", aggregate)
        self.assertNotIn("get_po_grni_candidates", aggregate)
        booked = set(routing_tool_names(
            "What booked receipt-accrual liability posted to GL?", available,
            procurement_authority="coupa"))
        self.assertNotIn("get_coupa_rni", booked)
        self.assertNotIn("get_po_grni_candidates", booked)


class CoupaEvidenceTests(unittest.TestCase):
    """Payloads from the CONTROL module's connector, which has its own zone.

    This module's CURRENT_DATE is derived from BUSINESS_TIMEZONE ("UTC"),
    but the connector() helper borrowed below is built with COUPA_TZ
    ("America/New_York"). The gate compares as_of_date against the current
    date in the CONNECTOR's zone, so passing this module's UTC date failed
    for the four hours each evening when the two calendars disagree — a
    test that was red between 20:00 and midnight New York time and green
    the rest of the day. Derive the cut-off from the zone that will judge
    it.
    """

    @staticmethod
    def _connector_today() -> str:
        from tests.test_coupa_rni_control import COUPA_TZ
        return dt.datetime.now(ZoneInfo(COUPA_TZ)).date().isoformat()

    def test_actual_connector_payload_satisfies_the_strict_candidate_gate(self):
        from tests.test_coupa_rni_control import (connector, invoice,
                                                   invoice_line, receipt)
        control, _ = connector(
            [receipt(1, amount="100")],
            [invoice(1, status="approved",
                     lines=[invoice_line(11, amount="25")])])
        cutoff = self._connector_today()
        payload = control.received_not_invoiced(
            business_unit="US001", as_of_date=cutoff,
            today=dt.date.fromisoformat(cutoff))
        ok, why = tool_result_status("get_coupa_rni", json.dumps(payload))
        self.assertTrue(ok, why)

    def test_source_precision_is_preserved_through_the_strict_gate(self):
        from tests.test_coupa_rni_control import connector, receipt, run

        receipts = [
            receipt(index, amount="0.0000006", quantity="0.6",
                    price="0.000001", line_id=100 + index,
                    line_type="OrderQuantityLine")
            for index in range(1, 5)
        ]
        control, _ = connector(receipts, [])
        payload = run(control)
        self.assertEqual(payload["totals_by_currency"], {"USD": 0.0000024})
        self.assertEqual(len(payload["lines"]), 4)
        self.assertTrue(all(row["rni_candidate_amount"] == 0.0000006
                            for row in payload["lines"]))
        ok, why = tool_result_status("get_coupa_rni", json.dumps(payload))
        self.assertTrue(ok, why)

    def test_only_complete_live_structured_population_is_evidence(self):
        ok, why = tool_result_status("get_coupa_rni", json.dumps(
            _coupa_payload()))
        self.assertTrue(ok, why)
        for content in ("plain text", "[]"):
            self.assertFalse(tool_result_status("get_coupa_rni", content)[0])
        fixture = _coupa_payload(mode="fixtures")
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(fixture))[0])
        incomplete = _coupa_payload()
        incomplete["pagination"]["receipts"]["complete"] = False
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(incomplete))[0])

    def test_current_only_cutoff_cannot_be_relabelled_historical(self):
        payload = _coupa_payload()
        payload["as_of_date"] = "2026-06-30"
        payload["coverage"]["current_date"] = "2026-06-30"
        payload["snapshot"]["as_of"] = "2026-06-30"
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(payload))[0])
        for field, bad in (
            ("missing", None),
            ("invalid", "Not/AZone"),
            ("mismatch", "America/New_York"),
        ):
            payload = _coupa_payload()
            payload["coverage"]["business_timezone"] = bad
            self.assertFalse(tool_result_status(
                "get_coupa_rni", json.dumps(payload))[0], field)

    def test_amounts_and_full_totals_are_cross_checked(self):
        for bad in ("n/a", None, True, float("nan"), float("inf")):
            payload = _coupa_payload()
            payload["lines"][0]["rni_candidate_amount"] = bad
            self.assertFalse(tool_result_status(
                "get_coupa_rni", json.dumps(payload))[0], repr(bad))
        mismatch = _coupa_payload()
        mismatch["totals_by_currency"]["USD"] = 74.0
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(mismatch))[0])

    def test_candidate_rows_require_bounded_receipt_and_invoice_provenance(self):
        mutations = []
        missing_receipts = _coupa_payload()
        missing_receipts["lines"][0].pop("receipt_transaction_ids")
        mutations.append(missing_receipts)
        negative_count = _coupa_payload()
        negative_count["lines"][0]["receipt_transaction_count"] = -5
        mutations.append(negative_count)
        future_receipt = _coupa_payload()
        future_receipt["lines"][0]["last_receipt_date"] = "2099-01-01"
        mutations.append(future_receipt)
        reversed_dates = _coupa_payload()
        reversed_dates["lines"][0].update({
            "first_receipt_date": CURRENT_DATE,
            "last_receipt_date": "2020-01-01",
        })
        mutations.append(reversed_dates)
        missing_invoice_evidence = _coupa_payload()
        missing_invoice_evidence["lines"][0].update({
            "eligible_invoice_line_count": 99,
            "eligible_invoice_lines": [],
            "eligible_invoice_evidence_displayed_count": 0,
            "eligible_invoice_evidence_truncated": True,
        })
        mutations.append(missing_invoice_evidence)
        wrong_invoice_scope = _coupa_payload()
        wrong_invoice_scope["lines"][0]["eligible_invoice_lines"][0][
            "order_line_id"] = "OTHER"
        mutations.append(wrong_invoice_scope)
        ineligible_header = _coupa_payload()
        ineligible_header["lines"][0]["eligible_invoice_lines"][0][
            "header_status"] = "pending_approval"
        mutations.append(ineligible_header)
        for payload in mutations:
            self.assertFalse(tool_result_status(
                "get_coupa_rni", json.dumps(payload))[0])

    def test_complete_totals_survive_bounded_display(self):
        payload = _coupa_payload()
        payload["population"].update({
            "candidate_count": 205,
            "positive_candidate_count": 205,
            "displayed_candidate_count": 1,
            "display_truncated": True,
            "display_row_cap": 1,
        })
        payload["count"] = 205
        payload["totals_by_currency"] = {"USD": 7500.123456}
        payload["all_positive_candidate_totals_by_currency"] = {
            "USD": 7500.123456}
        payload["observed"]["candidate_totals_by_currency"] = {
            "USD": 7500.123456}
        ok, why = tool_result_status("get_coupa_rni", json.dumps(payload))
        self.assertTrue(ok, why)

        payload["totals_by_currency"] = {"USD": 74.999999}
        payload["all_positive_candidate_totals_by_currency"] = {
            "USD": 7500.123456}
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(payload))[0])

    def test_row_arithmetic_scope_and_exception_claims_are_cross_checked(self):
        mutations = []
        wrong_math = _coupa_payload()
        wrong_math["lines"][0]["eligible_invoice_amount"] = 24.0
        mutations.append(wrong_math)
        wrong_bu = _coupa_payload()
        wrong_bu["lines"][0]["coupa_business_unit"] = "CA_CORP"
        mutations.append(wrong_bu)
        wrong_scope = _coupa_payload()
        wrong_scope["scope"]["business_unit"] = "CA001"
        mutations.append(wrong_scope)
        visible_hidden_exception = _coupa_payload()
        visible_hidden_exception["exceptions"]["over_invoiced"] = [
            {"difference": -1.0}]
        mutations.append(visible_hidden_exception)
        negative_inputs = _coupa_payload()
        negative_inputs["lines"][0].update({
            "net_receipt_amount": -100.0,
            "net_receipt_value_at_receipt_valuation": -100.0,
            "eligible_invoice_amount": -175.0,
        })
        mutations.append(negative_inputs)
        conflicting_alias = _coupa_payload()
        conflicting_alias["lines"][0]["rni_amt"] = 999999.0
        mutations.append(conflicting_alias)
        conflicting_coverage_alias = _coupa_payload()
        conflicting_coverage_alias["lines"][0][
            "eligible_invoice_coverage_at_receipt_valuation"] = 999999.0
        mutations.append(conflicting_coverage_alias)
        conflicting_receipt_alias = _coupa_payload()
        conflicting_receipt_alias["lines"][0][
            "net_receipt_value_at_receipt_valuation"] = 99.0
        mutations.append(conflicting_receipt_alias)
        conflicting_face_difference = _coupa_payload()
        conflicting_face_difference["lines"][0][
            "receipt_face_to_valuation_difference"] = 1.0
        mutations.append(conflicting_face_difference)
        conflicting_observed_alias = _coupa_payload()
        conflicting_observed_alias["observed"][
            "net_receipt_face_totals_by_currency"] = {"USD": 101.0}
        mutations.append(conflicting_observed_alias)
        for payload in mutations:
            self.assertFalse(tool_result_status(
                "get_coupa_rni", json.dumps(payload))[0])

    def test_display_population_and_total_aliases_cannot_conflict(self):
        payload = _coupa_payload()
        payload["population"].update({
            "candidate_count": 205, "positive_candidate_count": 205,
            "displayed_candidate_count": 0, "display_truncated": True,
        })
        payload["count"] = 205
        payload["lines"] = []
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(payload))[0])

        payload = _coupa_payload()
        payload["rni_totals_by_currency"] = {"USD": 999999.0}
        self.assertFalse(tool_result_status(
            "get_coupa_rni", json.dumps(payload))[0])

    def test_quantity_candidate_requires_all_quantity_valuation_invariants(self):
        payload = _coupa_payload()
        row = payload["lines"][0]
        row.update({
            "line_type": "OrderQuantityLine",
            "net_receipt_amount": 100.0,
            "net_receipt_value_at_receipt_valuation": 100.0,
            "net_receipt_face_amount": 100.01,
            "receipt_face_to_valuation_difference": 0.01,
            "net_receipt_valuation_basis": (
                "net quantity times single proven receipt price"),
            "eligible_invoice_amount": 40.0,
            "rni_candidate_amount": 60.0,
            "net_receipt_quantity": 10.0,
            "eligible_invoice_quantity": 4.0,
            "remaining_quantity": 6.0,
            "valuation_unit_price": 10.0,
        })
        payload["totals_by_currency"] = {"USD": 60.0}
        payload["all_positive_candidate_totals_by_currency"] = {"USD": 60.0}
        payload["observed"].update({
            "net_receipt_face_totals_by_currency": {"USD": 100.01},
            "receipt_face_to_valuation_differences_by_currency": {
                "USD": 0.01},
            "candidate_totals_by_currency": {"USD": 60.0},
        })
        self.assertTrue(tool_result_status(
            "get_coupa_rni", json.dumps(payload))[0])
        for field, bad in (
            ("remaining_quantity", 7.0),
            ("net_receipt_amount", 99.0),
            ("eligible_invoice_amount", 39.0),
        ):
            broken = json.loads(json.dumps(payload))
            broken["lines"][0][field] = bad
            self.assertFalse(tool_result_status(
                "get_coupa_rni", json.dumps(broken))[0], field)

    def test_export_domain_requires_complete_consistent_source_flags(self):
        payload = _coupa_payload()
        domains = financial_result_domains(
            "get_coupa_rni", json.dumps(payload))
        self.assertIn("coupa_receipt_export_state", domains)
        payload["export_evidence"]["unknown_export_receipt_transactions"] = 1
        self.assertNotIn("coupa_receipt_export_state",
                         financial_result_domains(
                             "get_coupa_rni", json.dumps(payload)))
        for mutation in ("wrong_flag", "bad_date", "duplicate"):
            payload = _coupa_payload()
            row = payload["export_evidence"]["receipt_transactions"][0]
            if mutation == "wrong_flag":
                row["exported"] = False
            elif mutation == "bad_date":
                row["transaction_date"] = "2099-99-99"
            else:
                payload["export_evidence"]["receipt_transactions"].append(
                    dict(row))
                payload["export_evidence"][
                    "displayed_receipt_transaction_count"] = 2
            self.assertNotIn(
                "coupa_receipt_export_state",
                financial_result_domains(
                    "get_coupa_rni", json.dumps(payload)), mutation)
        payload = _coupa_payload()
        payload["export_evidence"]["receipt_transactions"] = []
        self.assertNotIn("coupa_receipt_export_state",
                         financial_result_domains(
                             "get_coupa_rni", json.dumps(payload)))
        payload = _coupa_payload()
        payload["export_evidence"].update({
            "receipt_transaction_count": 2,
            "displayed_receipt_transaction_count": 1,
            "display_truncated": True,
            "exported_receipt_transactions": 2,
        })
        payload["population"]["receipt_events_in_scope"] = 2
        self.assertNotIn("coupa_receipt_export_state",
                         financial_result_domains(
                             "get_coupa_rni", json.dumps(payload)))
        for mutation in (
            "invalid_count", "missing_validity", "false_validity",
            "naive_timestamp", "blank_timestamp",
        ):
            payload = _coupa_payload()
            export = payload["export_evidence"]
            row = export["receipt_transactions"][0]
            if mutation == "invalid_count":
                export["invalid_last_exported_at_transactions"] = 1
            elif mutation == "missing_validity":
                row.pop("last_exported_at_valid")
            elif mutation == "false_validity":
                row["last_exported_at_valid"] = False
            elif mutation == "naive_timestamp":
                row["last_exported_at"] = "2026-08-17T12:00:00"
            else:
                row["last_exported_at"] = ""
            self.assertNotIn(
                "coupa_receipt_export_state",
                financial_result_domains(
                    "get_coupa_rni", json.dumps(payload)), mutation)


class CoupaConfigTests(unittest.TestCase):
    def _load(self, body: str):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "config.yaml"
            path.write_text(body)
            return load_config(str(path))

    def test_config_is_typed_and_normalized(self):
        cfg = self._load("""
coupa:
  po_receipt_authority: true
  business_timezone: UTC
  business_unit_path: account.segment-1
  receipt_business_unit_filter: account[segment_1]
  invoice_business_unit_filter: invoice_lines[account][segment_1]
  invoice_scope_order_line_invariant: true
  business_unit_map: {US001: US_CORP}
  invoice_eligible_statuses: [APPROVED]
  rni_max_rows: 25000
""")
        self.assertTrue(cfg.coupa.po_receipt_authority)
        self.assertEqual(cfg.coupa.business_unit_map, {"US001": "US_CORP"})
        self.assertEqual(cfg.coupa.receipt_business_unit_filter,
                         "account[segment_1]")
        self.assertTrue(cfg.coupa.invoice_scope_order_line_invariant)
        self.assertEqual(cfg.coupa.invoice_eligible_statuses, ["approved"])

    def test_quoted_false_and_malformed_shapes_fail_closed(self):
        for body in (
            'coupa:\n  po_receipt_authority: "false"\n',
            'coupa:\n  po_receipt_authority: true\n',
            'coupa:\n  po_receipt_authority: true\n  business_timezone: Not/AZone\n',
            'coupa:\n  business_unit_map: [US001]\n',
            'coupa:\n  invoice_eligible_statuses: approved\n',
            'coupa:\n  invoice_scope_order_line_invariant: "false"\n',
            'coupa:\n  receipt_business_unit_filter: "bad key"\n',
            'coupa:\n  rni_max_rows: 100001\n',
        ):
            with self.assertRaises(RuntimeError):
                self._load(body)

    def test_known_nonapproved_and_paid_only_eligibility_fail_at_config_load(self):
        for statuses in ("[pending_approval]", "[draft]", "[voided]",
                         "[paid]"):
            with self.subTest(statuses=statuses):
                with self.assertRaises(RuntimeError):
                    self._load(
                        "coupa:\n  invoice_eligible_statuses: "
                        + statuses + "\n")


class ScriptedProvider:
    name = "test"
    model = "scripted"

    def __init__(self, responses, authority="coupa"):
        self.responses = list(responses)
        self.procurement_authority = authority
        self.routing_questions = []

    def set_routing_question(self, text):
        self.routing_questions.append(text)

    def send_user(self, text):
        return self.responses.pop(0)

    def send_tool_results(self, results):
        return self.responses.pop(0)


class FakeSession:
    def __init__(self, outputs):
        self.outputs = outputs

    async def call_tool(self, name, arguments):
        await asyncio.sleep(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps(self.outputs[name]))],
            is_error=False,
        )


class AuthorityEvidenceGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_currency_grouped_candidate_is_grounded_without_fx(self):
        expected = (
            "Coupa PO-line review candidates are grouped by native currency; "
            "booked status not evaluated."
        )
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(
                id="a", name="get_coupa_rni", args={})]),
            LLMResponse(text=expected),
        ])
        answer = await agent_turn(
            provider, FakeSession({"get_coupa_rni": _coupa_payload()}),
            "Show Coupa PO-line candidates separately for each currency",
            scope={"business_unit": "US001"}, surface="gui")
        self.assertEqual(answer, expected)

    async def test_candidate_cannot_ground_receipt_to_invoice_identity(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(
                id="a", name="get_coupa_rni", args={})]),
            LLMResponse(text="Receipt R123 has an invoice."),
        ])
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider, FakeSession({"get_coupa_rni": _coupa_payload()}),
                "Is Coupa receipt R123 invoiced?",
                scope={"business_unit": "US001"}, surface="gui")
        self.assertIn("not established", answer.lower())
        self.assertIn("matching-allocation", answer.lower())

    async def test_wrong_source_cannot_satisfy_generic_candidate_question(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(
                id="a", name="get_po_grni_candidates", args={})]),
            LLMResponse(text="One candidate was found."),
        ])
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider, FakeSession({"get_po_grni_candidates": _ps_payload()}),
                "Which current received-not-invoiced items should we review?",
                scope={"business_unit": "US001"}, surface="gui")
        self.assertIn("incomplete", answer.lower())
        self.assertIn("Booked status not evaluated", answer)

    async def test_raw_sql_cannot_fabricate_the_unconfigured_posting_bridge(self):
        provider = ScriptedProvider([
            LLMResponse(tool_calls=[ToolCall(
                id="a", name="run_sql", args={"sql": "SELECT 1 AS x"})]),
            LLMResponse(text="The Coupa accrual was posted."),
        ])
        with patch("pstb.client.chat.MAX_NUDGES", 0):
            answer = await agent_turn(
                provider, FakeSession({"run_sql": {"rows": [{"x": 1}],
                                                     "row_count": 1}}),
                "Did exported Coupa receipts get booked in PeopleSoft?",
                scope={"business_unit": "US001", "ledger": "ACTUALS"},
                surface="gui")
        self.assertIn("not established", answer.lower())
        self.assertIn("governed integration", answer.lower())

    async def test_raw_sql_cannot_replace_curated_coupa_candidate_or_export(self):
        for question, expected in (
            ("Which current Coupa PO lines have net receipt activity above "
             "eligible invoice coverage?", "incomplete"),
            ("Which Coupa receipts are flagged exported?",
             "export-flag population is incomplete"),
        ):
            provider = ScriptedProvider([
                LLMResponse(tool_calls=[ToolCall(
                    id="a", name="run_sql", args={"sql": "SELECT 1 AS x"})]),
                LLMResponse(text="The Coupa control passed."),
            ])
            with patch("pstb.client.chat.MAX_NUDGES", 0):
                answer = await agent_turn(
                    provider,
                    FakeSession({"run_sql": {"rows": [{"x": 1}],
                                             "row_count": 1}}),
                    question,
                    scope={"business_unit": "US001"}, surface="gui")
            self.assertIn(expected, answer.lower(), question)


class PresentationContractTests(unittest.TestCase):
    def test_ui_has_dedicated_truthful_coupa_card(self):
        text = (ROOT / "pstb/gui/static/index.html").read_text()
        for phrase in (
            "if(name==='get_coupa_rni') return renderCoupaRNI(data);",
            "Coupa PO-line review candidates",
            "Booking and posting:",
            "order-line ID; a specific receipt is not claimed uninvoiced",
            "export does not prove ERP booking",
            "No zero, clean, booked, or posted conclusion is shown.",
            "positive candidates",
            "Display limited",
            "Eligible invoice coverage at receipt valuation",
            "Net receipt value at receipt valuation",
            "Coupa source face total",
            "receipt_face_to_valuation_difference",
            "Exceptions and exclusions",
            "Observed amount",
            "exceptionRows",
            "of '+esc(receiptCount)+' IDs shown",
            "coverage.coupa_business_unit",
            "completed sequential collections",
            "coupaAmount",
            "receipt_transactions",
            "Receipt export flags",
        ):
            self.assertIn(phrase, text)
        coupa_card = text.split("function renderCoupaRNI", 1)[1].split(
            "function ", 1
        )[0]
        self.assertNotIn("amounts.slice(0,3)", coupa_card)
        self.assertNotIn("if(noData) return frag", coupa_card)


if __name__ == "__main__":
    unittest.main()
