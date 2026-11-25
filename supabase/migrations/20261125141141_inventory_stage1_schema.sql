-- Stage 1 inventory exercise. Applied to the explicitly selected club-chatbot project.
CREATE SCHEMA inventory;
CREATE SCHEMA inventory_checkpoints;

REVOKE ALL ON SCHEMA inventory FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SCHEMA inventory_checkpoints FROM PUBLIC, anon, authenticated;
GRANT USAGE ON SCHEMA inventory TO service_role;
GRANT USAGE ON SCHEMA inventory_checkpoints TO service_role;

CREATE TABLE inventory.suppliers (
  supplier_id text PRIMARY KEY,
  name text NOT NULL UNIQUE,
  ordering_fee numeric(12,2) NOT NULL DEFAULT 0 CHECK (ordering_fee >= 0),
  shipping_fee numeric(12,2) NOT NULL DEFAULT 0 CHECK (shipping_fee >= 0),
  free_shipping_threshold numeric(12,2) NOT NULL DEFAULT 0 CHECK (free_shipping_threshold >= 0),
  minimum_order_value numeric(12,2) NOT NULL DEFAULT 0 CHECK (minimum_order_value >= 0),
  currency char(3) NOT NULL DEFAULT 'USD' CHECK (currency = 'USD')
);

CREATE TABLE inventory.products (
  sku text PRIMARY KEY,
  name text NOT NULL UNIQUE,
  category text NOT NULL,
  unit text NOT NULL DEFAULT 'unit',
  active_scenarios text[] NOT NULL,
  cumulative_cap integer NOT NULL CHECK (cumulative_cap > 0),
  safety_stock integer NOT NULL DEFAULT 0 CHECK (safety_stock >= 0),
  CHECK (cardinality(active_scenarios) > 0)
);

CREATE TABLE inventory.supplier_products (
  supplier_id text NOT NULL REFERENCES inventory.suppliers ON DELETE RESTRICT,
  sku text NOT NULL REFERENCES inventory.products ON DELETE RESTRICT,
  unit_price numeric(12,2) NOT NULL CHECK (unit_price >= 0),
  pack_size integer NOT NULL CHECK (pack_size > 0),
  moq integer NOT NULL CHECK (moq > 0),
  lead_days integer NOT NULL CHECK (lead_days >= 0),
  PRIMARY KEY (supplier_id, sku)
);

CREATE TABLE inventory.price_breaks (
  supplier_id text NOT NULL,
  sku text NOT NULL,
  min_units integer NOT NULL CHECK (min_units > 0),
  unit_price numeric(12,2) NOT NULL CHECK (unit_price >= 0),
  PRIMARY KEY (supplier_id, sku, min_units),
  FOREIGN KEY (supplier_id, sku) REFERENCES inventory.supplier_products ON DELETE CASCADE
);

CREATE TABLE inventory.stock (
  sku text PRIMARY KEY REFERENCES inventory.products ON DELETE RESTRICT,
  on_hand integer NOT NULL CHECK (on_hand >= 0),
  safety_stock integer NOT NULL CHECK (safety_stock >= 0),
  CHECK (safety_stock <= on_hand)
);

CREATE TABLE inventory.sales_history (
  sale_date date NOT NULL,
  sku text NOT NULL REFERENCES inventory.products ON DELETE RESTRICT,
  units_sold integer NOT NULL CHECK (units_sold >= 0),
  event_annotation text,
  PRIMARY KEY (sale_date, sku)
);

CREATE TABLE inventory.supplier_messages (
  message_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scenario text NOT NULL,
  supplier_id text NOT NULL REFERENCES inventory.suppliers ON DELETE RESTRICT,
  sku text REFERENCES inventory.products ON DELETE RESTRICT,
  message text NOT NULL,
  received_at date NOT NULL DEFAULT DATE '2026-03-01'
);

