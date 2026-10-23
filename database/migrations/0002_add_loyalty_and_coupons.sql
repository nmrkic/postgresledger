-- migration: 0002_add_loyalty_and_coupons
-- generated: 2026-10-08T14:52:53 by dbmig/pgschema
-- change in schema.sql

DROP MATERIALIZED VIEW mv_customer_ltv RESTRICT;

CREATE TABLE IF NOT EXISTS coupons (
    id integer GENERATED ALWAYS AS IDENTITY,
    code text NOT NULL,
    discount_pct smallint NOT NULL,
    valid_until date,
    CONSTRAINT coupons_pkey PRIMARY KEY (id),
    CONSTRAINT coupons_code_key UNIQUE (code),
    CONSTRAINT coupons_discount_pct_check CHECK (discount_pct >= 1 AND discount_pct <= 100)
);

ALTER TABLE customers ADD COLUMN loyalty_points integer DEFAULT 0 NOT NULL;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_customer_ltv AS
 SELECT c.id AS customer_id,
    c.email,
    c.loyalty_points,
    count(o.id) AS order_count,
    COALESCE(sum(o.total), 0::numeric) AS lifetime_value,
    max(o.ordered_at) AS last_order_at
   FROM customers c
     LEFT JOIN orders o ON o.customer_id = c.id AND o.status <> 'cancelled'::order_status
  GROUP BY c.id, c.email, c.loyalty_points;
