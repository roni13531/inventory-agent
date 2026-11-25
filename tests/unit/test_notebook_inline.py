from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from langchain_core.messages import AIMessage


ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = ROOT / "notebooks" / "inventory_agent_stage4.ipynb"


class ScriptedModel:
    """Offline tool-calling double; it never contacts a model provider."""

    def bind_tools(self, tools):
        self.tools = tools
        return self

    def invoke(self, messages):
        if not any(getattr(message, "type", None) == "tool" for message in messages):
            return AIMessage(content="", tool_calls=[{
                "name": "list_inventory", "args": {"scenario": "anomaly"},
                "id": "scripted-list-1", "type": "tool_call",
            }])
        return AIMessage(content="Scripted analysis complete; no order was created.")


class InlineNotebookTests(unittest.TestCase):
    def test_inline_graph_runs_without_repository_imports_or_model_cost(self):
        notebook = json.loads(NOTEBOOK.read_text())
        cells = notebook["cells"]
        # These generated cells are the complete inlined application modules.
        namespace = {"__name__": "__main__"}
        module_prefixes = (
            "# Inventory calculations", "# Supabase client",
            "# Model-facing StructuredTools", "# Notebook-only reset and clock utilities",
            "# Explicit LangGraph agent and persistence",
        )
        module_cells = [
            (index, cell) for index, cell in enumerate(cells)
            if cell["cell_type"] == "code"
            and any("".join(cell["source"]).startswith(prefix) for prefix in module_prefixes)
        ]
        self.assertEqual(len(module_cells), len(module_prefixes))
        for index, cell in module_cells:
            text = "".join(cell["source"])
            self.assertNotIn("inventory_agent.", text)
            compile(text, f"notebook-cell-{index}", "exec")
            exec(compile(text, f"notebook-cell-{index}", "exec"), namespace)

        class Rpc:
            def execute(self):
                fixture = {
                    "state": {"scenario": "anomaly", "run_id": "offline",
                              "simulated_date": "2026-03-01", "deadline": "2026-03-15",
                              "currency": "USD", "budget": "20000.00"},
                    "products": [{"sku": "PENS-BLUE", "name": "Blue pens",
                                  "active_scenarios": ["anomaly"], "cumulative_cap": 20,
                                  "safety_stock": 2}],
                    "stock": [{"sku": "PENS-BLUE", "on_hand": 10, "on_order": 0,
                               "safety_stock": 2}],
                    "sales_history": [], "suppliers": [], "messages": [], "open_orders": [],
                }
                return type("Result", (), {"data": fixture})()

        class Client:
            def rpc(self, name, params):
                if name != "inventory_get_context":
                    raise AssertionError(f"unexpected RPC {name}")
                return Rpc()

        service = namespace["InventoryService"](Client())
        tools = namespace["build_model_tools"](service)
        namespace["assert_model_tool_surface"](tools)
        graph = namespace["build_agent_graph"](ScriptedModel(), tools)
        output = io.StringIO()
        with redirect_stdout(output):
            namespace["run_turn"](graph, "Inspect inventory only.", "scripted:offline")
        rendered = output.getvalue()
        self.assertIn("AGENT MESSAGE:", rendered)
        self.assertIn("TOOL CALL [list_inventory]", rendered)
        self.assertIn("TOOL RESULT [list_inventory]", rendered)
        self.assertIn("Scripted analysis complete", rendered)

    def test_notebook_structure_and_outputs_are_clean(self):
        notebook = json.loads(NOTEBOOK.read_text())
        self.assertEqual(notebook["nbformat"], 4)
        self.assertGreaterEqual(notebook["nbformat_minor"], 5)
        ids = [cell.get("id") for cell in notebook["cells"]]
        # Cell IDs are optional in nbformat 4.5, but when present they must be unique.
        present_ids = [item for item in ids if item]
        self.assertEqual(len(present_ids), len(set(present_ids)))
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])
                text = "".join(cell["source"])
                if not text.lstrip().startswith("%"):
                    compile(text, "notebook-code-cell", "exec")
                self.assertNotIn("from inventory_agent", text)
                self.assertNotIn("import inventory_agent", text)
        all_text = "\n".join("".join(c.get("source", [])) for c in notebook["cells"])
        for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "SUPABASE_URL", "SUPABASE_KEY", "SUPABASE_DB_URI"):
            self.assertIn(name, all_text)
        self.assertIn("reset_and_start_scenario", all_text)
        self.assertIn("advance_clock", all_text)
        self.assertIn("receive_due", all_text)
        self.assertIn("close_database_session", all_text)


if __name__ == "__main__":
    unittest.main()
