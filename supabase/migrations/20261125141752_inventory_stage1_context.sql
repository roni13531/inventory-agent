CREATE OR REPLACE FUNCTION public.inventory_get_context(p_scenario text)
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
    'sales_history', (SELECT COALESCE(jsonb_agg(jsonb_build_object('date',h.sale_date,'sku',h.sku,'units_sold',h.units_sold,'annotation',h.event_annotation) ORDER BY h.sale_date,h.sku),'[]')
      FROM inventory.sales_history h JOIN inventory.products p USING(sku) WHERE p.active_scenarios @> ARRAY[p_scenario]),
    'suppliers', (SELECT COALESCE(jsonb_agg(jsonb_build_object('supplier',to_jsonb(sp),'items',sp_items.items) ORDER BY sp.supplier_id),'[]')
      FROM inventory.suppliers sp JOIN LATERAL (SELECT jsonb_agg(jsonb_build_object('sku',x.sku,'unit_price',x.unit_price,'pack_size',x.pack_size,'moq',x.moq,'lead_days',x.lead_days,'price_breaks',pb.breaks) ORDER BY x.sku) items
        FROM inventory.supplier_products x JOIN inventory.products p USING(sku)
        LEFT JOIN LATERAL (SELECT jsonb_agg(to_jsonb(b) ORDER BY b.min_units) breaks FROM inventory.price_breaks b WHERE b.supplier_id=x.supplier_id AND b.sku=x.sku) pb ON true
        WHERE x.supplier_id=sp.supplier_id AND p.active_scenarios @> ARRAY[p_scenario]) sp_items ON true),
    'messages', (SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY m.message_id),'[]') FROM inventory.supplier_messages m WHERE m.scenario=p_scenario),
    'open_orders', (SELECT COALESCE(jsonb_agg(to_jsonb(o)||jsonb_build_object('lines',COALESCE((SELECT jsonb_agg(to_jsonb(l) ORDER BY l.sku) FROM inventory.po_lines l WHERE l.po_id=o.po_id),'[]')) ORDER BY o.created_at),'[]') FROM inventory.purchase_orders o WHERE o.scenario=p_scenario)
  ) INTO out_json;
  IF out_json->'state' = 'null'::jsonb THEN RAISE EXCEPTION 'unknown scenario'; END IF;
  RETURN out_json;
END; $$;