CREATE TABLE inventory.demo_state (
  scenario text PRIMARY KEY CHECK (scenario IN ('disruption','anomaly','consolidation','budget','injection')),
  run_id uuid NOT NULL DEFAULT gen_random_uuid(),
  simulated_date date NOT NULL DEFAULT DATE '2026-03-01',
  deadline date NOT NULL DEFAULT DATE '2026-03-15',
  currency char(3) NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  budget numeric(12,2) NOT NULL DEFAULT 20000 CHECK (budget >= 0),
  UNIQUE (scenario, run_id),
  CHECK (simulated_date <= deadline)
);

CREATE TABLE inventory.purchase_orders (
  po_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scenario text NOT NULL REFERENCES inventory.demo_state(scenario) ON DELETE CASCADE,
  run_id uuid NOT NULL,
  supplier_id text NOT NULL REFERENCES inventory.suppliers ON DELETE RESTRICT,
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  request_payload jsonb NOT NULL CHECK (jsonb_typeof(request_payload) = 'array'),
  payload_hash bytea NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','received')),
  merchandise_total numeric(12,2) NOT NULL CHECK (merchandise_total >= 0),
  ordering_fee numeric(12,2) NOT NULL CHECK (ordering_fee >= 0),
  shipping_fee numeric(12,2) NOT NULL CHECK (shipping_fee >= 0),
  total numeric(12,2) NOT NULL CHECK (total >= 0),
  created_at date NOT NULL,
  sent_at date,
  due_date date NOT NULL,
  received_at date,
  UNIQUE (scenario, run_id, idempotency_key),
  UNIQUE (po_id, supplier_id),
  FOREIGN KEY (scenario, run_id) REFERENCES inventory.demo_state(scenario, run_id) ON DELETE CASCADE,
  CHECK ((status = 'draft' AND sent_at IS NULL AND received_at IS NULL)
      OR (status = 'sent' AND sent_at IS NOT NULL AND received_at IS NULL)
      OR (status = 'received' AND sent_at IS NOT NULL AND received_at IS NOT NULL))
);

CREATE TABLE inventory.po_lines (
  po_id uuid NOT NULL,
  supplier_id text NOT NULL,
  sku text NOT NULL REFERENCES inventory.products ON DELETE RESTRICT,
  quantity integer NOT NULL CHECK (quantity > 0),
  unit_price numeric(12,2) NOT NULL CHECK (unit_price >= 0),
  line_total numeric(12,2) NOT NULL CHECK (line_total >= 0),
  PRIMARY KEY (po_id, sku),
  FOREIGN KEY (po_id, supplier_id) REFERENCES inventory.purchase_orders(po_id, supplier_id) ON DELETE CASCADE,
  FOREIGN KEY (supplier_id, sku) REFERENCES inventory.supplier_products(supplier_id, sku) ON DELETE RESTRICT
);

CREATE TABLE inventory.agent_decisions (
  decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scenario text NOT NULL,
  run_id uuid NOT NULL,
  decision text NOT NULL,
  evidence jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(evidence) = 'array'),
  po_id uuid REFERENCES inventory.purchase_orders ON DELETE SET NULL,
  created_at date NOT NULL,
  FOREIGN KEY (scenario, run_id) REFERENCES inventory.demo_state(scenario, run_id) ON DELETE CASCADE
);

CREATE INDEX po_scenario_status_idx ON inventory.purchase_orders(scenario, run_id, status);
CREATE INDEX po_lines_sku_idx ON inventory.po_lines(sku, po_id);
CREATE INDEX sales_history_sku_date_idx ON inventory.sales_history(sku, sale_date);
CREATE INDEX supplier_messages_scenario_idx ON inventory.supplier_messages(scenario, supplier_id);
CREATE INDEX agent_decisions_run_idx ON inventory.agent_decisions(scenario, run_id, created_at);

CREATE FUNCTION inventory.payload_digest(p_text text)
RETURNS bytea LANGUAGE sql IMMUTABLE STRICT SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$ SELECT decode(pg_catalog.md5(p_text) || pg_catalog.md5('inventory:' || p_text), 'hex') $$;

