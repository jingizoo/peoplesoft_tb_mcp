"""CPA regressions for the Coupa-first RNI review-candidate control.

These fixtures use Coupa's event and nested-line shapes, not the legacy
mutable PO-line counters.  The transport deliberately behaves like a live
50-row Coupa endpoint so pagination and server-scope failures are exercised.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.parse
import unittest
from types import SimpleNamespace
from unittest import mock

from pstb.connectors.coupa import CoupaConnector, from_env


TODAY = dt.date(2026, 8, 17)
COUPA_BU = "US_CORP"
COUPA_TZ = "America/New_York"


def receipt(
    identifier: int,
    *,
    amount: object = "100",
    quantity: object | None = None,
    price: object | None = None,
    line_id: int = 10,
    line_type: str = "OrderAmountLine",
    event_type: str = "InventoryReceipt",
    original_id: int | None = None,
    voided_value: object | None = None,
    business_unit: str = COUPA_BU,
    currency: str = "USD",
    event_date: str = "2026-08-17T09:00:00Z",
    exported: bool | None = False,
) -> dict:
    order_line = {
        "id": line_id,
        "line-num": 1,
        "type": line_type,
        "order-header-number": f"PO-{line_id}",
        "supplier": {"name": "Example Supplier"},
    }
    if price is not None:
        order_line["price"] = price
    row = {
        "id": identifier,
        "type": event_type,
        "status": "created",
        "transaction-date": event_date,
        "created-at": event_date,
        "total": amount,
        "currency": {"code": currency},
        "account": {"segment-1": business_unit},
        "order-line": order_line,
        "exported": exported,
    }
    if quantity is not None:
        row["quantity"] = quantity
    if price is not None:
        row["price"] = price
    if original_id is not None:
        row["original_transaction_id"] = original_id
    if voided_value is not None:
        row["voided_value"] = voided_value
    return row


def invoice_line(
    identifier: int,
    *,
    line_id: int = 10,
    amount: object = "40",
    quantity: object | None = None,
    line_type: str = "InvoiceAmountLine",
    line_status: str = "new",
    business_unit: str = COUPA_BU,
    currency: str = "USD",
) -> dict:
    row = {
        "id": identifier,
        "order-line-id": line_id,
        "po-number": f"PO-{line_id}",
        "type": line_type,
        "status": line_status,
        "created-at": "2026-08-17T10:00:00Z",
        "total": amount,
        "currency": {"code": currency},
        "account": {"segment-1": business_unit},
    }
    if quantity is not None:
        row["quantity"] = quantity
    return row


def invoice(
    identifier: int,
    *,
    status: str = "approved",
    lines: list[dict] | None = None,
    paid: bool | None = False,
    canceled: bool | None = False,
    credit: bool | None = None,
) -> dict:
    row = {
        "id": identifier,
        "invoice-number": f"INV-{identifier}",
        "status": status,
        "paid": paid,
        "canceled": canceled,
        "invoice-lines": list(lines or []),
    }
    if credit is not None:
        row["is-credit-note"] = credit
    return row


class PagedTransport:
    def __init__(self, receipts=None, invoices=None, *,
                 ignore_offset: bool = False,
                 ignore_filter: bool = False,
                 fail_endpoint: str = "",
                 fail_offset: int | None = None):
        self.receipts = list(receipts or [])
        self.invoices = list(invoices or [])
        self.ignore_offset = ignore_offset
        self.ignore_filter = ignore_filter
        self.fail_endpoint = fail_endpoint
        self.fail_offset = fail_offset
        self.calls: list[str] = []

    @staticmethod
    def _receipt_bu(row: dict) -> str:
        return str((row.get("account") or {}).get("segment-1") or "")

    @staticmethod
    def _invoice_has_bu(row: dict, value: str) -> bool:
        return any(str((line.get("account") or {}).get("segment-1") or "")
                   == value for line in row.get("invoice-lines", []))

    def __call__(self, method: str, url: str, headers: dict,
                 body=None) -> tuple[int, str]:
        parsed = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(parsed.query)
        offset = int(params.get("offset", [0])[0])
        limit = int(params.get("limit", [50])[0])
        self.calls.append(url)
        if (parsed.path == self.fail_endpoint
                and (self.fail_offset is None or offset == self.fail_offset)):
            return 503, json.dumps({"error": "page unavailable"})
        if parsed.path == "/api/receiving_transactions":
            rows = list(self.receipts)
            value = params.get("account[segment_1]", [""])[0]
            if value and not self.ignore_filter:
                rows = [row for row in rows if self._receipt_bu(row) == value]
        elif parsed.path == "/api/invoices":
            rows = list(self.invoices)
            value = params.get(
                "invoice_lines[account][segment_1]", [""])[0]
            if value and not self.ignore_filter:
                rows = [row for row in rows
                        if self._invoice_has_bu(row, value)]
        else:
            return 404, json.dumps({"error": "not found"})
        start = 0 if self.ignore_offset and offset else offset
        return 200, json.dumps(rows[start:start + limit])


def connector(receipts=None, invoices=None, **transport_kwargs) -> tuple[
        CoupaConnector, PagedTransport]:
    transport = PagedTransport(receipts, invoices, **transport_kwargs)
    control = CoupaConnector(
        "https://coupa.example",
        transport=transport,
        rni_business_unit_path="account.segment-1",
        rni_business_timezone=COUPA_TZ,
        rni_receipt_business_unit_filter="account[segment_1]",
        rni_invoice_business_unit_filter=(
            "invoice_lines[account][segment_1]"),
        rni_business_unit_map={"US001": COUPA_BU},
        rni_eligible_invoice_statuses=["approved", "paid"],
        rni_invoice_scope_order_line_invariant=True,
        rni_max_rows=50_000,
    )
    return control, transport


def run(control: CoupaConnector, **kwargs) -> dict:
    return control.received_not_invoiced(
        business_unit="US001", as_of_date=TODAY.isoformat(), today=TODAY,
        **kwargs)


class ScopeAndPaginationTests(unittest.TestCase):
    def test_live_requires_a_valid_coupa_company_timezone_before_scan(self):
        for timezone_name in ("", "Mars/Olympus_Mons"):
            with self.subTest(timezone=timezone_name):
                transport = PagedTransport([receipt(1)], [])
                control = CoupaConnector(
                    "https://coupa.example", transport=transport,
                    rni_business_unit_path="account.segment-1",
                    rni_business_timezone=timezone_name,
                    rni_receipt_business_unit_filter="account[segment_1]",
                    rni_invoice_business_unit_filter=(
                        "invoice_lines[account][segment_1]"),
                    rni_business_unit_map={"US001": COUPA_BU},
                    rni_invoice_scope_order_line_invariant=True)
                out = run(control)
                self.assertFalse(out["evaluated"])
                self.assertIn("business_timezone", out["reason"])
                self.assertEqual(transport.calls, [])

    def test_company_timezone_drives_current_day_without_test_override(self):
        real_datetime = dt.datetime

        class FixedDateTime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                instant = real_datetime(
                    2026, 8, 18, 1, 0, tzinfo=dt.timezone.utc)
                return instant.astimezone(tz) if tz else instant.replace(
                    tzinfo=None)

        control, _ = connector([receipt(1)])
        with mock.patch("pstb.connectors.coupa.dt.datetime", FixedDateTime):
            out = control.received_not_invoiced(
                business_unit="US001", as_of_date="2026-08-17")
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["coverage"]["current_date"], "2026-08-17")

    def test_source_timestamps_are_resolved_on_the_coupa_company_day(self):
        row = receipt(1, event_date="2026-08-18T01:00:00Z")
        inv_line = invoice_line(11, amount="40")
        inv_line["created-at"] = "2026-08-18T01:30:00Z"
        control, _ = connector([row], [invoice(1, lines=[inv_line])])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 60.0})

        malformed_sources = []
        for field, value in (
                ("transaction-date", "2026-08-17"),
                ("transaction-date", "2026-08-17T10:00:00"),
                ("created-at", "2026-08-17")):
            malformed = receipt(2)
            malformed[field] = value
            malformed_sources.append((field, value, malformed, []))
        malformed_invoice_line = invoice_line(12)
        malformed_invoice_line["created-at"] = "2026-08-17"
        malformed_sources.append((
            "invoice created-at", "2026-08-17", receipt(3),
            [invoice(2, lines=[malformed_invoice_line])]))

        for field, value, malformed_receipt, invoices in malformed_sources:
            with self.subTest(field=field, value=value):
                control, _ = connector([malformed_receipt], invoices)
                out = run(control)
                self.assertFalse(out["evaluated"])
                self.assertRegex(out["reason"],
                                 r"not an ISO datetime|timezone offset")

    def test_governed_mapping_drives_exact_filters_and_payload_scope(self):
        control, transport = connector([receipt(1)])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["scope"]["business_unit"], "US001")
        self.assertEqual(out["scope"]["coupa_business_unit"], COUPA_BU)
        self.assertEqual(out["scope"]["business_timezone"], COUPA_TZ)
        self.assertEqual(out["coverage"]["business_timezone"], COUPA_TZ)
        self.assertEqual(out["snapshot"]["business_timezone"], COUPA_TZ)
        self.assertEqual(out["coverage"]["current_date_basis"],
                         "configured_coupa_company_timezone")
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                   for url in transport.calls]
        self.assertEqual(queries[0]["account[segment_1]"], [COUPA_BU])
        self.assertEqual(
            queries[1]["invoice_lines[account][segment_1]"], [COUPA_BU])

    def test_invoice_order_line_scope_invariant_is_required_live(self):
        transport = PagedTransport([receipt(1)], [])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="account[segment_1]",
            rni_invoice_business_unit_filter=(
                "invoice_lines[account][segment_1]"),
            rni_business_unit_map={"US001": COUPA_BU})
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("invoice_scope_order_line_invariant", out["reason"])
        self.assertEqual(transport.calls, [])

    def test_even_standard_paths_require_tenant_tested_live_filter_keys(self):
        transport = PagedTransport([receipt(1)], [])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_business_unit_map={"US001": COUPA_BU},
            rni_invoice_scope_order_line_invariant=True)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("exact tenant-tested query keys", out["reason"])
        self.assertEqual(transport.calls, [])

    def test_missing_or_ambiguous_business_unit_map_fails_before_scan(self):
        transport = PagedTransport([receipt(1)], [])
        missing = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_business_unit_map={"CA001": "CA_CORP"})
        out = run(missing)
        self.assertFalse(out["evaluated"])
        self.assertIn("not present", out["reason"])
        self.assertEqual(transport.calls, [])

        ambiguous = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_business_unit_map={"US001": COUPA_BU, "US002": COUPA_BU})
        out = run(ambiguous)
        self.assertFalse(out["evaluated"])
        self.assertIn("ambiguous", out["reason"])

    def test_custom_business_unit_path_is_not_guessed_or_prefix_dependent(self):
        row = receipt(1)
        row.pop("account")
        row["custom-fields"] = {"erp-bu": "CO_CUSTOM"}
        transport = PagedTransport([row], [], ignore_filter=True)
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="custom-fields.erp-bu",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="erp-business-unit",
            rni_invoice_business_unit_filter=(
                "invoice_lines[erp-business-unit]"),
            rni_invoice_scope_order_line_invariant=True,
            rni_business_unit_map={"US001": "CO_CUSTOM"})
        out = run(control)
        self.assertTrue(out["evaluated"])
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                   for url in transport.calls]
        self.assertEqual(queries[0]["erp-business-unit"], ["CO_CUSTOM"])
        self.assertEqual(
            queries[1]["invoice_lines[erp-business-unit]"],
            ["CO_CUSTOM"])

        row = receipt(2)
        control = CoupaConnector(
            "https://coupa.example", transport=PagedTransport([row], []),
            rni_business_unit_path="custom-fields.erp-bu",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="erp-business-unit",
            rni_invoice_business_unit_filter=(
                "invoice_lines[erp-business-unit]"),
            rni_invoice_scope_order_line_invariant=True,
            rni_business_unit_map={"US001": "CO_CUSTOM"})
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("no verifiable business-unit", out["reason"])

    def test_ignored_server_filter_fails_closed_without_cross_bu_rows(self):
        control, _ = connector(
            [receipt(1), receipt(2, business_unit="CA_CORP")],
            ignore_filter=True)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("filter may have been ignored", out["reason"])
        self.assertEqual(out["lines"], [])

        foreign_invoice = invoice(1, lines=[invoice_line(
            11, line_id=20, business_unit="CA_CORP")])
        control, _ = connector(
            [receipt(1)], [foreign_invoice], ignore_filter=True)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("no nested line", out["reason"])

        # Header values from a foreign-only response must never be validated
        # into the selected-BU error payload before nested scope is known.
        for secret_header in (
            invoice(987654, status="secret_custom_status", lines=[
                invoice_line(91, line_id=91, business_unit="CA_CORP")]),
            invoice(987655, paid="secret_paid_value", lines=[
                invoice_line(92, line_id=92, business_unit="CA_CORP")]),
        ):
            with self.subTest(secret_header=secret_header["id"]):
                control, _ = connector(
                    [receipt(1)], [secret_header], ignore_filter=True)
                out = run(control)
                rendered = json.dumps(out)
                self.assertFalse(out["evaluated"])
                self.assertIn("filter may have been ignored", out["reason"])
                self.assertNotIn(str(secret_header["id"]), rendered)
                self.assertNotIn("secret_custom_status", rendered)
                self.assertNotIn("secret_paid_value", rendered)

    def test_mixed_bu_invoice_header_redacts_unrelated_child_but_same_line_fails(self):
        target = invoice_line(11, line_id=10, amount="40")
        unrelated = invoice_line(
            12, line_id=20, amount="500", business_unit="CA_CORP")
        control, _ = connector([receipt(1)], [invoice(
            1, lines=[target, unrelated])])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 60.0})
        self.assertEqual(
            out["population"]["invoice_lines_outside_business_unit"], 1)
        self.assertNotIn("500.0", json.dumps(out))

        conflicting = invoice_line(
            13, line_id=10, amount="10", business_unit="CA_CORP")
        control, _ = connector([receipt(1)], [invoice(
            2, lines=[target, conflicting])])
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("ownership/allocation is inconsistent", out["reason"])

    def test_no_receipts_is_no_data_not_a_zero_rni_pass(self):
        control, _ = connector([], [])
        out = run(control)
        self.assertEqual(out["status"], "no_data")
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["totals_by_currency"])

    def test_page_2_is_read_and_exact_50_reads_terminal_page(self):
        rows = [receipt(i, amount="1", line_id=i) for i in range(1, 52)]
        control, _ = connector(rows)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["population"]["candidate_count"], 51)
        self.assertEqual(out["pagination"]["receipts"]["pages_read"], 2)

        control, _ = connector(rows[:50])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["pagination"]["receipts"]["pages_read"], 2)

    def test_repeated_page_page_error_and_cap_never_report_totals(self):
        rows = [receipt(i, amount="1", line_id=i) for i in range(1, 52)]
        control, _ = connector(rows, ignore_offset=True)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["totals_by_currency"])
        self.assertIn("repeated", out["reason"])

        control, _ = connector(
            rows, fail_endpoint="/api/receiving_transactions", fail_offset=50)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIsNone(out["totals_by_currency"])

        control, _ = connector(rows)
        out = run(control, max_rows=50)
        self.assertFalse(out["evaluated"])
        self.assertTrue(out["population"]["truncated"])

        control, _ = connector([receipt(1)], [invoice(
            1, lines=[invoice_line(11), invoice_line(12, line_id=20)])])
        out = run(control, max_rows=1)
        self.assertFalse(out["evaluated"])
        self.assertTrue(out["population"]["truncated"])
        self.assertIn("nested invoice-line", out["reason"])

    def test_call_limit_cannot_raise_the_configured_scan_cap(self):
        rows = [receipt(i, amount="1", line_id=i) for i in range(1, 12)]
        transport = PagedTransport(rows, [])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="account[segment_1]",
            rni_invoice_business_unit_filter=(
                "invoice_lines[account][segment_1]"),
            rni_business_unit_map={"US001": COUPA_BU},
            rni_invoice_scope_order_line_invariant=True,
            rni_max_rows=10)
        out = run(control, max_rows=100)
        self.assertFalse(out["evaluated"])
        self.assertTrue(out["population"]["truncated"])
        self.assertEqual(out["population"]["requested_row_cap"], 100)
        self.assertEqual(out["population"]["configured_row_cap"], 10)
        self.assertEqual(out["population"]["effective_row_cap"], 10)
        self.assertIsNone(out["totals_by_currency"])

    def test_historical_cutoff_refuses_before_calling_mutable_status_api(self):
        control, transport = connector([receipt(1)])
        out = control.received_not_invoiced(
            business_unit="US001", as_of_date="2026-07-31", today=TODAY)
        self.assertFalse(out["evaluated"])
        self.assertIn("historical", out["reason"])
        self.assertEqual(out["coverage"]["cutoff_classification"],
                         "current_date_only")
        self.assertEqual(out["coverage"]["current_date"], TODAY.isoformat())
        self.assertEqual(transport.calls, [])

    def test_from_env_consumes_reviewable_cfg_and_receipt_oauth_scope(self):
        cfg = SimpleNamespace(coupa=SimpleNamespace(
            business_unit_path="custom-fields.erp-bu",
            business_timezone=COUPA_TZ,
            receipt_business_unit_filter="erp-business-unit",
            invoice_business_unit_filter="invoice_lines[erp-business-unit]",
            invoice_scope_order_line_invariant=True,
            business_unit_map={"US001": "CO_CUSTOM"},
            invoice_eligible_statuses=["approved"],
            receipt_eligible_statuses=["created", "received_custom"],
            rni_max_rows=321,
        ))
        with mock.patch.dict(os.environ, {
                "COUPA_BASE_URL": "https://coupa.example",
                "COUPA_API_KEY": "read-only-key",
        }, clear=True):
            control = from_env(cfg=cfg)
        self.assertEqual(control.rni_business_unit_path,
                         "custom-fields.erp-bu")
        self.assertEqual(control.rni_business_timezone, COUPA_TZ)
        self.assertEqual(control.rni_business_unit_map["US001"], "CO_CUSTOM")
        self.assertEqual(control.rni_receipt_business_unit_filter,
                         "erp-business-unit")
        self.assertEqual(control.rni_invoice_business_unit_filter,
                         "invoice_lines[erp-business-unit]")
        self.assertEqual(control.rni_max_rows, 321)
        self.assertEqual(control.rni_receipt_statuses,
                         frozenset({"created", "received_custom"}))
        self.assertTrue(control.rni_invoice_scope_order_line_invariant)
        self.assertIn("core.inventory.receiving.read", control._scope)


class AccountingArithmeticTests(unittest.TestCase):
    def test_approved_header_controls_eligibility_not_line_status(self):
        approved = invoice(1, status="approved", lines=[
            invoice_line(11, amount="40", line_status="new")])
        control, _ = connector([receipt(1)], [approved])
        out = run(control)
        self.assertEqual(out["totals_by_currency"], {"USD": 60.0})
        self.assertEqual(out["lines"][0]["eligible_invoice_line_count"], 1)

        pending = invoice(2, status="pending_approval", lines=[
            invoice_line(12, amount="40", line_status="approved")])
        control, _ = connector([receipt(1)], [pending])
        out = run(control)
        self.assertEqual(out["totals_by_currency"], {"USD": 100.0})
        self.assertEqual(
            out["exceptions"]["counts"]["invoice_present_not_eligible"], 1)

    def test_paid_boolean_never_rescues_pending_header(self):
        pending = invoice(2, status="pending_approval", paid=True, lines=[
            invoice_line(12, amount="100", line_status="approved")])
        control, _ = connector([receipt(1)], [pending])
        out = run(control)
        self.assertEqual(out["totals_by_currency"], {"USD": 100.0})

    def test_paid_only_config_is_rejected_as_boolean_not_header_status(self):
        paid_invoice = invoice(
            1, status="approved", paid=True,
            lines=[invoice_line(11, amount="40")])
        transport = PagedTransport([receipt(1)], [paid_invoice])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="account[segment_1]",
            rni_invoice_business_unit_filter=(
                "invoice_lines[account][segment_1]"),
            rni_business_unit_map={"US001": COUPA_BU},
            rni_eligible_invoice_statuses=["paid"],
            rni_invoice_scope_order_line_invariant=True)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("paid is a boolean, not a header status", out["reason"])
        self.assertEqual(transport.calls, [])

    def test_missing_cancellation_or_configured_paid_state_fails_closed(self):
        for value in (None, "false"):
            with self.subTest(canceled=value):
                bad_canceled = invoice(
                    1, lines=[invoice_line(11, amount="40")])
                if value is None:
                    bad_canceled.pop("canceled")
                else:
                    bad_canceled["canceled"] = value
                control, _ = connector([receipt(1)], [bad_canceled])
                out = run(control)
                self.assertFalse(out["evaluated"])
                self.assertIn("boolean canceled state", out["reason"])

        missing_paid = invoice(
            2, lines=[invoice_line(12, amount="40")])
        missing_paid.pop("paid")
        control, _ = connector([receipt(1)], [missing_paid])
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("boolean paid state", out["reason"])

    def test_explicit_tenant_header_status_is_supported_but_unknown_is_not(self):
        transport = PagedTransport([receipt(1)], [invoice(
            1, status="approved_legacy", lines=[invoice_line(11)])])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="account[segment_1]",
            rni_invoice_business_unit_filter=(
                "invoice_lines[account][segment_1]"),
            rni_business_unit_map={"US001": COUPA_BU},
            rni_eligible_invoice_statuses=["approved_legacy"],
            rni_invoice_scope_order_line_invariant=True)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 60.0})

        control, _ = connector([receipt(1)], [invoice(
            2, status="approved_legacy", lines=[invoice_line(12)])])
        self.assertFalse(run(control)["evaluated"])

    def test_delivered_noneligible_statuses_cannot_be_configured_as_eligible(self):
        delivered_noneligible = (
            "new", "ap_hold", "draft", "on_hold", "pending_receipt",
            "rejected", "abandoned", "disputed", "pending_approval",
            "booking_hold", "save_as_draft", "pending_action", "voided",
            "processing", "invalid", "payable_adjustment",
        )
        for status in delivered_noneligible:
            with self.subTest(status=status):
                transport = PagedTransport([receipt(1)], [])
                control = CoupaConnector(
                    "https://coupa.example", transport=transport,
                    rni_business_unit_path="account.segment-1",
                    rni_business_timezone=COUPA_TZ,
                    rni_receipt_business_unit_filter="account[segment_1]",
                    rni_invoice_business_unit_filter=(
                        "invoice_lines[account][segment_1]"),
                    rni_business_unit_map={"US001": COUPA_BU},
                    rni_eligible_invoice_statuses=[status],
                    rni_invoice_scope_order_line_invariant=True)
                out = run(control)
                self.assertFalse(out["evaluated"])
                self.assertIn("not an approved outbound invoice",
                              out["reason"])
                self.assertIn(status, out["reason"])
                self.assertEqual(transport.calls, [])

    def test_explicit_tenant_receipt_status_is_supported_but_unknown_is_not(self):
        custom = receipt(1)
        custom["status"] = "received_custom"
        transport = PagedTransport([custom], [])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_receipt_business_unit_filter="account[segment_1]",
            rni_invoice_business_unit_filter=(
                "invoice_lines[account][segment_1]"),
            rni_business_unit_map={"US001": COUPA_BU},
            rni_receipt_statuses=["created", "received_custom"],
            rni_invoice_scope_order_line_invariant=True)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["candidate_basis"]["eligible_receipt_statuses"],
                         ["created", "received_custom"])
        self.assertEqual(out["coverage"]["eligible_receipt_statuses"],
                         ["created", "received_custom"])

        control, _ = connector([custom])
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("unsupported status", out["reason"])

        transport = PagedTransport([receipt(2)], [])
        control = CoupaConnector(
            "https://coupa.example", transport=transport,
            rni_business_unit_path="account.segment-1",
            rni_business_timezone=COUPA_TZ,
            rni_business_unit_map={"US001": COUPA_BU},
            rni_receipt_statuses=["created status"],
            rni_invoice_scope_order_line_invariant=True)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("receipt eligibility", out["reason"])
        self.assertEqual(transport.calls, [])

    def test_quantity_candidate_uses_received_quantity_at_receipt_price(self):
        row = receipt(1, amount="1000", quantity="10", price="100",
                      line_type="OrderQuantityLine")
        inv = invoice(1, lines=[invoice_line(
            11, amount="450", quantity="4", line_type="InvoiceQuantityLine")])
        control, _ = connector([row], [inv])
        out = run(control)
        self.assertEqual(out["totals_by_currency"], {"USD": 600.0})
        self.assertEqual(out["lines"][0]["remaining_quantity"], 6.0)
        self.assertEqual(out["lines"][0]["valuation_unit_price"], 100.0)

    def test_quantity_face_total_is_separate_from_receipt_valuation(self):
        row = receipt(
            1, amount="100.00", quantity="3", price="33.333333",
            line_type="OrderQuantityLine")
        control, _ = connector([row])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 99.999999})
        candidate = out["lines"][0]
        self.assertEqual(candidate["net_receipt_value_at_receipt_valuation"],
                         99.999999)
        self.assertEqual(candidate["net_receipt_amount"], 99.999999)
        self.assertEqual(candidate["net_receipt_face_amount"], 100.0)
        self.assertEqual(candidate["receipt_face_to_valuation_difference"],
                         0.000001)
        self.assertEqual(candidate["rni_candidate_amount"], 99.999999)
        self.assertEqual(
            out["observed"][
                "net_receipt_values_at_receipt_valuation_by_currency"],
            {"USD": 99.999999})
        self.assertEqual(
            out["observed"]["net_receipt_face_totals_by_currency"],
            {"USD": 100.0})
        self.assertEqual(
            out["observed"][
                "receipt_face_to_valuation_differences_by_currency"],
            {"USD": 0.000001})

        # Exercise the production evidence contract with the actual engine
        # payload: ordinary Coupa face-total rounding must not make a valid
        # quantity candidate unusable by the financial evidence gate.
        from pstb.guards import tool_result_status
        ok, why = tool_result_status("get_coupa_rni", json.dumps(out))
        self.assertTrue(ok, why)

    def test_signed_return_is_normalized_once(self):
        rows = [
            receipt(1, amount="100", quantity="1", price="100",
                    line_type="OrderQuantityLine"),
            receipt(2, amount="-20", quantity="-.2", price="100",
                    line_type="OrderQuantityLine",
                    event_type="ReceivingQuantityReturnToSupplier",
                    original_id=1),
        ]
        control, _ = connector(rows)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 80.0})

    def test_return_and_void_chain_nets_each_event_once(self):
        rows = [
            receipt(1, amount="1000", quantity="10", price="100",
                    line_type="OrderQuantityLine", voided_value="2"),
            receipt(2, amount="-300", quantity="-3", price="100",
                    line_type="OrderQuantityLine",
                    event_type="ReceivingQuantityReturnToSupplier",
                    original_id=1, voided_value="1"),
            receipt(3, amount="-200", quantity="-2", price="100",
                    line_type="OrderQuantityLine",
                    event_type="VoidInventoryReceipt", original_id=1),
            receipt(4, amount="100", quantity="1", price="100",
                    line_type="OrderQuantityLine",
                    event_type="VoidReceivingQuantityReturnToSupplier",
                    original_id=2),
        ]
        control, _ = connector(rows)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 600.0})
        self.assertEqual(out["lines"][0]["return_or_void_transaction_count"], 3)

    def test_voided_value_and_parent_type_must_match_linked_reversal(self):
        mismatch = [
            receipt(1, amount="100", quantity="1", price="100",
                    line_type="OrderQuantityLine", voided_value="1"),
            receipt(2, amount="-1", quantity="-.01", price="100",
                    line_type="OrderQuantityLine",
                    event_type="VoidInventoryReceipt", original_id=1),
        ]
        control, _ = connector(mismatch)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("does not equal linked void", out["reason"])

        wrong_parent = [
            receipt(1, amount="100", line_type="OrderAmountLine"),
            receipt(2, amount="-20", line_type="OrderAmountLine",
                    event_type="ReceivingAmountReturnToSupplier", original_id=1),
            receipt(3, amount="-10", line_type="OrderAmountLine",
                    event_type="VoidInventoryReceipt", original_id=2),
        ]
        control, _ = connector(wrong_parent)
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("cannot reverse parent type", out["reason"])

    def test_credit_note_restores_coverage_once_and_voided_invoice_does_not(self):
        debit = invoice(1, lines=[invoice_line(11, amount="80")])
        credit = invoice(2, lines=[invoice_line(12, amount="20")], credit=True)
        voided = invoice(3, status="voided", lines=[
            invoice_line(13, amount="50", line_status="voided")])
        control, _ = connector([receipt(1)], [debit, credit, voided])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 40.0})
        eligible = out["lines"][0]["eligible_invoice_lines"]
        self.assertEqual(sum(line["candidate_valuation_amount"]
                             for line in eligible), 60.0)

        control, _ = connector([receipt(1)], [invoice(
            4, lines=[invoice_line(14, amount="20")], credit=True)])
        out = run(control)
        self.assertEqual(out["conclusion"],
                         "exceptions_present_no_positive_candidates")
        self.assertEqual(out["population"]["candidate_count"], 0)
        self.assertEqual(out["exceptions"]["counts"][
            "net_credit_invoice_activity"], 1)

    def test_unknown_status_type_currency_and_split_allocations_fail_closed(self):
        control, _ = connector([receipt(1, event_type="SupplierReturnEvent")])
        self.assertFalse(run(control)["evaluated"])

        unknown = invoice(1, status="future_state", lines=[invoice_line(11)])
        control, _ = connector([receipt(1)], [unknown])
        self.assertFalse(run(control)["evaluated"])

        wrong_currency = invoice(1, lines=[invoice_line(11, currency="EUR")])
        control, _ = connector([receipt(1)], [wrong_currency])
        self.assertFalse(run(control)["evaluated"])

        split = receipt(1)
        split["order-line"]["account-allocations"] = [{"id": 1}]
        control, _ = connector([split])
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("account-allocations", out["reason"])

    def test_orphan_links_missing_order_line_and_nonfinite_amounts_fail_closed(self):
        orphan = receipt(
            2, amount="-10", event_type="ReceivingAmountReturnToSupplier",
            original_id=999)
        control, _ = connector([orphan])
        self.assertFalse(run(control)["evaluated"])

        inv_line = invoice_line(11)
        inv_line.pop("order-line-id")
        control, _ = connector([receipt(1)], [invoice(1, lines=[inv_line])])
        out = run(control)
        self.assertFalse(out["evaluated"])
        self.assertIn("no exact order-line-id", out["reason"])

        for bad in (None, "NaN", "Infinity", "1e1000", True, ""):
            with self.subTest(amount=bad):
                control, _ = connector([receipt(1, amount=bad)])
                out = run(control)
                self.assertFalse(out["evaluated"])
                self.assertIsNone(out["totals_by_currency"])

    def test_known_consumption_only_is_no_data_not_zero_candidate(self):
        control, _ = connector([receipt(
            1, event_type="ReceivingAmountConsumption")])
        out = run(control)
        self.assertEqual(out["status"], "no_data")
        self.assertFalse(out["evaluated"])
        self.assertEqual(
            out["population"]["excluded_receiving_types"], 1)

    def test_as_of_boundary_is_inclusive_and_future_events_are_not_counted(self):
        boundary = receipt(1, amount="40", line_id=1)
        future = receipt(
            2, amount="60", line_id=2,
            # Midnight in the configured America/New_York company calendar.
            # A UTC-midnight event is still on August 17 for this tenant.
            event_date="2026-08-18T04:00:00Z")
        control, _ = connector([boundary, future])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 40.0})


class PresentationTruthTests(unittest.TestCase):
    def test_micro_amount_rows_preserve_source_magnitude_and_add_to_total(self):
        rows = [receipt(
            identifier, amount="0.0000006", line_id=identifier)
                for identifier in range(1, 5)]
        control, _ = connector(rows)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 0.0000024})
        self.assertEqual(
            [row["rni_candidate_amount"] for row in out["lines"]],
            [0.0000006] * 4)
        self.assertAlmostEqual(
            sum(row["rni_candidate_amount"] for row in out["lines"]),
            out["totals_by_currency"]["USD"], places=18)

    def test_threshold_preserves_full_positive_population(self):
        control, _ = connector([receipt(1, amount="100")])
        out = run(control, min_amount=200)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["conclusion"], "no_candidates_above_threshold")
        self.assertEqual(out["population"]["candidate_count"], 0)
        self.assertEqual(out["population"]["positive_candidate_count"], 1)
        self.assertEqual(out["totals_by_currency"], {})
        self.assertEqual(out["all_positive_candidate_totals_by_currency"],
                         {"USD": 100.0})
        self.assertEqual(out["min_amount"], 200.0)

    def test_zero_candidates_with_exception_is_never_a_clean_conclusion(self):
        inv = invoice(1, lines=[invoice_line(11, amount="120")])
        control, _ = connector([receipt(1)], [inv])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["conclusion"],
                         "exceptions_present_no_positive_candidates")
        self.assertEqual(out["exceptions"]["counts"]["over_invoiced"], 1)

    def test_display_cap_does_not_truncate_complete_totals(self):
        rows = [receipt(i, amount="1", line_id=i) for i in range(1, 206)]
        control, _ = connector(rows)
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["population"]["candidate_count"], 205)
        self.assertEqual(out["population"]["displayed_candidate_count"], 200)
        self.assertTrue(out["population"]["display_truncated"])
        self.assertTrue(out["population"]["totals_complete"])
        self.assertEqual(out["totals_by_currency"], {"USD": 205.0})
        self.assertEqual(len(out["lines"]), 200)

        control, _ = connector(rows)
        export_out = run(control, display_rows=250)
        self.assertTrue(export_out["evaluated"])
        self.assertEqual(export_out["population"]["candidate_count"], 205)
        self.assertEqual(
            export_out["population"]["displayed_candidate_count"], 205)
        self.assertFalse(export_out["population"]["display_truncated"])
        self.assertEqual(export_out["totals_by_currency"], {"USD": 205.0})
        self.assertEqual(len(export_out["lines"]), 205)

        control, _ = connector([receipt(1)])
        capped = run(control, display_rows=100_000)
        self.assertEqual(capped["population"]["requested_display_row_cap"],
                         100_000)
        self.assertEqual(capped["population"]["display_row_cap"], 50_000)
        self.assertEqual(capped["population"]["hard_display_row_cap"], 50_000)

    def test_payload_never_claims_booked_or_receipt_level_matching(self):
        control, _ = connector([receipt(1, exported=True)])
        out = run(control)
        self.assertEqual(out["coverage"]["matching_precision"],
                         "order_line_aggregate")
        self.assertFalse(out["coverage"]["all_grni_complete"])
        self.assertTrue(out["coverage"]["collection_complete"])
        self.assertFalse(out["coverage"]["point_in_time_complete"])
        self.assertEqual(out["coverage"]["cutoff_classification"],
                         "current_date_only")
        self.assertEqual(out["coverage"]["current_date"], TODAY.isoformat())
        self.assertTrue(out["coverage"][
            "invoice_scope_order_line_invariant"])
        self.assertFalse(out["snapshot"]["atomic"])
        self.assertFalse(out["snapshot"]["complete"])
        self.assertTrue(out["snapshot"]["collection_complete"])
        self.assertEqual(out["booked_status"], "not_evaluated")
        self.assertEqual(
            out["export_evidence"]["exported_receipt_transactions"], 1)
        self.assertIn("not prove ERP", out["export_evidence"]["meaning"])

    def test_export_evidence_covers_all_receipts_not_only_candidates(self):
        rows = [
            receipt(1, amount="100", line_id=1, exported=True),
            receipt(2, amount="50", line_id=2, exported=False),
        ]
        fully_invoiced = invoice(
            1, lines=[invoice_line(11, line_id=1, amount="100")])
        control, _ = connector(rows, [fully_invoiced])
        out = run(control, min_amount=75, display_rows=1)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["population"]["candidate_count"], 0)
        export = out["export_evidence"]
        self.assertTrue(export["evaluated"])
        self.assertEqual(export["receipt_transaction_count"], 2)
        self.assertEqual(export["displayed_receipt_transaction_count"], 1)
        self.assertTrue(export["display_truncated"])
        self.assertEqual(export["exported_receipt_transactions"], 1)
        self.assertEqual(export["not_exported_receipt_transactions"], 1)
        self.assertEqual(
            set(export["receipt_transactions"][0]),
            {"receipt_transaction_id", "order_line_id", "type",
             "transaction_date", "exported", "last_exported_at",
             "last_exported_at_valid"})

    def test_malformed_export_timestamp_closes_only_the_export_leg(self):
        row = receipt(1, exported=True)
        row["last-exported-at"] = "not-a-datetime"
        control, _ = connector([row])
        out = run(control)
        self.assertTrue(out["evaluated"])
        self.assertEqual(out["totals_by_currency"], {"USD": 100.0})
        export = out["export_evidence"]
        self.assertFalse(export["evaluated"])
        self.assertFalse(export["complete"])
        self.assertEqual(export["invalid_last_exported_at_transactions"], 1)
        event = export["receipt_transactions"][0]
        self.assertIsNone(event["last_exported_at"])
        self.assertFalse(event["last_exported_at_valid"])
        self.assertNotIn("not-a-datetime", json.dumps(out))

        row["last-exported-at"] = "2026-08-17T10:30:00Z"
        control, _ = connector([row])
        export = run(control)["export_evidence"]
        self.assertTrue(export["evaluated"])
        self.assertEqual(
            export["receipt_transactions"][0]["last_exported_at"],
            "2026-08-17T10:30:00+00:00")

    def test_currency_totals_are_kept_separate(self):
        control, _ = connector([
            receipt(1, amount="100", line_id=1, currency="USD"),
            receipt(2, amount="80", line_id=2, currency="EUR"),
        ])
        out = run(control)
        self.assertEqual(out["totals_by_currency"],
                         {"USD": 100.0, "EUR": 80.0})
        self.assertEqual([row["currency"] for row in out["lines"]],
                         ["EUR", "USD"])
        self.assertIn("never ranked", out["candidate_basis"][
            "display_order_basis"])

    def test_canonical_amounts_and_compatibility_aliases_never_diverge(self):
        control, _ = connector(
            [receipt(1, amount="100")],
            [invoice(1, lines=[invoice_line(11, amount="40")])])
        out = run(control)
        self.assertEqual(out["count"], out["population"]["candidate_count"])
        self.assertEqual(out["totals_by_currency"],
                         out["rni_totals_by_currency"])
        for row in out["lines"]:
            self.assertGreaterEqual(row["net_receipt_amount"], 0)
            self.assertGreaterEqual(row["eligible_invoice_amount"], 0)
            self.assertGreater(row["rni_candidate_amount"], 0)
            self.assertEqual(row["rni_candidate_amount"], row["rni_amt"])
            self.assertEqual(
                row["eligible_invoice_amount"],
                row["eligible_invoice_coverage_at_receipt_valuation"])


if __name__ == "__main__":
    unittest.main()
