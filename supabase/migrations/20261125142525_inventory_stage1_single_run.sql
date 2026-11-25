-- All scenario views share one demo run UUID; explicit reset rotates it once.
WITH new_run AS MATERIALIZED (SELECT gen_random_uuid() AS run_id)
UPDATE inventory.demo_state d SET run_id=n.run_id FROM new_run n;
