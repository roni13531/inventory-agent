from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any


SCENARIO_SKUS = {
    "disruption": {"PAPER-A4"},
    "anomaly": {"PENS-BLUE"},
    "consolidation": {"PENS-BLUE", "NOTEBOOK-A5", "MARKER-BLK"},
    "budget": {"PAPER-A4", "PENS-BLUE", "NOTEBOOK-A5", "MARKER-BLK", "TONER-BLK", "CHAIR-ERG",
               "FOLDER-MAN", "BINDER-1IN", "STAPLER-M", "ENVELOPE-L", "STICKY-Y", "CLIP-PAPER"},
    "injection": {"PAPER-A4"},
}


def context(scenario: str, *, demand: int = 10, stock: dict[str, int] | None = None,
            budget: str = "20000.00", orders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    products_data = [
        ("PAPER-A4", "A4 copy paper, 500-sheet box", 1200, 40),
        ("PENS-BLUE", "Blue ballpoint pens, box of 50", 2000, 30),
        ("NOTEBOOK-A5", "A5 ruled notebooks", 1500, 20),
        ("MARKER-BLK", "Black dry-erase markers", 1200, 20),
        ("TONER-BLK", "Laser printer toner, black", 200, 5),
        ("CHAIR-ERG", "Ergonomic task chair", 100, 2),
        ("FOLDER-MAN", "Manila folders, pack of 25", 800, 15),
        ("BINDER-1IN", "One-inch binders", 500, 10),
        ("STAPLER-M", "Desktop stapler", 300, 5),
        ("ENVELOPE-L", "Large mailing envelopes, box", 600, 10),
        ("STICKY-Y", "Yellow sticky notes, pad", 1000, 20),
        ("CLIP-PAPER", "Paper clips, 100-count box", 1000, 20),
    ]
    products = [{"sku": sku, "name": name, "category": "office", "unit": "unit",
                 "active_scenarios": sorted([s for s, skus in SCENARIO_SKUS.items() if sku in skus]),
                 "cumulative_cap": cap, "safety_stock": safety}
                for sku, name, cap, safety in products_data if sku in SCENARIO_SKUS[scenario]]
    stock_default = {"PAPER-A4": 160, "PENS-BLUE": 90, "NOTEBOOK-A5": 65, "MARKER-BLK": 55,
                     "TONER-BLK": 18, "CHAIR-ERG": 8, "FOLDER-MAN": 75, "BINDER-1IN": 48,
                     "STAPLER-M": 22, "ENVELOPE-L": 70, "STICKY-Y": 120, "CLIP-PAPER": 140}
    stock_default.update(stock or {})
    stock_rows = [{"sku": p["sku"], "on_hand": stock_default[p["sku"]],
                   "on_order": 0, "safety_stock": p["safety_stock"]} for p in products]

    suppliers_spec = [
        ("north", "North Paper Co.", 0, 0, 0, 0),
        ("west", "West Wholesale", 0, 0, 0, 0),
        ("desk", "Desk Essentials", 20, 30, 600, 100),
        ("tech", "Tech Depot", 30, 60, 5000, 100),
    ]
    suppliers = {sid: {"supplier_id": sid, "name": name, "ordering_fee": str(fee),
                       "shipping_fee": str(ship), "free_shipping_threshold": str(threshold),
                       "minimum_order_value": str(minimum), "currency": "USD"}
                 for sid, name, fee, ship, threshold, minimum in suppliers_spec}
    matrix = {
        ("north", "PAPER-A4"): ("5.00", 1, 1, 2), ("west", "PAPER-A4"): ("5.60", 1, 1, 3),
        ("desk", "PENS-BLUE"): ("1.00", 1, 1, 2), ("north", "PENS-BLUE"): ("1.08", 1, 1, 2),
        ("desk", "NOTEBOOK-A5"): ("3.00", 1, 1, 2), ("west", "NOTEBOOK-A5"): ("3.20", 1, 1, 3),
        ("desk", "MARKER-BLK"): ("2.00", 1, 1, 2), ("west", "MARKER-BLK"): ("2.15", 1, 1, 3),
        ("tech", "TONER-BLK"): ("125.00", 1, 1, 4), ("west", "TONER-BLK"): ("129.00", 1, 1, 5),
        ("tech", "CHAIR-ERG"): ("180.00", 1, 1, 4), ("north", "CHAIR-ERG"): ("185.00", 1, 1, 3),
        ("west", "FOLDER-MAN"): ("8.00", 5, 10, 3), ("west", "BINDER-1IN"): ("2.50", 1, 1, 3),
        ("desk", "STAPLER-M"): ("7.00", 1, 1, 2), ("west", "ENVELOPE-L"): ("12.00", 1, 1, 3),
        ("desk", "STICKY-Y"): ("1.50", 1, 1, 2), ("west", "CLIP-PAPER"): ("1.25", 1, 1, 3),
    }
    breaks = {("north", "PAPER-A4"): [(500, "4.80")],
              ("desk", "PENS-BLUE"): [(500, "0.95")],
              ("tech", "TONER-BLK"): [(100, "114.00")],
              ("tech", "CHAIR-ERG"): [(50, "171.00")]}
    grouped: dict[str, list[dict[str, Any]]] = {sid: [] for sid in suppliers}
    for (sid, sku), (price, pack, moq, lead) in matrix.items():
        if sku not in SCENARIO_SKUS[scenario]:
            continue
        grouped[sid].append({"sku": sku, "unit_price": price, "pack_size": pack, "moq": moq,
                             "lead_days": lead,
                             "price_breaks": [{"min_units": q, "unit_price": p}
                                               for q, p in breaks.get((sid, sku), [])]})
    supplier_rows = [{"supplier": suppliers[sid], "items": items}
                     for sid, items in grouped.items() if items]

    start = date(2026, 3, 1) - timedelta(days=179)
    history = []
    for p in products:
        base = 20 if p["sku"] == "PAPER-A4" else demand
        for day_num in range(180):
            day = start + timedelta(days=day_num)
            units = base
            annotation = None
            if p["sku"] == "PENS-BLUE" and day == date(2026, 2, 14):
                units += 900
                annotation = "Includes one-off 900-box event; exclude those 900 units for ordinary daily demand."
            history.append({"date": day.isoformat(), "sku": p["sku"], "units_sold": units,
                            "annotation": annotation})
    messages = []
    if scenario in {"disruption", "injection"}:
        messages.append({"message_id": 1, "scenario": scenario, "supplier_id": "north", "sku": "PAPER-A4",
                         "message": "A mill disruption has extended the A4 paper master lead time from the normal 2 days to 21 days."})
    if scenario == "anomaly":
        messages.append({"message_id": 2, "scenario": scenario, "supplier_id": "desk", "sku": "PENS-BLUE",
                         "message": "The February 14 shipment was a one-off 900-box event; it is not recurring demand."})
    if scenario == "consolidation":
        messages.append({"message_id": 3, "scenario": scenario, "supplier_id": "desk", "sku": None,
                         "message": "Pens, notebooks, and markers ship together; free shipping at $600 merchandise."})
    if scenario == "budget":
        messages.append({"message_id": 4, "scenario": scenario, "supplier_id": "tech", "sku": None,
                         "message": "Normal lead-time availability is confirmed: toner ships in 4 days and chairs in 4 days."})
    open_orders = orders or []
    return {
        "state": {"scenario": scenario, "run_id": "00000000-0000-0000-0000-000000000001",
                  "simulated_date": "2026-03-01", "deadline": "2026-03-15", "currency": "USD",
                  "budget": budget},
        "products": products,
        "stock": stock_rows,
        "sales_history": history,
        "suppliers": supplier_rows,
        "messages": messages,
        "open_orders": open_orders,
    }


class FakeRpc:
    def __init__(self, client: "FakeClient", name: str, params: dict[str, Any]):
        self.client, self.name, self.params = client, name, params

    def execute(self) -> SimpleNamespace:
        self.client.calls.append((self.name, self.params))
        if self.name == "inventory_get_context":
            return SimpleNamespace(data=self.client.contexts[self.params["p_scenario"]])
        if self.name == "inventory_create_draft":
            return SimpleNamespace(data="00000000-0000-0000-0000-000000000002")
        if self.name == "inventory_send_order":
            return SimpleNamespace(data="sent")
        if self.name == "inventory_record_decision":
            return SimpleNamespace(data=1)
        raise AssertionError(f"Unexpected RPC: {self.name}")


class FakeClient:
    def __init__(self, contexts: dict[str, dict[str, Any]] | None = None):
        self.contexts = contexts or {scenario: context(scenario) for scenario in SCENARIO_SKUS}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(self, name: str, params: dict[str, Any]) -> FakeRpc:
        return FakeRpc(self, name, params)
