# Inventory agent exercise

This repository contains deterministic stage 2 tools and a stage 4 LangGraph notebook for the stage 1 Supabase inventory database. Orders and supplier messages are simulated; the agent never contacts a real supplier.

## Runtime

Use Python 3.12 and install the direct pins in `requirements.txt`; `requirements.lock` freezes the full verified environment. The local stage 4 verification ran in the existing Python 3.11.4 virtual environment. The selected OpenAI model is supplied only through `OPENAI_MODEL`.

```bash
python3.12 -m pip install -r requirements.txt
```

Open [`notebooks/inventory_agent_stage4.ipynb`](notebooks/inventory_agent_stage4.ipynb) in Google Colab. It installs the complete locked package set and reads `OPENAI_API_KEY`, `OPENAI_MODEL`, `SUPABASE_URL`, `SUPABASE_KEY`, and `SUPABASE_DB_URI` from Colab Secrets. The notebook contains the complete application source in readable cells and does not import local repository modules or ask for project file uploads. Rebuild it after changing source modules with:

```bash
python scripts/build_notebook.py
```

The notebook explicitly validates model access and structured tool calling before a scenario; this sends a small model request. PostgreSQL checkpointing uses the existing session-pooler URI on port 5432 with SSL and the `inventory_checkpoints` search path. The notebook checks the pre-applied checkpoint migration version (9), never runs a runtime migration, keeps the connection open for the graph session, and offers a no-model reconnect smoke check. Scenario reset, clock advancement, due receipt, outcome checks, and closing connections are separate notebook-only controls, not model tools. Use a fresh reset for a new scenario; after a runtime restart, restore conversation state with the exact previously printed thread ID.

LangGraph may execute tool calls concurrently. `InventoryService` serializes use of its shared synchronous Supabase client with a reentrant lock. Transient `httpx` network/timeout errors get at most three attempts on read RPCs, with 0.1 and 0.2 second exponential delays. Write RPCs are single-attempt; an error keeps the outcome-uncertain guidance to inspect orders and retry only with the stable idempotency key.

For Colab troubleshooting, `SUPABASE_DB_URI` must use the session-pooler on port `5432` and include SSL mode. If the notebook says it must require SSL, add `?sslmode=require` to the URI in Colab Secrets (or `&sslmode=require` when query parameters already exist); do not print the URI. Use one active notebook per Supabase project at a time because resets affect the shared demo fixtures.

Stage 4 scenarios, prompts, observable acceptance checks, and discussion questions are embedded in the notebook. Local cap-guard tests cover the malicious 10,000-unit request and two 700-unit orders against the 1,200-unit cap. The PostgreSQL integration suite additionally checks order state transitions and checkpoint recovery across connection replacement. Live Colab scenario outcomes are recorded in [`docs/stage4-run-log.md`](docs/stage4-run-log.md); unexecuted scenarios remain explicitly marked outstanding.

Set `SUPABASE_URL` and `SUPABASE_KEY` in the environment or Colab Secrets. The client helper reads them without printing or logging their values:

```python
from inventory_agent import InventoryService
from inventory_agent.client import create_supabase_client
from inventory_agent.tools import build_model_tools

service = InventoryService(create_supabase_client())
model_tools = build_model_tools(service)  # exactly 13 StructuredTools; no LLM is constructed
```

The notebook-only `reset_scenario`, `advance_clock`, and `receive_due` utilities require an explicitly configured `SUPABASE_DB_URI`, because stage 1 deliberately denies those control functions to the service role. `reset_scenario` validates the selected scenario, then invokes the stage 1 reset, which restores this exercise's fixtures across all scenarios and rotates the shared run UUID. Advance the clock explicitly before calling `receive_due`.

## Deterministic calculations

Demand uses the 90 calendar dates ending on the simulated date, keeping missing dates in the denominator. The stage 1 sales table's primary key is `(sale_date, sku)`, so tools expose the stable synthetic ID `SKU:YYYY-MM-DD`. Only annotated rows within the requested SKU's active 90-day window can be excluded. Excluding a daily aggregate removes that day's ordinary sales too; the denominator remains 90.

EOQ uses annual demand (`daily average * 365`), supplier order/shipping fees as `S`, and `H = unit price * 25% annual holding rate`. The service compares the rounded EOQ with feasible MOQ, supplier minimum, all-unit price break, and free-shipping quantities. The planning function uses an urgency-first heuristic, combines lines by supplier, and reduces less urgent quantities in whole packs to fit the database's budget; it does not claim a global optimum.

## Tests

Run unit tests with:

```bash
python3 -m unittest discover -s tests -v
```

The suite includes a scripted-model test of the fully inlined notebook graph. It exercises tool routing without local application imports, credentials, PostgreSQL, or model API calls. The live model validation and the checkpoint reconnection cell still need to be run in Colab with the selected secrets configured.

PostgreSQL integration tests are skipped unless `TEST_DATABASE_URL` is explicitly set. They refuse non-local hosts and refuse databases with pre-existing inventory schemas or public tables. Use a disposable empty local PostgreSQL database; the tests bootstrap the saved stage 1 migrations, exercise rollback, idempotency, and concurrent budget locking, then remove only the exercise schemas and RPCs they created.

## Submission

- **Database migrations:** [`supabase/migrations/`](supabase/migrations/)
- **Python tools and tests:** [`inventory_agent/`](inventory_agent/) and [`tests/`](tests/)
- **Cleared Colab notebook:** [`notebooks/inventory_agent_stage4.ipynb`](notebooks/inventory_agent_stage4.ipynb)
- **Observed scenario account:** [`docs/stage4-run-log.md`](docs/stage4-run-log.md)

The saved live-run evidence, including Colab outputs, is [`docs/stage4_live_run_outputs.ipynb`](docs/stage4_live_run_outputs.ipynb). The submission notebook is the cleared version; do not use the outputs notebook as the runnable submission.
