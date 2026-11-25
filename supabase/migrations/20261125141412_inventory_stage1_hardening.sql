CREATE OR REPLACE FUNCTION inventory.payload_digest(p_text text)
RETURNS bytea LANGUAGE sql IMMUTABLE STRICT SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$ SELECT pg_catalog.sha256(pg_catalog.convert_to(p_text,'UTF8')) $$;

CREATE OR REPLACE FUNCTION inventory.reset_demo()
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE new_run uuid := gen_random_uuid();
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('inventory-stage1-reset'));
  DELETE FROM inventory.purchase_orders;
  DELETE FROM inventory.agent_decisions;
  UPDATE inventory.demo_state SET run_id=new_run, simulated_date=DATE '2026-03-01', budget=20000;
  UPDATE inventory.stock s SET on_hand=v.on_hand,safety_stock=v.safety_stock
  FROM (VALUES
    ('PAPER-A4',160,40),('PENS-BLUE',90,30),('NOTEBOOK-A5',65,20),('MARKER-BLK',55,20),
    ('TONER-BLK',18,5),('CHAIR-ERG',8,2),('FOLDER-MAN',75,15),('BINDER-1IN',48,10),
    ('STAPLER-M',22,5),('ENVELOPE-L',70,10),('STICKY-Y',120,20),('CLIP-PAPER',140,20)
  ) AS v(sku,on_hand,safety_stock) WHERE s.sku=v.sku;
  RETURN new_run;
END; $$;

CREATE OR REPLACE FUNCTION inventory.receive_due_orders(p_scenario text)
RETURNS integer LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE d date; n integer; changed_ids uuid[];
BEGIN
  SELECT simulated_date INTO d FROM inventory.demo_state WHERE scenario=p_scenario FOR UPDATE;
  IF d IS NULL THEN RAISE EXCEPTION 'unknown scenario'; END IF;
  WITH changed AS (
    UPDATE inventory.purchase_orders SET status='received', received_at=d
    WHERE scenario=p_scenario AND status='sent' AND due_date <= d RETURNING po_id
  ) SELECT COALESCE(array_agg(po_id),'{}'::uuid[]),count(*)::integer INTO changed_ids,n FROM changed;
  UPDATE inventory.stock s SET on_hand=s.on_hand+x.qty
  FROM (SELECT l.sku,sum(l.quantity)::integer qty FROM inventory.po_lines l
        WHERE l.po_id=ANY(changed_ids) GROUP BY l.sku) x
  WHERE s.sku=x.sku;
  RETURN n;
END; $$;

REVOKE ALL ON FUNCTION inventory.current_on_order(text,text), inventory.payload_digest(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION inventory.current_on_order(text,text), inventory.payload_digest(text) TO service_role;
