-- migration: 0003_orders_notes_and_open_orders
-- generated: 2026-10-08T14:53:22 by dbmig/pgschema
-- manual changes captured from environment 'prod' (marked as captured there)

CREATE OR REPLACE VIEW v_open_orders AS
 SELECT id,
    customer_id,
    status,
    ordered_at
   FROM orders
  WHERE status = ANY (ARRAY['pending'::order_status, 'paid'::order_status]);

ALTER TABLE orders ADD COLUMN notes text;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_status ON orders (status);

-- pgschema:wait
SELECT 
    COALESCE(i.indisvalid, false) as done,
    CASE 
        WHEN p.blocks_total > 0 THEN p.blocks_done * 100 / p.blocks_total
        ELSE 0
    END as progress
FROM pg_class c
LEFT JOIN pg_index i ON c.oid = i.indexrelid
LEFT JOIN pg_stat_progress_create_index p ON c.oid = p.index_relid
WHERE c.relname = 'idx_orders_status';

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END;
$$;
