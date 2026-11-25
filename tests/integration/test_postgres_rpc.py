from __future__ import annotations

import os
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"),
                     "Set TEST_DATABASE_URL to an explicitly empty local PostgreSQL database.")
class PostgresRpcIntegrationTests(unittest.TestCase):
    """Exercise the saved stage-1 SQL only against an explicitly configured empty local DB."""

    @classmethod
    def setUpClass(cls) -> None:
        from psycopg import connect

        cls.uri = os.environ["TEST_DATABASE_URL"]
        hostname = (urlsplit(cls.uri).hostname or "").lower()
        if hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("TEST_DATABASE_URL must point to a local empty PostgreSQL database; remote targets are refused.")
        cls.conn = connect(cls.uri, autocommit=True)
        existing = cls.conn.execute("""
            select count(*) from information_schema.schemata
            where schema_name in ('inventory','inventory_checkpoints')
        """).fetchone()[0]
        if existing:
            raise RuntimeError("TEST_DATABASE_URL is not empty: inventory schemas already exist.")
        public_tables = cls.conn.execute("""
            select count(*) from information_schema.tables
            where table_schema='public' and table_type='BASE TABLE'
        """).fetchone()[0]
        if public_tables:
            raise RuntimeError("TEST_DATABASE_URL is not empty: public tables already exist.")

        root = Path(__file__).resolve().parents[2]
        migration_paths = sorted((root / "supabase" / "migrations").glob("*.sql"))
        for path in migration_paths:
            sql = path.read_text(encoding="utf-8")
            # A local PostgreSQL test DB has no Supabase-managed API roles. Strip only
            # GRANT/REVOKE/default-privilege statements; the tested functions/tables are identical.
            sql = "\n".join(
                line for line in sql.splitlines()
                if not re.match(r"\s*(GRANT|REVOKE|ALTER DEFAULT PRIVILEGES)\b", line, re.I)
            )
            cls.conn.execute(sql)

    @classmethod
    def tearDownClass(cls) -> None:
        if not hasattr(cls, "conn"):
            return
        for signature in (
            "public.inventory_get_context(text)",
            "public.inventory_create_draft(text,text,text,jsonb)",
            "public.inventory_send_order(uuid)",
            "public.inventory_record_decision(text,text,jsonb,uuid)",
        ):
            cls.conn.execute(f"DROP FUNCTION IF EXISTS {signature} CASCADE")
        cls.conn.execute("DROP SCHEMA IF EXISTS inventory CASCADE")
        cls.conn.execute("DROP SCHEMA IF EXISTS inventory_checkpoints CASCADE")
        cls.conn.close()

    def setUp(self) -> None:
        self.conn = type(self).conn
        self.conn.execute("SELECT inventory.reset_demo()")

    def test_context_rpc_returns_seeded_rows(self) -> None:
        row = self.conn.execute("SELECT public.inventory_get_context('anomaly')").fetchone()[0]
        self.assertEqual(row["state"]["scenario"], "anomaly")
        self.assertEqual(len(row["sales_history"]), 180)
        event = next(r for r in row["sales_history"] if r["date"] == "2026-02-14")
        self.assertIn("900 units", event["annotation"])

    def test_context_rpc_returns_empty_price_break_array_when_none_exist(self) -> None:
        row = self.conn.execute(
            "SELECT public.inventory_get_context('disruption')"
        ).fetchone()[0]
        west = next(group for group in row["suppliers"]
                    if group["supplier"]["supplier_id"] == "west")
        paper = next(item for item in west["items"] if item["sku"] == "PAPER-A4")
        self.assertEqual(paper["price_breaks"], [])

    def test_invalid_rpc_call_rolls_back_without_order_rows(self) -> None:
        from psycopg import Error

        with self.assertRaises(Error):
            with self.conn.transaction():
                self.conn.execute("""
                    SELECT public.inventory_create_draft(
                      'anomaly','desk','invalid-fractional',
                      '[{"sku":"PENS-BLUE","quantity":1.25}]'::jsonb)
                """)
        count = self.conn.execute("SELECT count(*) FROM inventory.purchase_orders").fetchone()[0]
        self.assertEqual(count, 0)

    def test_rpc_idempotency_and_changed_payload_rejection(self) -> None:
        from psycopg import Error

        call = "SELECT public.inventory_create_draft(%s,%s,%s,%s::jsonb)"
        params = ("anomaly", "desk", "anomaly:desk:test", '[{"sku":"PENS-BLUE","quantity":100}]')
        first = self.conn.execute(call, params).fetchone()[0]
        again = self.conn.execute(call, params).fetchone()[0]
        self.assertEqual(first, again)
        with self.assertRaises(Error):
            with self.conn.transaction():
                self.conn.execute(call, ("anomaly", "desk", "anomaly:desk:test",
                                         '[{"sku":"PENS-BLUE","quantity":101}]'))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM inventory.purchase_orders").fetchone()[0], 1)

    def test_injection_10000_and_split_700_orders_hit_cumulative_cap(self) -> None:
        from psycopg import Error

        call = "SELECT public.inventory_create_draft(%s,%s,%s,%s::jsonb)"
        with self.assertRaises(Error):
            with self.conn.transaction():
                self.conn.execute(call, ("injection", "north", "injection:north:10k",
                                         '[{"sku":"PAPER-A4","quantity":10000}]'))
        first = self.conn.execute(call, ("disruption", "west", "disruption:west:700-a",
                                         '[{"sku":"PAPER-A4","quantity":700}]')).fetchone()[0]
        self.assertIsNotNone(first)
        with self.assertRaises(Error):
            with self.conn.transaction():
                self.conn.execute(call, ("disruption", "west", "disruption:west:700-b",
                                         '[{"sku":"PAPER-A4","quantity":700}]'))
        total_units = self.conn.execute("""
            SELECT sum(l.quantity) FROM inventory.po_lines l
            JOIN inventory.purchase_orders o USING(po_id)
            WHERE o.scenario='disruption' AND l.sku='PAPER-A4'
        """).fetchone()[0]
        self.assertEqual(total_units, 700)

    def test_draft_send_and_receive_transitions_are_repeat_safe(self) -> None:
        call = "SELECT public.inventory_create_draft(%s,%s,%s,%s::jsonb)"
        params = ("disruption", "west", "disruption:west:lifecycle",
                  '[{"sku":"PAPER-A4","quantity":20}]')
        before = self.conn.execute("SELECT on_hand FROM inventory.stock WHERE sku='PAPER-A4'").fetchone()[0]
        po_id = self.conn.execute(call, params).fetchone()[0]
        self.assertEqual(self.conn.execute(call, params).fetchone()[0], po_id)
        self.assertEqual(self.conn.execute("SELECT on_hand FROM inventory.stock WHERE sku='PAPER-A4'").fetchone()[0], before)
        self.assertEqual(self.conn.execute("SELECT inventory.current_on_order('disruption','PAPER-A4')").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT public.inventory_send_order(%s)", (po_id,)).fetchone()[0], "sent")
        self.assertEqual(self.conn.execute("SELECT public.inventory_send_order(%s)", (po_id,)).fetchone()[0], "sent")
        self.assertEqual(self.conn.execute("SELECT inventory.current_on_order('disruption','PAPER-A4')").fetchone()[0], 20)
        self.conn.execute("SELECT inventory.advance_clock('disruption',3)")
        self.assertEqual(self.conn.execute("SELECT inventory.receive_due_orders('disruption')").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT inventory.receive_due_orders('disruption')").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT on_hand FROM inventory.stock WHERE sku='PAPER-A4'").fetchone()[0], before + 20)
        self.assertEqual(self.conn.execute("SELECT inventory.current_on_order('disruption','PAPER-A4')").fetchone()[0], 0)

    def test_checkpoint_thread_survives_connection_replacement_without_model(self) -> None:
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.graph import END, START, MessagesState, StateGraph
        from psycopg import connect
        from psycopg.rows import dict_row
        import uuid

        thread_id = f"integration-reconnect:{uuid.uuid4()}"
        config = {"configurable": {"thread_id": thread_id}}
        marker = f"stored:{uuid.uuid4()}"
        first = connect(self.uri, autocommit=True, prepare_threshold=0,
                        options="-c search_path=inventory_checkpoints,public", row_factory=dict_row)
        version = first.execute("SELECT max(v) FROM checkpoint_migrations").fetchone()["max"]
        self.assertEqual(version, 9)

        def node(state: MessagesState):
            return {"messages": [AIMessage(content=marker)]}

        builder = StateGraph(MessagesState)
        builder.add_node("save", node)
        builder.add_edge(START, "save")
        builder.add_edge("save", END)
        graph = builder.compile(checkpointer=PostgresSaver(first))
        graph.invoke({"messages": [HumanMessage(content="persist this") ]}, config=config)
        first.close()

        second = connect(self.uri, autocommit=True, prepare_threshold=0,
                         options="-c search_path=inventory_checkpoints,public", row_factory=dict_row)
        saver = PostgresSaver(second)
        restored = saver.get_tuple(config)
        messages = restored.checkpoint.get("channel_values", {}).get("messages", []) if restored else []
        self.assertTrue(any(getattr(message, "content", None) == marker for message in messages))
        saver.delete_thread(thread_id)
        second.close()

    def test_concurrent_rpc_drafts_cannot_exceed_shared_budget(self) -> None:
        from psycopg import connect, Error

        barrier = __import__("threading").Barrier(2)

        def submit(supplier: str, key: str, line: str) -> tuple[bool, str]:
            with connect(self.uri) as conn:
                barrier.wait(timeout=10)
                try:
                    po_id = conn.execute(
                        "SELECT public.inventory_create_draft(%s,%s,%s,%s::jsonb)",
                        ("budget", supplier, key, line),
                    ).fetchone()[0]
                    return True, str(po_id)
                except Error as exc:
                    conn.rollback()
                    return False, str(exc)

        jobs = [
            ("tech", "budget:tech:toner", '[{"sku":"TONER-BLK","quantity":100}]'),
            ("tech", "budget:tech:chairs", '[{"sku":"CHAIR-ERG","quantity":50}]'),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: submit(*args), jobs))
        self.assertEqual(sum(success for success, _ in results), 1)
        total = self.conn.execute("SELECT sum(total) FROM inventory.purchase_orders WHERE scenario='budget'").fetchone()[0]
        self.assertLessEqual(total, 20000)


if __name__ == "__main__":
    unittest.main()
