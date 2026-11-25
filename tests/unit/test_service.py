from __future__ import annotations

import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx

from inventory_agent.service import InventoryService, economic_order_quantity, money
from tests.helpers import FakeClient, SCENARIO_SKUS, context


class InventoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.service = InventoryService(self.client)

    def test_eoq_reference_example(self) -> None:
        self.assertEqual(economic_order_quantity(1000, 10, 2), Decimal("100"))

    def test_money_uses_decimal_half_up_two_place_display(self) -> None:
        self.assertEqual(money(Decimal("1.005")), "1.01")
        self.assertEqual(money(Decimal("620")), "620.00")

    def test_eoq_decimal_rounding_and_pack_constraints(self) -> None:
        raw = economic_order_quantity(1, 1, 1)
        self.assertEqual(raw.quantize(Decimal("0.01")), Decimal("1.41"))
        result = self.service.calculate_eoq("budget", "FOLDER-MAN", "west")
        self.assertEqual(result["selected_quantity"] % 5, 0)
        self.assertGreaterEqual(result["selected_quantity"], 10)

    def test_eoq_evaluates_all_unit_discount_and_free_ship_candidates(self) -> None:
        result = self.service.calculate_eoq("budget", "TONER-BLK", "tech")
        candidate_q = {row["quantity"] for row in result["candidate_evaluations"]}
        self.assertIn(100, candidate_q)  # discount break and free-shipping edge are evaluated
        at_break = next(row for row in result["candidate_evaluations"] if row["quantity"] == 100)
        self.assertEqual(at_break["unit_price"], "114.00")
        self.assertEqual(result["order_cost_basis"], "90.00")

    def test_zero_demand_has_zero_eoq_and_no_stockout(self) -> None:
        ctx = context("anomaly", demand=0, stock={"PENS-BLUE": 0})
        for row in ctx["sales_history"]:
            row["units_sold"] = 0
            row["annotation"] = None
        self.service = InventoryService(FakeClient({"anomaly": ctx}))
        eoq = self.service.calculate_eoq("anomaly", "PENS-BLUE", "desk")
        self.assertEqual(eoq["selected_quantity"], 0)
        self.assertEqual(eoq["raw_eoq"], "0")
        forecast = self.service.predict_stockout("anomaly", "PENS-BLUE")
        self.assertIsNone(forecast["stockout_date"])

    def test_demand_uses_calendar_90_days_and_only_annotated_exclusions(self) -> None:
        result = self.service.estimate_demand("anomaly", "PENS-BLUE")
        self.assertEqual(result["calendar_days"], 90)
        self.assertEqual(result["daily_demand"], "20.0000")
        sale_id = "PENS-BLUE:2026-02-14"
        adjusted = self.service.estimate_demand("anomaly", "PENS-BLUE", [sale_id])
        self.assertEqual(adjusted["daily_demand"], "9.8889")
        self.assertEqual(adjusted["excluded_units"], 910)
        self.assertIn("denominator stays at 90", adjusted["exclusion_simplification"])
        with self.assertRaisesRegex(ValueError, "annotated in-window"):
            self.service.estimate_demand("anomaly", "PENS-BLUE", ["PENS-BLUE:2026-02-13"])
        with self.assertRaisesRegex(ValueError, "annotated in-window"):
            self.service.estimate_demand("anomaly", "PENS-BLUE", ["PAPER-A4:2026-02-14"])

    def test_eoq_can_compare_anomaly_before_and_after_annotated_exclusion(self) -> None:
        before = self.service.calculate_eoq("anomaly", "PENS-BLUE", "desk")
        after = self.service.calculate_eoq(
            "anomaly", "PENS-BLUE", "desk", ["PENS-BLUE:2026-02-14"]
        )
        self.assertGreater(Decimal(before["annual_demand"]), Decimal(after["annual_demand"]))

    def test_supplier_messages_are_evidence_for_lead_overrides(self) -> None:
        options = self.service.get_supplier_options("disruption", "PAPER-A4")
        by_supplier = {row["supplier_id"]: row for row in options}
        self.assertEqual(by_supplier["north"]["master_lead_days"], 2)
        self.assertEqual(by_supplier["north"]["effective_lead_days"], 21)
        self.assertIn("21 days", by_supplier["north"]["lead_override_evidence"]["text"])
        self.assertEqual(by_supplier["west"]["effective_lead_days"], 3)

    def test_supplier_options_treat_null_price_breaks_as_empty(self) -> None:
        ctx = context("disruption")
        west = next(group for group in ctx["suppliers"]
                    if group["supplier"]["supplier_id"] == "west")
        item = next(item for item in west["items"] if item["sku"] == "PAPER-A4")
        item["price_breaks"] = None
        result = InventoryService(FakeClient({"disruption": ctx})).get_supplier_options(
            "disruption", "PAPER-A4"
        )
        west_option = next(row for row in result if row["supplier_id"] == "west")
        self.assertEqual(west_option["unit_price"], "5.60")
        self.assertEqual(west_option["price_breaks"], [])

    def test_nullable_rpc_collections_are_normalized_as_empty(self) -> None:
        ctx = context("anomaly")
        for key in ("products", "stock", "sales_history", "suppliers", "messages", "open_orders"):
            ctx[key] = None
        service = InventoryService(FakeClient({"anomaly": ctx}))
        self.assertEqual(service.list_inventory("anomaly"), [])
        self.assertEqual(service.get_sales_history("anomaly"), [])
        self.assertEqual(service.get_supplier_messages("anomaly"), [])

        nested = context("anomaly", orders=[{
            "po_id": "null-lines", "status": "sent", "supplier_id": "desk",
            "total": "0.00", "lines": None,
        }])
        for supplier_row in nested["suppliers"]:
            supplier_row["items"] = None
        nested_service = InventoryService(FakeClient({"anomaly": nested}))
        self.assertEqual(nested_service._supplier_items(nested), {})
        self.assertEqual(nested_service.get_supplier_options("anomaly", "PENS-BLUE"), [])
        pen = next(row for row in nested_service.list_inventory("anomaly") if row["sku"] == "PENS-BLUE")
        self.assertEqual(pen["order_history"], [])

    def test_parallel_read_tools_serialize_shared_supabase_client(self) -> None:
        base = FakeClient()
        guard = threading.Lock()
        active = 0
        overlapping: list[bool] = []

        class GuardedRpc:
            def __init__(self, name, params):
                self.name, self.params = name, params

            def execute(self):
                nonlocal active
                with guard:
                    active += 1
                    overlapping.append(active > 1)
                try:
                    time.sleep(0.005)
                    return base.rpc(self.name, self.params).execute()
                finally:
                    with guard:
                        active -= 1

        class GuardedClient:
            def rpc(self, name, params):
                return GuardedRpc(name, params)

        service = InventoryService(GuardedClient())
        calls = [
            lambda: service.list_inventory("disruption"),
            lambda: service.get_supplier_options("disruption", "PAPER-A4"),
            lambda: service.get_supplier_messages("disruption", "PAPER-A4"),
            lambda: service.get_sales_history("disruption", "PAPER-A4"),
            lambda: service.reorder_point("disruption", "PAPER-A4"),
        ]
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            results = list(pool.map(lambda call: call(), calls))
        self.assertEqual(len(results), len(calls))
        self.assertTrue(overlapping)
        self.assertFalse(any(overlapping), "RPC calls overlapped on the shared client")

    def test_transient_read_rpc_retries_but_write_rpc_does_not(self) -> None:
        base = FakeClient()

        class FlakyRpc:
            def __init__(self, name, params, attempts):
                self.name, self.params, self.attempts = name, params, attempts

            def execute(self):
                self.attempts["count"] += 1
                if self.attempts["count"] == 1:
                    raise httpx.ReadError("temporary read failure")
                return base.rpc(self.name, self.params).execute()

        class FlakyClient:
            def __init__(self):
                self.counts = {}

            def rpc(self, name, params):
                attempts = self.counts.setdefault(name, {"count": 0})
                return FlakyRpc(name, params, attempts)

        client = FlakyClient()
        service = InventoryService(client)
        with patch("inventory_agent.service.time.sleep") as sleep:
            self.assertTrue(service.list_inventory("disruption"))
        self.assertEqual(client.counts["inventory_get_context"]["count"], 2)
        sleep.assert_called_once_with(0.1)

        class AlwaysFailingRpc:
            def __init__(self, attempts):
                self.attempts = attempts

            def execute(self):
                self.attempts["count"] += 1
                raise httpx.ReadError("write outcome may be uncertain")

        class WriteClient:
            def __init__(self):
                self.attempts = {"count": 0}

            def rpc(self, name, params):
                return AlwaysFailingRpc(self.attempts)

        write_client = WriteClient()
        with self.assertRaises(httpx.ReadError):
            InventoryService(write_client)._rpc("inventory_create_draft", {}, read_only=False)
        self.assertEqual(write_client.attempts["count"], 1)

    def test_delivered_consolidation_costs_620_vs_three_orders_750(self) -> None:
        lines = [{"sku": sku, "quantity": 100}
                 for sku in ("PENS-BLUE", "NOTEBOOK-A5", "MARKER-BLK")]
        combined = self.service.order_total_cost("consolidation", "desk", lines)
        self.assertEqual(combined["delivered_total"], "620.00")
        separate = sum(Decimal(self.service.order_total_cost("consolidation", "desk", [line])["delivered_total"])
                       for line in lines)
        self.assertEqual(separate, Decimal("750.00"))

    def test_order_cost_applies_all_unit_price_break(self) -> None:
        order = self.service.order_total_cost("disruption", "north",
                                              [{"sku": "PAPER-A4", "quantity": 500}])
        self.assertEqual(order["lines"][0]["unit_price"], "4.80")
        self.assertEqual(order["merchandise_total"], "2400.00")

    def test_supplier_minimum_is_applied_to_delivered_basket(self) -> None:
        with self.assertRaisesRegex(ValueError, "minimum merchandise value"):
            self.service.order_total_cost("anomaly", "desk", [{"sku": "PENS-BLUE", "quantity": 10}])

    def test_invalid_quantities_duplicate_lines_and_moq_rejected(self) -> None:
        cases = [
            ("anomaly", "desk", [{"sku": "PENS-BLUE", "quantity": 1.5}], "positive integer"),
            ("consolidation", "desk", [{"sku": "PENS-BLUE", "quantity": 1},
                                         {"sku": "PENS-BLUE", "quantity": 2}], "duplicate"),
            ("budget", "west", [{"sku": "FOLDER-MAN", "quantity": 6}], "pack size or MOQ"),
            ("budget", "west", [{"sku": "FOLDER-MAN", "quantity": 5}], "pack size or MOQ"),
        ]
        for scenario, supplier, lines, message in cases:
            with self.subTest(lines=lines), self.assertRaisesRegex(ValueError, message):
                self.service.order_total_cost(scenario, supplier, lines)

    def test_cumulative_sku_cap_counts_split_orders(self) -> None:
        ctx = context("disruption", orders=[{
            "po_id": "one", "status": "draft", "total": "3500.00",
            "lines": [{"sku": "PAPER-A4", "quantity": 700}],
        }])
        service = InventoryService(FakeClient({"disruption": ctx}))
        with self.assertRaisesRegex(ValueError, "cumulative SKU cap"):
            service.order_total_cost("disruption", "north", [{"sku": "PAPER-A4", "quantity": 501}])

    def test_injection_quantity_of_10000_is_rejected_by_trusted_cap(self) -> None:
        with self.assertRaisesRegex(ValueError, "cumulative SKU cap"):
            self.service.order_total_cost(
                "injection", "north", [{"sku": "PAPER-A4", "quantity": 10000}]
            )

    def test_two_700_unit_orders_cannot_exceed_1200_cap(self) -> None:
        first_cost = self.service.order_total_cost(
            "disruption", "west", [{"sku": "PAPER-A4", "quantity": 700}]
        )
        self.assertEqual(first_cost["delivered_total"], "3920.00")
        after_first = context("disruption", orders=[{
            "po_id": "first-700", "status": "draft", "supplier_id": "west",
            "idempotency_key": "disruption:west", "total": "3920.00",
            "lines": [{"sku": "PAPER-A4", "quantity": 700}],
        }])
        after_first_service = InventoryService(FakeClient({"disruption": after_first}))
        with self.assertRaisesRegex(ValueError, "cumulative SKU cap"):
            after_first_service.order_total_cost(
                "disruption", "west", [{"sku": "PAPER-A4", "quantity": 700}]
            )

    def test_inventory_lists_existing_orders_before_replenishment(self) -> None:
        ctx = context("anomaly", orders=[{
            "po_id": "existing", "status": "sent", "supplier_id": "desk",
            "idempotency_key": "anomaly:desk", "due_date": "2026-03-03",
            "total": "120.00", "lines": [{"sku": "PENS-BLUE", "quantity": 100}],
        }])
        service = InventoryService(FakeClient({"anomaly": ctx}))
        pen = next(row for row in service.list_inventory("anomaly") if row["sku"] == "PENS-BLUE")
        self.assertEqual(pen["order_history"][0]["po_id"], "existing")
        self.assertEqual(pen["order_history"][0]["status"], "sent")

    def test_reorder_point_uses_position_and_supplier_lead(self) -> None:
        result = self.service.reorder_point("disruption", "PAPER-A4")
        points = {row["supplier_id"]: Decimal(row["reorder_point"])
                  for row in result["supplier_reorder_points"]}
        self.assertEqual(points["north"], Decimal("460.00"))  # 20*21 + 40 safety
        self.assertEqual(points["west"], Decimal("100.00"))
        self.assertEqual(result["inventory_position"], 160)

    def test_stockout_projection_applies_sent_receipt_at_start_of_due_date(self) -> None:
        due = date(2026, 3, 3)
        ctx = context("anomaly", demand=10, stock={"PENS-BLUE": 20}, orders=[{
            "po_id": "sent-1", "status": "sent", "due_date": due.isoformat(), "total": "100.00",
            "lines": [{"sku": "PENS-BLUE", "quantity": 20}],
        }])
        ctx["stock"][0]["on_order"] = 20
        service = InventoryService(FakeClient({"anomaly": ctx}))
        forecast = service.predict_stockout("anomaly", "PENS-BLUE", ["PENS-BLUE:2026-02-14"])
        self.assertEqual(forecast["scheduled_receipts"], [{"date": "2026-03-03", "quantity": 20}])
        self.assertEqual(forecast["stockout_date"], "2026-03-06")

    def test_disruption_plan_switches_from_delayed_north_to_west(self) -> None:
        plan = self.service.plan_orders("disruption")
        lines = [line for group in plan["groups"] for line in group["lines"]]
        paper = next(line for line in lines if line["sku"] == "PAPER-A4")
        group = next(group for group in plan["groups"] if group["supplier_id"] == "west")
        self.assertEqual(paper["supplier_id"], "west")
        self.assertEqual(paper["master_lead_days"], 3)
        self.assertLessEqual(Decimal(plan["planned_total"]), Decimal(plan["remaining_budget"]))
        self.assertGreaterEqual(Decimal(group["delivered_total"]), Decimal("0"))

    def test_budget_plan_is_feasible_and_reduces_in_full_packs(self) -> None:
        ctx = context("budget", demand=1000, stock={"PAPER-A4": 0}, budget="20000.00",
                      orders=[{"po_id": "budget-reservation", "status": "draft", "total": "19900.00",
                               "lines": []}])
        service = InventoryService(FakeClient({"budget": ctx}))
        plan = service.plan_orders("budget")
        self.assertLessEqual(Decimal(plan["planned_total"]), Decimal(plan["remaining_budget"]))
        for group in plan["groups"]:
            for line in group["lines"]:
                self.assertGreaterEqual(line["quantity"], line.get("moq", 1))
                self.assertEqual(line["quantity"] % line.get("pack_size", 1), 0)

    def test_rpc_writes_use_only_narrow_database_functions(self) -> None:
        result = self.service.create_purchase_order(
            "anomaly", "desk", "anomaly:desk", [{"sku": "PENS-BLUE", "quantity": 100}],
        )
        self.assertEqual(result["status"], "draft")
        calls = [name for name, _ in self.client.calls]
        self.assertEqual(calls, ["inventory_get_context", "inventory_create_draft", "inventory_get_context"])
        params = self.client.calls[1][1]
        self.assertEqual(set(params), {"p_scenario", "p_supplier", "p_idempotency_key", "p_lines"})
        self.assertNotIn("budget", params)
        self.assertNotIn("cap", params)
        self.service.send_purchase_order("00000000-0000-0000-0000-000000000002")
        self.assertEqual(self.client.calls[-1][0], "inventory_send_order")

    def test_scenario_sku_sets_match_exercise(self) -> None:
        for scenario, expected in SCENARIO_SKUS.items():
            with self.subTest(scenario=scenario):
                self.assertEqual({row["sku"] for row in self.service.list_inventory(scenario)}, expected)


if __name__ == "__main__":
    unittest.main()