-- Freeze langgraph-checkpoint-postgres==3.2.1's PostgresSaver schema (migration 9).
CREATE TABLE inventory_checkpoints.checkpoint_migrations (v integer PRIMARY KEY);
CREATE TABLE inventory_checkpoints.checkpoints (
  thread_id text NOT NULL,
  checkpoint_ns text NOT NULL DEFAULT '',
  checkpoint_id text NOT NULL,
  parent_checkpoint_id text,
  type text,
  checkpoint jsonb NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}',
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE inventory_checkpoints.checkpoint_blobs (
  thread_id text NOT NULL,
  checkpoint_ns text NOT NULL DEFAULT '',
  channel text NOT NULL,
  version text NOT NULL,
  type text NOT NULL,
  blob bytea,
  PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE TABLE inventory_checkpoints.checkpoint_writes (
  thread_id text NOT NULL,
  checkpoint_ns text NOT NULL DEFAULT '',
  checkpoint_id text NOT NULL,
  task_id text NOT NULL,
  idx integer NOT NULL,
  channel text NOT NULL,
  type text,
  blob bytea NOT NULL,
  task_path text NOT NULL DEFAULT '',
  PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
CREATE INDEX checkpoints_thread_id_idx ON inventory_checkpoints.checkpoints(thread_id);
CREATE INDEX checkpoint_blobs_thread_id_idx ON inventory_checkpoints.checkpoint_blobs(thread_id);
CREATE INDEX checkpoint_writes_thread_id_idx ON inventory_checkpoints.checkpoint_writes(thread_id);
INSERT INTO inventory_checkpoints.checkpoint_migrations(v) VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

ALTER TABLE inventory.suppliers ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.products ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.supplier_products ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.price_breaks ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.stock ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.sales_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.supplier_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.demo_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.purchase_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.po_lines ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory.agent_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_checkpoints.checkpoint_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_checkpoints.checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_checkpoints.checkpoint_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE inventory_checkpoints.checkpoint_writes ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA inventory FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA inventory_checkpoints FROM PUBLIC, anon, authenticated;
GRANT SELECT ON ALL TABLES IN SCHEMA inventory TO service_role;
GRANT INSERT, UPDATE, DELETE ON inventory.purchase_orders, inventory.po_lines, inventory.agent_decisions TO service_role;
GRANT INSERT, UPDATE, DELETE ON inventory.stock, inventory.demo_state, inventory.sales_history, inventory.supplier_messages TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA inventory_checkpoints TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA inventory TO service_role;

CREATE FUNCTION inventory.current_on_order(p_scenario text, p_sku text)
RETURNS integer LANGUAGE sql STABLE SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
  SELECT COALESCE(sum(l.quantity),0)::integer
  FROM inventory.po_lines l JOIN inventory.purchase_orders o USING (po_id)
  WHERE o.scenario=p_scenario AND o.status='sent' AND l.sku=p_sku
$$;

CREATE FUNCTION inventory.reset_demo()
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE new_run uuid := gen_random_uuid();
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('inventory-stage1-reset'));
  DELETE FROM inventory.purchase_orders;
  DELETE FROM inventory.agent_decisions;
  UPDATE inventory.demo_state SET run_id=new_run, simulated_date=DATE '2026-03-01', budget=20000;
  UPDATE inventory.stock SET on_hand=CASE sku WHEN 'PAPER-A4' THEN 160 ELSE on_hand END,
    safety_stock=CASE sku WHEN 'PAPER-A4' THEN 40 ELSE safety_stock END;
  RETURN new_run;
END; $$;

CREATE FUNCTION inventory.advance_clock(p_scenario text, p_days integer)
RETURNS date LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE d date;
BEGIN
  IF p_days IS NULL OR p_days < 0 THEN RAISE EXCEPTION 'days must be a nonnegative integer'; END IF;
  UPDATE inventory.demo_state SET simulated_date=simulated_date+p_days
   WHERE scenario=p_scenario AND simulated_date+p_days <= deadline
   RETURNING simulated_date INTO d;
  IF d IS NULL THEN RAISE EXCEPTION 'unknown scenario or advancement exceeds deadline'; END IF;
  RETURN d;
END; $$;

CREATE FUNCTION inventory.receive_due_orders(p_scenario text)
RETURNS integer LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE d date; n integer;
BEGIN
  SELECT simulated_date INTO d FROM inventory.demo_state WHERE scenario=p_scenario FOR UPDATE;
  IF d IS NULL THEN RAISE EXCEPTION 'unknown scenario'; END IF;
  UPDATE inventory.purchase_orders SET status='received', received_at=d
   WHERE scenario=p_scenario AND status='sent' AND due_date <= d;
  GET DIAGNOSTICS n = ROW_COUNT;
  UPDATE inventory.stock s SET on_hand=s.on_hand+x.qty
  FROM (SELECT l.sku, sum(l.quantity)::integer qty FROM inventory.po_lines l
        JOIN inventory.purchase_orders o USING(po_id)
        WHERE o.scenario=p_scenario AND o.status='received' AND o.received_at=d GROUP BY l.sku) x
  WHERE s.sku=x.sku;
  RETURN n;
END; $$;

-- Read-only model-facing API returns only this demo's current scenario data.
CREATE FUNCTION public.inventory_get_context(p_scenario text)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE out_json jsonb;
BEGIN
  SELECT jsonb_build_object(
    'state', (SELECT to_jsonb(d) FROM inventory.demo_state d WHERE d.scenario=p_scenario),
    'products', (SELECT COALESCE(jsonb_agg(to_jsonb(p) ORDER BY p.sku),'[]') FROM inventory.products p WHERE p.active_scenarios @> ARRAY[p_scenario]),
    'stock', (SELECT COALESCE(jsonb_agg(jsonb_build_object('sku',s.sku,'on_hand',s.on_hand,'safety_stock',s.safety_stock,
      'on_order',inventory.current_on_order(p_scenario,s.sku)) ORDER BY s.sku),'[]') FROM inventory.stock s JOIN inventory.products p USING(sku) WHERE p.active_scenarios @> ARRAY[p_scenario]),
    'suppliers', (SELECT COALESCE(jsonb_agg(jsonb_build_object('supplier',to_jsonb(sp),'items',sp_items.items) ORDER BY sp.supplier_id),'[]')
      FROM inventory.suppliers sp JOIN LATERAL (SELECT jsonb_agg(jsonb_build_object('sku',x.sku,'unit_price',x.unit_price,'pack_size',x.pack_size,'moq',x.moq,'lead_days',x.lead_days,'price_breaks',pb.breaks) ORDER BY x.sku) items
        FROM inventory.supplier_products x JOIN inventory.products p USING(sku)
        LEFT JOIN LATERAL (SELECT jsonb_agg(to_jsonb(b) ORDER BY b.min_units) breaks FROM inventory.price_breaks b WHERE b.supplier_id=x.supplier_id AND b.sku=x.sku) pb ON true
        WHERE x.supplier_id=sp.supplier_id AND p.active_scenarios @> ARRAY[p_scenario]) sp_items ON true),
    'messages', (SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY m.message_id),'[]') FROM inventory.supplier_messages m WHERE m.scenario=p_scenario),
    'open_orders', (SELECT COALESCE(jsonb_agg(to_jsonb(o) ORDER BY o.created_at),'[]') FROM inventory.purchase_orders o WHERE o.scenario=p_scenario)
  ) INTO out_json;
  IF out_json->'state' = 'null'::jsonb THEN RAISE EXCEPTION 'unknown scenario'; END IF;
  RETURN out_json;
