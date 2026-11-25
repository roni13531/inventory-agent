CREATE INDEX agent_decisions_po_id_idx ON inventory.agent_decisions(po_id);
CREATE INDEX po_lines_order_supplier_idx ON inventory.po_lines(po_id,supplier_id);
CREATE INDEX po_lines_supplier_sku_idx ON inventory.po_lines(supplier_id,sku);
CREATE INDEX purchase_orders_supplier_id_idx ON inventory.purchase_orders(supplier_id);
CREATE INDEX supplier_messages_supplier_id_idx ON inventory.supplier_messages(supplier_id);
CREATE INDEX supplier_messages_sku_idx ON inventory.supplier_messages(sku);
CREATE INDEX supplier_products_sku_idx ON inventory.supplier_products(sku);

-- Require useful bulk multiples for folder cartons while preserving positive integer units.
UPDATE inventory.supplier_products SET pack_size=5,moq=10 WHERE supplier_id='west' AND sku='FOLDER-MAN';
