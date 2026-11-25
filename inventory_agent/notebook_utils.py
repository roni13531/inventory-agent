from __future__ import annotations

import os
from typing import Any


SCENARIOS = {"disruption", "anomaly", "consolidation", "budget", "injection"}


def _db_uri() -> str:
    uri = os.environ.get("SUPABASE_DB_URI")
    if uri:
        return uri
    try:
        from google.colab import userdata  # type: ignore[import-not-found]
        uri = userdata.get("SUPABASE_DB_URI")
    except Exception:
        uri = None
    if not uri:
        raise RuntimeError("Set SUPABASE_DB_URI in the environment or Colab Secrets for notebook-only controls.")
    return str(uri)


def _connect() -> Any:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("Install the pinned psycopg[binary] package for notebook-only controls.") from exc
    return psycopg.connect(_db_uri())


def reset_scenario(scenario: str) -> str:
    """Explicitly reset this exercise's fixtures; stage 1 resets all scenarios and rotates run_id."""
    if scenario not in SCENARIOS:
        raise ValueError("unknown inventory exercise scenario")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT inventory.reset_demo()")
        row = cur.fetchone()
        return str(row[0])


def advance_clock(scenario: str, days: int) -> str:
    """Notebook-only explicit clock advancement; not included in model tools."""
    if scenario not in SCENARIOS:
        raise ValueError("unknown inventory exercise scenario")
    if type(days) is not int or days < 0:
        raise ValueError("days must be a nonnegative integer")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT inventory.advance_clock(%s,%s)", (scenario, days))
        row = cur.fetchone()
        return row[0].isoformat()


def receive_due(scenario: str) -> int:
    """Notebook-only idempotent receipt after the user explicitly advances the clock."""
    if scenario not in SCENARIOS:
        raise ValueError("unknown inventory exercise scenario")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT inventory.receive_due_orders(%s)", (scenario,))
        row = cur.fetchone()
        return int(row[0])