END; $$;

CREATE FUNCTION public.inventory_create_draft(p_scenario text, p_supplier text, p_idempotency_key text, p_lines jsonb)
RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE d inventory.demo_state%ROWTYPE; sup inventory.suppliers%ROWTYPE; po uuid; payload_hash bytea;
        merchandise numeric(12,2); fee numeric(12,2); ship numeric(12,2); total_cost numeric(12,2); max_lead integer; override_days integer;
BEGIN
  IF jsonb_typeof(p_lines) <> 'array' OR jsonb_array_length(p_lines)=0 THEN RAISE EXCEPTION 'lines must be a nonempty array'; END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_lines) e WHERE jsonb_typeof(e) <> 'object' OR (e->>'sku') IS NULL OR (e->>'quantity') IS NULL OR (e->>'quantity') !~ '^[0-9]+$') THEN RAISE EXCEPTION 'each line needs an SKU and positive integer quantity'; END IF;
  IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_lines) e GROUP BY e->>'sku' HAVING count(*)>1) THEN RAISE EXCEPTION 'duplicate SKU lines are not allowed'; END IF;
  payload_hash := inventory.payload_digest(jsonb_build_object('supplier',p_supplier,'lines',p_lines)::text);
  PERFORM pg_advisory_xact_lock(hashtext('inventory-stage1:'||p_scenario));
  SELECT * INTO d FROM inventory.demo_state WHERE scenario=p_scenario FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown scenario'; END IF;
  SELECT * INTO sup FROM inventory.suppliers WHERE supplier_id=p_supplier;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown supplier'; END IF;
  SELECT o.po_id,o.payload_hash INTO po,payload_hash FROM inventory.purchase_orders o
    WHERE scenario=p_scenario AND run_id=d.run_id AND idempotency_key=p_idempotency_key;
  IF po IS NOT NULL THEN
    IF payload_hash <> inventory.payload_digest(jsonb_build_object('supplier',p_supplier,'lines',p_lines)::text) THEN RAISE EXCEPTION 'idempotency key reused with a changed payload'; END IF;
    RETURN po;
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(p_lines) e
    LEFT JOIN inventory.supplier_products sp ON sp.supplier_id=p_supplier AND sp.sku=e->>'sku'
    LEFT JOIN inventory.products p ON p.sku=e->>'sku'
    WHERE sp.sku IS NULL OR p.sku IS NULL OR NOT p.active_scenarios @> ARRAY[p_scenario]
      OR (e->>'quantity')::numeric > 2147483647
      OR (e->>'quantity')::integer < sp.moq OR (e->>'quantity')::integer % sp.pack_size <> 0
  ) THEN RAISE EXCEPTION 'SKU unavailable, scenario inactive, or quantity violates pack/MOQ'; END IF;
  SELECT COALESCE(sum((e->>'quantity')::integer * COALESCE(b.unit_price,sp.unit_price)),0),
         COALESCE(sum(sp.lead_days),0)
    INTO merchandise,max_lead
    FROM jsonb_array_elements(p_lines) e
    JOIN inventory.supplier_products sp ON sp.supplier_id=p_supplier AND sp.sku=e->>'sku'
    LEFT JOIN LATERAL (SELECT pb.unit_price FROM inventory.price_breaks pb WHERE pb.supplier_id=sp.supplier_id AND pb.sku=sp.sku AND pb.min_units <= (e->>'quantity')::integer ORDER BY pb.min_units DESC LIMIT 1) b ON true;
  IF merchandise < sup.minimum_order_value THEN RAISE EXCEPTION 'supplier minimum order value not met'; END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(p_lines) e JOIN inventory.products p ON p.sku=e->>'sku'
    WHERE COALESCE((SELECT sum(l.quantity) FROM inventory.po_lines l JOIN inventory.purchase_orders o USING(po_id)
      WHERE o.scenario=p_scenario AND o.run_id=d.run_id AND o.status IN ('draft','sent','received') AND l.sku=p.sku),0) + (e->>'quantity')::integer > p.cumulative_cap
  ) THEN RAISE EXCEPTION 'cumulative SKU cap exceeded'; END IF;
  fee := sup.ordering_fee;
  ship := CASE WHEN merchandise < sup.free_shipping_threshold THEN sup.shipping_fee ELSE 0 END;
  total_cost := merchandise + fee + ship;
  IF COALESCE((SELECT sum(o.total) FROM inventory.purchase_orders o WHERE o.scenario=p_scenario AND o.run_id=d.run_id),0) + total_cost > d.budget THEN RAISE EXCEPTION 'shared scenario budget exceeded'; END IF;
  SELECT CASE WHEN p_scenario IN ('disruption','injection') AND p_supplier='north' AND bool_or(e->>'sku'='PAPER-A4') THEN 21 ELSE COALESCE(max(sp.lead_days),0) END
    INTO override_days FROM jsonb_array_elements(p_lines) e JOIN inventory.supplier_products sp ON sp.supplier_id=p_supplier AND sp.sku=e->>'sku';
  INSERT INTO inventory.purchase_orders(scenario,run_id,supplier_id,idempotency_key,request_payload,payload_hash,merchandise_total,ordering_fee,shipping_fee,total,created_at,due_date)
    VALUES (p_scenario,d.run_id,p_supplier,p_idempotency_key,p_lines,inventory.payload_digest(jsonb_build_object('supplier',p_supplier,'lines',p_lines)::text),merchandise,fee,ship,total_cost,d.simulated_date,d.simulated_date+override_days)
    RETURNING po_id INTO po;
  INSERT INTO inventory.po_lines(po_id,supplier_id,sku,quantity,unit_price,line_total)
    SELECT po,p_supplier,e->>'sku',(e->>'quantity')::integer,COALESCE(b.unit_price,sp.unit_price),(e->>'quantity')::integer*COALESCE(b.unit_price,sp.unit_price)
    FROM jsonb_array_elements(p_lines) e JOIN inventory.supplier_products sp ON sp.supplier_id=p_supplier AND sp.sku=e->>'sku'
    LEFT JOIN LATERAL (SELECT pb.unit_price FROM inventory.price_breaks pb WHERE pb.supplier_id=sp.supplier_id AND pb.sku=sp.sku AND pb.min_units <= (e->>'quantity')::integer ORDER BY pb.min_units DESC LIMIT 1) b ON true;
  RETURN po;
