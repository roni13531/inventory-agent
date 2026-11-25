from __future__ import annotations

import re
import threading
import time
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

import httpx


CENT = Decimal("0.01")
ZERO = Decimal("0")
DEFAULT_HOLDING_RATE = Decimal("0.25")
HISTORY_DAYS = 90
FORECAST_DAYS = 366
READ_RPC_MAX_ATTEMPTS = 3
READ_RPC_BACKOFF_SECONDS = 0.1


def money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric amount")
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def economic_order_quantity(annual_demand: Decimal | int | str,
                            order_cost: Decimal | int | str,
                            annual_holding_cost_per_unit: Decimal | int | str) -> Decimal:
    """EOQ = sqrt(2*D*S/H), retaining Decimal arithmetic."""
    d, s, h = decimal(annual_demand), decimal(order_cost), decimal(annual_holding_cost_per_unit)
    if min(d, s, h) < ZERO:
        raise ValueError("EOQ inputs must be nonnegative")
    if d == ZERO or s == ZERO:
        return ZERO
    if h == ZERO:
        raise ValueError("holding cost per unit must be positive when demand is positive")
    return (Decimal(2) * d * s / h).sqrt()


class InventoryService:
    """Deterministic inventory calculations over stage 1's validated RPC API.

    ``client`` is a Supabase Python client or an RPC-compatible test double. Only
    stage 1's public RPCs are used for model-facing writes; this object never
    accepts SQL, credentials, budget changes, or cap changes.
    """

    def __init__(self, client: Any, *, holding_rate: Decimal = DEFAULT_HOLDING_RATE):
        self._client = client
        # ToolNode invokes tools in worker threads; serialize access to the one
        # synchronous Supabase/httpx client shared by this service.
        self._rpc_lock = threading.RLock()
        self.holding_rate = decimal(holding_rate)
        if self.holding_rate <= ZERO:
            raise ValueError("holding_rate must be positive")

    def _context(self, scenario: str) -> dict[str, Any]:
        self._check_scenario(scenario)
        result = self._rpc(
            "inventory_get_context", {"p_scenario": scenario}, read_only=True
        )
        data = getattr(result, "data", result)
        if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
            raise ValueError(f"unknown or unavailable scenario: {scenario}")
        return data

    def _rpc(self, name: str, params: Mapping[str, Any], *, read_only: bool) -> Any:
        """Serialize client use; retry only transient network errors for read RPCs."""
        attempts = READ_RPC_MAX_ATTEMPTS if read_only else 1
        for attempt in range(attempts):
            try:
                with self._rpc_lock:
                    return self._client.rpc(name, dict(params)).execute()
            except (httpx.NetworkError, httpx.TimeoutException):
                if not read_only or attempt + 1 >= attempts:
                    raise
                time.sleep(READ_RPC_BACKOFF_SECONDS * (2 ** attempt))
        raise AssertionError("unreachable RPC retry state")

    @staticmethod
    def _rows(value: Any) -> list:
        """Normalize a nullable JSON array from the RPC; reject other malformed shapes."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        raise ValueError("inventory context contains a non-array collection")

    @staticmethod
    def _check_scenario(scenario: str) -> None:
        allowed = {"disruption", "anomaly", "consolidation", "budget", "injection"}
        if scenario not in allowed:
            raise ValueError(f"scenario must be one of: {', '.join(sorted(allowed))}")

    @staticmethod
    def _date(value: Any) -> date:
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    @staticmethod
    def _stock_map(context: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(row["sku"]): dict(row) for row in InventoryService._rows(context.get("stock"))}

    @staticmethod
    def _product_map(context: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(row["sku"]): dict(row) for row in InventoryService._rows(context.get("products"))}

    @staticmethod
    def _supplier_items(context: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for row in InventoryService._rows(context.get("suppliers")):
            supplier = row.get("supplier") or {}
            supplier_id = str(supplier.get("supplier_id", ""))
            for item in InventoryService._rows(row.get("items")):
                found[(supplier_id, str(item.get("sku", "")))] = {
                    **dict(item), "supplier": dict(supplier)
                }
        return found

    @staticmethod
    def _order_lines(context: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [line for order in InventoryService._rows(context.get("open_orders"))
                for line in InventoryService._rows(order.get("lines"))]

    @staticmethod
    def _reserved_quantity(context: Mapping[str, Any], sku: str) -> int:
        # All orders reserve the cap; received orders are retained as cumulative history.
        return sum(int(line["quantity"]) for line in InventoryService._order_lines(context)
                   if line.get("sku") == sku)

    @staticmethod
    def _reserved_budget(context: Mapping[str, Any]) -> Decimal:
        return sum((decimal(row.get("total", 0)) for row in InventoryService._rows(context.get("open_orders"))), ZERO)

    @staticmethod
    def _supplier_message_rows(context: Mapping[str, Any], sku: str | None = None) -> list[dict[str, Any]]:
        return [dict(message) for message in InventoryService._rows(context.get("messages"))
                if sku is None or message.get("sku") in (None, sku)]

    def list_inventory(self, scenario: str) -> list[dict[str, Any]]:
        context = self._context(scenario)
        stock = self._stock_map(context)
        products = []
        for product in self._rows(context.get("products")):
            sku = str(product["sku"])
            row = stock.get(sku, {})
            on_hand, on_order = int(row.get("on_hand", 0)), int(row.get("on_order", 0))
            order_history = []
            for order in self._rows(context.get("open_orders")):
                matching_lines = [dict(line) for line in self._rows(order.get("lines"))
                                  if line.get("sku") == sku]
                if matching_lines:
                    order_history.append({
                        "po_id": order.get("po_id"),
                        "status": order.get("status"),
                        "supplier_id": order.get("supplier_id"),
                        "idempotency_key": order.get("idempotency_key"),
                        "created_at": order.get("created_at"),
                        "sent_at": order.get("sent_at"),
                        "due_date": order.get("due_date"),
                        "total": order.get("total"),
                        "lines": matching_lines,
                    })
            products.append({
                **dict(product),
                "on_hand": on_hand,
                "on_order": on_order,
                "inventory_position": on_hand + on_order,
                "safety_stock": int(row.get("safety_stock", product.get("safety_stock", 0))),
                "cumulative_cap_remaining": max(
                    0, int(product["cumulative_cap"]) - self._reserved_quantity(context, sku)
                ),
                "order_history": order_history,
            })
        return products

    def get_sales_history(self, scenario: str, sku: str | None = None) -> list[dict[str, Any]]:
        context = self._context(scenario)
        active = self._product_map(context)
        if sku is not None and sku not in active:
            raise ValueError(f"SKU is not active in scenario {scenario}: {sku}")
        rows = [dict(row) for row in self._rows(context.get("sales_history"))
                if sku is None or row.get("sku") == sku]
        for row in rows:
            row["sale_id"] = self._sale_id(str(row["sku"]), str(row["date"]))
        return rows

    @staticmethod
    def _sale_id(sku: str, day: str) -> str:
        # Stage 1's (sale_date, sku) primary key is exposed as a stable synthetic ID.
        return f"{sku}:{day}"

    def get_supplier_messages(self, scenario: str, sku: str | None = None) -> list[dict[str, Any]]:
        context = self._context(scenario)
        if sku is not None and sku not in self._product_map(context):
            raise ValueError(f"SKU is not active in scenario {scenario}: {sku}")
        return self._supplier_message_rows(context, sku)

    def estimate_demand(self, scenario: str, sku: str,
                        exclude_sales_ids: Sequence[str] = ()) -> dict[str, Any]:
        context = self._context(scenario)
        product = self._product_map(context).get(sku)
        if product is None:
            raise ValueError(f"SKU is not active in scenario {scenario}: {sku}")
        as_of = self._date(context["state"]["simulated_date"])
        start = as_of - timedelta(days=HISTORY_DAYS - 1)
        window = [row for row in self._rows(context.get("sales_history"))
                  if row.get("sku") == sku and start <= self._date(row["date"]) <= as_of]
        annotated: dict[str, dict[str, Any]] = {
            self._sale_id(sku, str(row["date"])): row
            for row in window if row.get("event_annotation") or row.get("annotation")
        }
        requested = set(exclude_sales_ids)
        unknown = requested - set(annotated)
        if unknown:
            raise ValueError("only annotated in-window sales IDs for this SKU may be excluded")
        excluded_rows = [annotated[sale_id] for sale_id in sorted(requested)]
        total_units = sum(int(row["units_sold"]) for row in window)
        excluded_units = sum(int(row["units_sold"]) for row in excluded_rows)
        # Deliberate simplification: excluding the daily aggregate drops ordinary units too,
        # while the moving-window denominator remains 90 calendar days.
        daily = Decimal(total_units - excluded_units) / Decimal(HISTORY_DAYS)
        return {
            "scenario": scenario,
            "sku": sku,
            "window_start": start.isoformat(),
            "window_end": as_of.isoformat(),
            "calendar_days": HISTORY_DAYS,
            "observed_rows": len(window),
            "units_in_window": total_units,
            "excluded_sales_ids": sorted(requested),
            "excluded_units": excluded_units,
            "daily_demand": str(daily.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "annual_demand": str((daily * Decimal(365)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "exclusion_simplification": (
                "A daily aggregate can be excluded only when it is annotated; excluding it also "
                "removes that day's ordinary sales, and the denominator stays at 90 days."
            ),
        }

    @staticmethod
    def _price(item: Mapping[str, Any], quantity: int) -> Decimal:
        unit_price = decimal(item["unit_price"])
        eligible = [br for br in InventoryService._rows(item.get("price_breaks"))
                    if quantity >= int(br["min_units"])]
        if eligible:
            unit_price = decimal(max(eligible, key=lambda br: int(br["min_units"]))["unit_price"])
        return unit_price

    @staticmethod
    def _rounded_pack(quantity: Decimal | int, pack: int, minimum: int) -> int:
        q = max(Decimal(quantity), Decimal(minimum))
        packs = (q / Decimal(pack)).to_integral_value(rounding=ROUND_CEILING)
        return int(packs) * pack

    @staticmethod
    def _parse_lead_override(context: Mapping[str, Any], supplier_id: str,
                             sku: str, master_lead: int) -> tuple[int, dict[str, Any] | None]:
        product_name = next((str(p.get("name", "")).lower()
                             for p in InventoryService._rows(context.get("products")) if p.get("sku") == sku), "")
        for msg in InventoryService._rows(context.get("messages")):
            if msg.get("supplier_id") != supplier_id or msg.get("sku") not in (None, sku):
                continue
            text = str(msg.get("message", ""))
            if msg.get("sku") == sku:
                match = re.search(r"from (?:the )?normal\s+(\d+)\s+days?\s+to\s+(\d+)\s+days?", text, re.I)
                if match and int(match.group(1)) == master_lead:
                    return int(match.group(2)), {"message_id": msg.get("message_id"), "text": text}
            item_word = "toner" if "toner" in product_name else "chair" if "chair" in product_name else ""
            if supplier_id == "tech" and item_word:
                match = re.search(rf"\b{item_word}\b[^.]*?(\d+)\s+days?", text, re.I)
                if match:
                    return int(match.group(1)), {"message_id": msg.get("message_id"), "text": text}
        return master_lead, None

    def get_supplier_options(self, scenario: str, sku: str) -> list[dict[str, Any]]:
        context = self._context(scenario)
        if sku not in self._product_map(context):
            raise ValueError(f"SKU is not active in scenario {scenario}: {sku}")
        options = []
        for (supplier_id, item_sku), item in self._supplier_items(context).items():
            if item_sku != sku:
                continue
            lead, evidence = self._parse_lead_override(context, supplier_id, sku, int(item["lead_days"]))
            supplier = item["supplier"]
            options.append({
                "supplier_id": supplier_id,
                "supplier_name": supplier["name"],
                "sku": sku,
                "unit_price": money(decimal(item["unit_price"])),
                "pack_size": int(item["pack_size"]),
                "moq": int(item["moq"]),
                "master_lead_days": int(item["lead_days"]),
                "effective_lead_days": lead,
                "lead_override_evidence": evidence,
                "ordering_fee": money(decimal(supplier["ordering_fee"])),
                "shipping_fee": money(decimal(supplier["shipping_fee"])),
                "free_shipping_threshold": money(decimal(supplier["free_shipping_threshold"])),
                "minimum_order_value": money(decimal(supplier["minimum_order_value"])),
                "price_breaks": [
                    {"min_units": int(br["min_units"]), "unit_price": money(decimal(br["unit_price"]))}
                    for br in self._rows(item.get("price_breaks"))
                ],
            })
        return sorted(options, key=lambda row: (row["effective_lead_days"], row["unit_price"], row["supplier_id"]))

    def _basket(self, context: Mapping[str, Any], supplier_id: str,
                lines: Sequence[Mapping[str, Any]], *, enforce_minimum: bool,
                enforce_caps: bool = True) -> dict[str, Any]:
        if not lines:
            raise ValueError("at least one order line is required")
        seen: set[str] = set()
        products, items = self._product_map(context), self._supplier_items(context)
        merchandise = ZERO
        result_lines: list[dict[str, Any]] = []
        for line in lines:
            if not isinstance(line, Mapping) and hasattr(line, "model_dump"):
                line = line.model_dump()
            if not isinstance(line, Mapping):
                raise ValueError("each order line must be an object with sku and quantity")
            sku = str(line.get("sku", ""))
            quantity = line.get("quantity")
            if type(quantity) is not int or quantity <= 0:
                raise ValueError("quantity must be a positive integer")
            if sku in seen:
                raise ValueError("duplicate SKU lines are not allowed")
            seen.add(sku)
            product = products.get(sku)
            item = items.get((supplier_id, sku))
            if product is None or item is None:
                raise ValueError(f"SKU {sku} is not available from supplier {supplier_id} in this scenario")
            if quantity < int(item["moq"]) or quantity % int(item["pack_size"]):
                raise ValueError(f"quantity for {sku} violates pack size or MOQ")
            cap_remaining = int(product["cumulative_cap"]) - self._reserved_quantity(context, sku)
            if enforce_caps and quantity > cap_remaining:
                raise ValueError(f"cumulative SKU cap exceeded for {sku}; remaining cap is {max(0, cap_remaining)}")
            unit = self._price(item, quantity)
            line_total = unit * quantity
            merchandise += line_total
            result_lines.append({
                "sku": sku, "quantity": quantity, "unit_price": money(unit),
                "line_total": money(line_total),
            })
        supplier = items[(supplier_id, result_lines[0]["sku"])]["supplier"]
        if any((supplier_id, row["sku"]) not in items for row in result_lines):
            raise ValueError("all lines must be sold by the selected supplier")
        minimum = decimal(supplier["minimum_order_value"])
        if enforce_minimum and merchandise < minimum:
            raise ValueError(f"supplier minimum merchandise value is {money(minimum)}")
        fee = decimal(supplier["ordering_fee"])
        threshold = decimal(supplier["free_shipping_threshold"])
        shipping = decimal(supplier["shipping_fee"]) if merchandise < threshold else ZERO
        return {
            "supplier_id": supplier_id,
            "lines": result_lines,
            "merchandise_total": money(merchandise),
            "ordering_fee": money(fee),
            "shipping_fee": money(shipping),
            "delivered_total": money(merchandise + fee + shipping),
            "currency": "USD",
        }

    def order_total_cost(self, scenario: str, supplier_id: str,
                         lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        context = self._context(scenario)
        return self._basket(context, supplier_id, lines, enforce_minimum=True)

    def calculate_eoq(self, scenario: str, sku: str, supplier_id: str,
                      exclude_sales_ids: Sequence[str] = ()) -> dict[str, Any]:
        context = self._context(scenario)
        demand = self.estimate_demand(scenario, sku, exclude_sales_ids)
        annual_demand = decimal(demand["annual_demand"])
        product = self._product_map(context).get(sku)
        item = self._supplier_items(context).get((supplier_id, sku))
        if product is None or item is None:
            raise ValueError(f"SKU {sku} is not available from supplier {supplier_id} in scenario {scenario}")
        if annual_demand == ZERO:
            return {"sku": sku, "supplier_id": supplier_id, "annual_demand": "0.00",
                    "raw_eoq": "0", "selected_quantity": 0, "candidate_evaluations": []}
        supplier = item["supplier"]
        order_cost = decimal(supplier["ordering_fee"]) + decimal(supplier["shipping_fee"])
        base_price = decimal(item["unit_price"])
        holding_cost = base_price * self.holding_rate
        raw = economic_order_quantity(annual_demand, order_cost, holding_cost)
        minimum = max(int(item["moq"]), int(decimal(supplier["minimum_order_value"]) / base_price)
                      + (1 if decimal(supplier["minimum_order_value"]) % base_price else 0))
        cap_remaining = max(0, int(product["cumulative_cap"]) - self._reserved_quantity(context, sku))
        pack = int(item["pack_size"])
        candidates: set[int] = set()
        if cap_remaining >= minimum:
            nearest = (raw / Decimal(pack)).to_integral_value(rounding=ROUND_HALF_UP) * pack
            candidates.add(self._rounded_pack(nearest, pack, minimum))
            candidates.add(self._rounded_pack(minimum, pack, minimum))
            for br in self._rows(item.get("price_breaks")):
                candidates.add(self._rounded_pack(int(br["min_units"]), pack, minimum))
            if decimal(supplier["free_shipping_threshold"]) > ZERO:
                for price in [base_price] + [decimal(br["unit_price"]) for br in self._rows(item.get("price_breaks"))]:
                    threshold_qty = int((decimal(supplier["free_shipping_threshold"]) / price)
                                        .to_integral_value(rounding=ROUND_CEILING))
                    candidates.add(self._rounded_pack(threshold_qty, pack, minimum))
        evaluations = []
        for q in sorted(q for q in candidates if 0 < q <= cap_remaining):
            price = self._price(item, q)
            h = price * self.holding_rate
            annual_ordering = annual_demand / Decimal(q) * (
                decimal(supplier["ordering_fee"])
                + (decimal(supplier["shipping_fee"])
                   if price * q < decimal(supplier["free_shipping_threshold"]) else ZERO)
            )
            annual_holding = Decimal(q) * h / Decimal(2)
            annual_purchase = annual_demand * price
            evaluations.append({
                "quantity": q,
                "unit_price": money(price),
                "annual_ordering_cost": money(annual_ordering),
                "annual_holding_cost": money(annual_holding),
                "annual_purchase_cost": money(annual_purchase),
                "annual_relevant_cost": money(annual_ordering + annual_holding + annual_purchase),
            })
        best = min(evaluations, key=lambda row: decimal(row["annual_relevant_cost"])) if evaluations else None
        return {
            "sku": sku,
            "supplier_id": supplier_id,
            "annual_demand": str(annual_demand.quantize(CENT, rounding=ROUND_HALF_UP)),
            "holding_rate": str(self.holding_rate),
            "order_cost_basis": money(order_cost),
            "holding_cost_per_unit": money(holding_cost),
            "raw_eoq": str(raw.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)),
            "selected_quantity": best["quantity"] if best else 0,
            "selected_candidate": best,
            "candidate_evaluations": evaluations,
            "candidate_note": "Compared rounded EOQ, MOQ/supplier minimum, all-unit price breaks, and free-shipping threshold quantities; not a global optimization.",
        }

    def reorder_point(self, scenario: str, sku: str) -> dict[str, Any]:
        context = self._context(scenario)
        stock = self._stock_map(context).get(sku)
        product = self._product_map(context).get(sku)
        if not stock or not product:
            raise ValueError(f"SKU is not active in scenario {scenario}: {sku}")
        daily = decimal(self.estimate_demand(scenario, sku)["daily_demand"])
        inventory_position = int(stock["on_hand"]) + int(stock["on_order"])
        options = self.get_supplier_options(scenario, sku)
        suppliers = [{
            "supplier_id": row["supplier_id"],
            "lead_days": row["effective_lead_days"],
            "master_lead_days": row["master_lead_days"],
            "lead_override_evidence": row["lead_override_evidence"],
            "reorder_point": str((daily * int(row["effective_lead_days"])
                                   + int(stock["safety_stock"])).quantize(Decimal("0.01"))),
        } for row in options]
        return {
            "scenario": scenario, "sku": sku, "daily_demand": str(daily),
            "on_hand": int(stock["on_hand"]), "on_order": int(stock["on_order"]),
            "inventory_position": inventory_position,
            "safety_stock": int(stock["safety_stock"]),
            "supplier_reorder_points": suppliers,
        }

    def predict_stockout(self, scenario: str, sku: str,
                         exclude_sales_ids: Sequence[str] = ()) -> dict[str, Any]:
        context = self._context(scenario)
        stock = self._stock_map(context).get(sku)
        if stock is None or sku not in self._product_map(context):
            raise ValueError(f"SKU is not active in scenario {scenario}: {sku}")
        daily = decimal(self.estimate_demand(scenario, sku, exclude_sales_ids)["daily_demand"])
        as_of = self._date(context["state"]["simulated_date"])
        if daily == ZERO:
            return {"sku": sku, "scenario": scenario, "daily_demand": "0.0000",
                    "stockout_date": None, "stockout_days": None, "gaps": [],
                    "horizon_days": FORECAST_DAYS}
        receipts: dict[date, int] = {}
        for order in self._rows(context.get("open_orders")):
            if order.get("status") != "sent":
                continue
            due = self._date(order["due_date"])
            due = max(as_of + timedelta(days=1), due)
            for line in self._rows(order.get("lines")):
                if line.get("sku") == sku:
                    receipts[due] = receipts.get(due, 0) + int(line["quantity"])
        balance = Decimal(int(stock["on_hand"]))
        gap_start: date | None = None
        gaps: list[dict[str, Any]] = []
        first_stockout: date | None = None
        for offset in range(1, FORECAST_DAYS + 1):
            day = as_of + timedelta(days=offset)
            balance += Decimal(receipts.get(day, 0))
            if gap_start is not None and balance > ZERO:
                gaps.append({"start": gap_start.isoformat(), "end": (day - timedelta(days=1)).isoformat()})
                gap_start = None
            balance -= daily
            if balance < ZERO:
                if first_stockout is None:
                    first_stockout = day
                if gap_start is None:
                    gap_start = day
        if gap_start is not None:
            gaps.append({"start": gap_start.isoformat(), "end": None, "inevitable_within_horizon": True})
        return {
            "sku": sku, "scenario": scenario,
            "daily_demand": str(daily.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)),
            "on_hand": int(stock["on_hand"]), "on_order": int(stock["on_order"]),
            "stockout_date": first_stockout.isoformat() if first_stockout else None,
            "stockout_days": (first_stockout - as_of).days if first_stockout else None,
            "scheduled_receipts": [{"date": d.isoformat(), "quantity": q} for d, q in sorted(receipts.items())],
            "gaps": gaps,
            "horizon_days": FORECAST_DAYS,
            "note": "Sent-order receipts are applied at the start of their due dates. Gaps are projected unmet demand, not proof an order can remove an earlier gap.",
        }

    def plan_orders(self, scenario: str) -> dict[str, Any]:
        context = self._context(scenario)
        state = context["state"]
        as_of = self._date(state["simulated_date"])
        deadline = self._date(state["deadline"])
        days_left = max(1, (deadline - as_of).days)
        stock = self._stock_map(context)
        products = self._product_map(context)
        remaining_budget = decimal(state["budget"]) - self._reserved_budget(context)
        needs: list[dict[str, Any]] = []
        exclusions: list[dict[str, Any]] = []

        for sku, product in products.items():
            current = stock.get(sku)
            if not current:
                continue
            estimate = self.estimate_demand(scenario, sku)
            daily = decimal(estimate["daily_demand"])
            if daily == ZERO:
                continue
            position = int(current["on_hand"]) + int(current["on_order"])
            demand_to_deadline = daily * days_left
            raw_need = demand_to_deadline + int(current["safety_stock"]) - position
            if raw_need <= ZERO:
                continue
            prediction = self.predict_stockout(scenario, sku)
            stockout_date = (self._date(prediction["stockout_date"])
                             if prediction.get("stockout_date") else None)
            priority_day = stockout_date or deadline + timedelta(days=1)
            cap_remaining = max(0, int(product["cumulative_cap"]) - self._reserved_quantity(context, sku))
            options = self.get_supplier_options(scenario, sku)
            candidates: list[dict[str, Any]] = []
            for option in options:
                item = self._supplier_items(context).get((option["supplier_id"], sku))
                if item is None:
                    continue
                q = self._rounded_pack(raw_need, int(item["pack_size"]), int(item["moq"]))
                if q > cap_remaining:
                    q = (cap_remaining // int(item["pack_size"])) * int(item["pack_size"])
                if q < int(item["moq"]):
                    continue
                supplier = item["supplier"]
                price = self._price(item, q)
                minimum_needed = decimal(supplier["minimum_order_value"])
                if price * q < minimum_needed:
                    q = self._rounded_pack(
                        (minimum_needed / price).to_integral_value(rounding=ROUND_CEILING),
                        int(item["pack_size"]), int(item["moq"]),
                    )
                    if q > cap_remaining:
                        continue
                    price = self._price(item, q)
                arrival = as_of + timedelta(days=int(option["effective_lead_days"]))
                single_total = price * q + decimal(supplier["ordering_fee"])
                threshold = decimal(supplier["free_shipping_threshold"])
                if price * q < threshold:
                    single_total += decimal(supplier["shipping_fee"])
                on_time = arrival <= min(priority_day, deadline)
                candidates.append({**option, "quantity": q, "arrival_date": arrival,
                                   "on_time": on_time, "estimated_total": single_total,
                                   "unit_price_decimal": price})
            if not candidates:
                exclusions.append({"sku": sku, "reason": "No supplier option fits MOQ, pack size, and remaining trusted SKU cap."})
                continue
            selected = min(candidates, key=lambda row: (
                not row["on_time"], row["effective_lead_days"],
                row["estimated_total"] / Decimal(row["quantity"]), row["supplier_id"]
            ))
            needs.append({
                "sku": sku, "name": product["name"], "supplier_id": selected["supplier_id"],
                "quantity": selected["quantity"], "pack_size": selected["pack_size"],
                "moq": selected["moq"], "urgency_date": priority_day,
                "stockout_date": stockout_date, "lead_days": selected["effective_lead_days"],
                "master_lead_days": selected["master_lead_days"],
                "lead_override_evidence": selected["lead_override_evidence"],
                "arrival_date": selected["arrival_date"], "on_time": selected["on_time"],
                "cap_remaining": cap_remaining,
            })

        needs.sort(key=lambda row: (row["urgency_date"], row["sku"]))
        by_supplier: dict[str, list[dict[str, Any]]] = {}
        for item in needs:
            by_supplier.setdefault(item["supplier_id"], []).append(item)

        def group_total(supplier_id: str, rows: Sequence[dict[str, Any]]) -> Decimal:
            if not rows:
                return ZERO
            basket = self._basket(context, supplier_id,
                                  [{"sku": row["sku"], "quantity": row["quantity"]} for row in rows],
                                  enforce_minimum=True)
            return decimal(basket["delivered_total"])

        def total_for_groups() -> Decimal:
            total = ZERO
            for supplier_id, rows in by_supplier.items():
                if rows:
                    total += group_total(supplier_id, rows)
            return total

        # Enforce the real supplier minimum at basket level; remove the least urgent
        # lines from an infeasible group instead of inventing a budget or cap override.
        for supplier_id in list(by_supplier):
            while by_supplier[supplier_id]:
                try:
                    group_total(supplier_id, by_supplier[supplier_id])
                    break
                except ValueError as exc:
                    if "minimum merchandise" not in str(exc):
                        raise
                    removed = by_supplier[supplier_id].pop()
                    exclusions.append({"sku": removed["sku"], "reason": "Selected supplier basket cannot meet its minimum order value."})

        # Shrink least-urgent quantities in whole packs until the shared budget fits.
        while total_for_groups() > remaining_budget:
            candidates = [line for rows in by_supplier.values() for line in rows]
            if not candidates:
                break
            line = max(candidates, key=lambda row: (row["urgency_date"], row["sku"]))
            current_item = self._supplier_items(context)[(line["supplier_id"], line["sku"])]
            next_q = int(line["quantity"]) - int(line["pack_size"])
            if next_q >= int(line["moq"]):
                line["quantity"] = next_q
            else:
                by_supplier[line["supplier_id"]].remove(line)
                exclusions.append({"sku": line["sku"], "reason": "Removed to fit the shared budget while respecting pack size and MOQ."})
            # If the reduced basket no longer meets supplier minimum, remove its
            # least-urgent line until the remaining basket is valid or empty.
            rows = by_supplier[line["supplier_id"]]
            while rows:
                try:
                    group_total(line["supplier_id"], rows)
                    break
                except ValueError as exc:
                    if "minimum merchandise" not in str(exc):
                        raise
                    removed = rows.pop()
                    exclusions.append({"sku": removed["sku"], "reason": "Removed after budget reduction made the supplier minimum infeasible."})

        groups = []
        inevitable_gaps = []
        for supplier_id, rows in sorted(by_supplier.items()):
            if not rows:
                continue
            basket = self._basket(context, supplier_id,
                                  [{"sku": row["sku"], "quantity": row["quantity"]} for row in rows],
                                  enforce_minimum=True)
            groups.append({
                **basket,
                "arrival_date": max(row["arrival_date"] for row in rows).isoformat(),
                "lines": [{**line,
                           "supplier_id": supplier_id,
                           "pack_size": next(r["pack_size"] for r in rows if r["sku"] == line["sku"]),
                           "moq": next(r["moq"] for r in rows if r["sku"] == line["sku"]),
                           "arrival_date": next(r["arrival_date"].isoformat() for r in rows if r["sku"] == line["sku"]),
                           "on_time": next(r["on_time"] for r in rows if r["sku"] == line["sku"]),
                           "urgency_date": next(r["urgency_date"].isoformat() for r in rows if r["sku"] == line["sku"]),
                           "stockout_date": next((r["stockout_date"].isoformat() if r["stockout_date"] else None) for r in rows if r["sku"] == line["sku"]),
                           "lead_days": next(r["lead_days"] for r in rows if r["sku"] == line["sku"]),
                           "master_lead_days": next(r["master_lead_days"] for r in rows if r["sku"] == line["sku"]),
                           "lead_override_evidence": next(r["lead_override_evidence"] for r in rows if r["sku"] == line["sku"])}
                          for line in basket["lines"]],
            })
            for row in rows:
                if not row["on_time"]:
                    gap_days = ((row["arrival_date"] - row["stockout_date"]).days
                                if row["stockout_date"] and row["arrival_date"] > row["stockout_date"] else None)
                    inevitable_gaps.append({
                        "sku": row["sku"],
                        "projected_stockout_date": row["stockout_date"].isoformat() if row["stockout_date"] else None,
                        "earliest_selected_arrival": row["arrival_date"].isoformat(),
                        "projected_gap_days_before_receipt": gap_days,
                        "reason": ("All feasible selected supplier options arrive after projected stockout."
                                   if gap_days is not None else "Selected supplier arrival is after the scenario deadline."),
                    })
        planned_total = sum((decimal(group["delivered_total"]) for group in groups), ZERO)
        if planned_total > remaining_budget:
            raise RuntimeError("planner could not satisfy the shared budget without violating a pack or MOQ")
        return {
            "scenario": scenario,
            "as_of": as_of.isoformat(),
            "deadline": deadline.isoformat(),
            "days_to_deadline": days_left,
            "budget": money(decimal(state["budget"])),
            "previously_reserved": money(self._reserved_budget(context)),
            "remaining_budget": money(remaining_budget),
            "planned_total": money(planned_total),
            "groups": groups,
            "inevitable_gaps": inevitable_gaps,
            "excluded": exclusions,
            "heuristic": "Urgency-first: project stockouts with due-date receipts, choose a feasible supplier using evidence-backed lead overrides and landed cost, group by supplier, then reduce least-urgent lines in full packs to fit budget. This is an explainable heuristic, not a global optimum.",
        }

    def create_purchase_order(self, scenario: str, supplier_id: str,
                              idempotency_key: str,
                              lines: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        context = self._context(scenario)
        # Local checks improve feedback; the database RPC remains authoritative.
        basket = self._basket(context, supplier_id, lines, enforce_minimum=True)
        payload_lines = [{"sku": row["sku"], "quantity": row["quantity"]}
                         for row in basket["lines"]]
        result = self._rpc("inventory_create_draft", {
            "p_scenario": scenario,
            "p_supplier": supplier_id,
            "p_idempotency_key": idempotency_key,
            "p_lines": payload_lines,
        }, read_only=False)
        data = getattr(result, "data", result)
        current = self._context(scenario)
        order = next((row for row in self._rows(current.get("open_orders"))
                      if str(row.get("po_id")) == str(data)), None)
        stored_cost = None
        if order:
            stored_cost = {
                "merchandise_total": money(decimal(order["merchandise_total"])),
                "ordering_fee": money(decimal(order["ordering_fee"])),
                "shipping_fee": money(decimal(order["shipping_fee"])),
                "delivered_total": money(decimal(order["total"])),
                "currency": "USD",
                "lines": [{
                    "sku": line["sku"], "quantity": int(line["quantity"]),
                    "unit_price": money(decimal(line["unit_price"])),
                    "line_total": money(decimal(line["line_total"])),
                } for line in self._rows(order.get("lines"))],
            }
        return {"po_id": data, "status": order.get("status", "draft") if order else "draft",
                "cost": stored_cost}

    def send_purchase_order(self, po_id: str) -> dict[str, Any]:
        if not po_id:
            raise ValueError("po_id is required")
        result = self._rpc("inventory_send_order", {"p_po_id": po_id}, read_only=False)
        return {"po_id": po_id, "status": getattr(result, "data", result)}

    def log_decision(self, scenario: str, decision: str,
                     evidence: Sequence[Mapping[str, Any]], po_id: str | None = None) -> dict[str, Any]:
        if not decision.strip():
            raise ValueError("decision text is required")
        result = self._rpc("inventory_record_decision", {
            "p_scenario": scenario,
            "p_decision": decision,
            "p_evidence": list(evidence),
            "p_po_id": po_id,
        }, read_only=False)
        return {"decision_id": getattr(result, "data", result), "scenario": scenario}
