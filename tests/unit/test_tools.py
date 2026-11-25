from __future__ import annotations

import unittest

try:
    from inventory_agent.tools import build_model_tools
    from tests.helpers import FakeClient
    from inventory_agent.service import InventoryService
    TOOL_DEPS_AVAILABLE = True
except ImportError:
    TOOL_DEPS_AVAILABLE = False


@unittest.skipUnless(TOOL_DEPS_AVAILABLE, "Install pinned langchain-core to test StructuredTool schemas.")
class StructuredToolTests(unittest.TestCase):
    def test_exact_model_tool_surface_and_safe_schemas(self) -> None:
        tools = build_model_tools(InventoryService(FakeClient()))
        self.assertEqual(len(tools), 13)
        expected = {
            "list_inventory", "get_sales_history", "get_supplier_options", "get_supplier_messages",
            "estimate_demand", "calculate_eoq", "reorder_point", "predict_stockout",
            "order_total_cost", "plan_orders", "create_purchase_order", "send_purchase_order",
            "log_decision",
        }
        self.assertEqual({tool.name for tool in tools}, expected)
        schemas = {tool.name: set(tool.args_schema.model_fields) for tool in tools}
        self.assertNotIn("reset_scenario", schemas)
        self.assertNotIn("receive_due", schemas)
        self.assertNotIn("advance_clock", schemas)
        forbidden = {
            "client_key", "api_key", "supabase_key", "service_role_key", "sql",
            "budget", "budget_override", "quantity_cap", "cap", "cap_override",
            "cumulative_cap",
        }
        for schema in schemas.values():
            self.assertTrue(forbidden.isdisjoint(schema), f"unsafe tool fields: {forbidden & schema}")


if __name__ == "__main__":
    unittest.main()