END; $$;

CREATE FUNCTION public.inventory_send_order(p_po_id uuid)
RETURNS text LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE state_row inventory.demo_state%ROWTYPE; result text;
BEGIN
  SELECT d.* INTO state_row FROM inventory.demo_state d JOIN inventory.purchase_orders o ON o.scenario=d.scenario WHERE o.po_id=p_po_id FOR UPDATE OF d;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown order'; END IF;
  UPDATE inventory.purchase_orders SET status='sent',sent_at=state_row.simulated_date WHERE po_id=p_po_id AND status='draft';
  IF FOUND THEN RETURN 'sent'; END IF;
  SELECT status INTO result FROM inventory.purchase_orders WHERE po_id=p_po_id;
  RETURN result;
END; $$;

CREATE FUNCTION public.inventory_record_decision(p_scenario text,p_decision text,p_evidence jsonb,p_po_id uuid DEFAULT NULL)
RETURNS bigint LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, inventory
AS $$
DECLARE d inventory.demo_state%ROWTYPE; result bigint;
BEGIN
  SELECT * INTO d FROM inventory.demo_state WHERE scenario=p_scenario;
  IF NOT FOUND THEN RAISE EXCEPTION 'unknown scenario'; END IF;
  INSERT INTO inventory.agent_decisions(scenario,run_id,decision,evidence,po_id,created_at)
  VALUES(p_scenario,d.run_id,p_decision,COALESCE(p_evidence,'[]'),p_po_id,d.simulated_date) RETURNING decision_id INTO result;
  RETURN result;
END; $$;

REVOKE ALL ON FUNCTION public.inventory_get_context(text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inventory_create_draft(text,text,text,jsonb) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inventory_send_order(uuid) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.inventory_record_decision(text,text,jsonb,uuid) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.inventory_get_context(text), public.inventory_create_draft(text,text,text,jsonb), public.inventory_send_order(uuid), public.inventory_record_decision(text,text,jsonb,uuid) TO service_role;
REVOKE ALL ON FUNCTION inventory.reset_demo() FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION inventory.advance_clock(text,integer) FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON FUNCTION inventory.receive_due_orders(text) FROM PUBLIC, anon, authenticated, service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA inventory REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA inventory_checkpoints REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
