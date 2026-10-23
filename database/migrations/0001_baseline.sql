-- migration: 0001_baseline
-- generated: 2026-10-08T14:52:17 by dbmig/pgschema
-- baseline taken from environment 'dev'

CREATE TYPE order_status AS ENUM (
    'pending',
    'paid',
    'shipped',
    'delivered',
    'cancelled'
);

CREATE TABLE IF NOT EXISTS categories (
    id integer GENERATED ALWAYS AS IDENTITY,
    parent_id integer,
    name text NOT NULL,
    slug text NOT NULL,
    CONSTRAINT categories_pkey PRIMARY KEY (id),
    CONSTRAINT categories_slug_key UNIQUE (slug),
    CONSTRAINT categories_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES categories (id)
);

CREATE TABLE IF NOT EXISTS customers (
    id bigint GENERATED ALWAYS AS IDENTITY,
    email text NOT NULL,
    full_name text NOT NULL,
    phone text,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT customers_pkey PRIMARY KEY (id),
    CONSTRAINT customers_email_key UNIQUE (email)
);

CREATE TABLE IF NOT EXISTS addresses (
    id bigint GENERATED ALWAYS AS IDENTITY,
    customer_id bigint NOT NULL,
    line1 text NOT NULL,
    city text NOT NULL,
    postal_code text NOT NULL,
    country character(2) DEFAULT 'RS' NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    CONSTRAINT addresses_pkey PRIMARY KEY (id),
    CONSTRAINT addresses_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_addresses_customer ON addresses (customer_id);

CREATE TABLE IF NOT EXISTS orders (
    id bigint GENERATED ALWAYS AS IDENTITY,
    customer_id bigint NOT NULL,
    shipping_address_id bigint,
    status order_status DEFAULT 'pending'::order_status NOT NULL,
    total numeric(12,2) DEFAULT 0 NOT NULL,
    ordered_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT orders_pkey PRIMARY KEY (id),
    CONSTRAINT orders_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers (id),
    CONSTRAINT orders_shipping_address_id_fkey FOREIGN KEY (shipping_address_id) REFERENCES addresses (id)
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders (customer_id);

CREATE INDEX IF NOT EXISTS idx_orders_ordered_at ON orders (ordered_at);

CREATE TABLE IF NOT EXISTS order_status_history (
    id bigint GENERATED ALWAYS AS IDENTITY,
    order_id bigint NOT NULL,
    old_status order_status,
    new_status order_status NOT NULL,
    changed_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT order_status_history_pkey PRIMARY KEY (id),
    CONSTRAINT order_status_history_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS payments (
    id bigint GENERATED ALWAYS AS IDENTITY,
    order_id bigint NOT NULL,
    amount numeric(12,2) NOT NULL,
    method text NOT NULL,
    paid_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT payments_pkey PRIMARY KEY (id),
    CONSTRAINT payments_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id),
    CONSTRAINT payments_method_check CHECK (method IN ('card'::text, 'paypal'::text, 'cash'::text))
);

CREATE INDEX IF NOT EXISTS idx_payments_order ON payments (order_id);

CREATE TABLE IF NOT EXISTS products (
    id bigint GENERATED ALWAYS AS IDENTITY,
    category_id integer NOT NULL,
    sku text NOT NULL,
    name text NOT NULL,
    price numeric(12,2) NOT NULL,
    active boolean DEFAULT true NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT products_pkey PRIMARY KEY (id),
    CONSTRAINT products_sku_key UNIQUE (sku),
    CONSTRAINT products_category_id_fkey FOREIGN KEY (category_id) REFERENCES categories (id),
    CONSTRAINT products_price_check CHECK (price >= 0::numeric)
);

CREATE INDEX IF NOT EXISTS idx_products_category ON products (category_id);

CREATE TABLE IF NOT EXISTS order_items (
    order_id bigint,
    product_id bigint,
    quantity integer NOT NULL,
    unit_price numeric(12,2) NOT NULL,
    CONSTRAINT order_items_pkey PRIMARY KEY (order_id, product_id),
    CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id) ON DELETE CASCADE,
    CONSTRAINT order_items_product_id_fkey FOREIGN KEY (product_id) REFERENCES products (id),
    CONSTRAINT order_items_quantity_check CHECK (quantity > 0)
);

CREATE TABLE IF NOT EXISTS reviews (
    id bigint GENERATED ALWAYS AS IDENTITY,
    product_id bigint NOT NULL,
    customer_id bigint NOT NULL,
    rating smallint NOT NULL,
    body text,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT reviews_pkey PRIMARY KEY (id),
    CONSTRAINT reviews_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES customers (id),
    CONSTRAINT reviews_product_id_fkey FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE,
    CONSTRAINT reviews_rating_check CHECK (rating >= 1 AND rating <= 5)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id integer GENERATED ALWAYS AS IDENTITY,
    name text NOT NULL,
    email text,
    country character(2) NOT NULL,
    CONSTRAINT suppliers_pkey PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS product_suppliers (
    product_id bigint,
    supplier_id integer,
    cost numeric(12,2) NOT NULL,
    CONSTRAINT product_suppliers_pkey PRIMARY KEY (product_id, supplier_id),
    CONSTRAINT product_suppliers_product_id_fkey FOREIGN KEY (product_id) REFERENCES products (id) ON DELETE CASCADE,
    CONSTRAINT product_suppliers_supplier_id_fkey FOREIGN KEY (supplier_id) REFERENCES suppliers (id)
);

CREATE TABLE IF NOT EXISTS warehouses (
    id integer GENERATED ALWAYS AS IDENTITY,
    code text NOT NULL,
    city text NOT NULL,
    CONSTRAINT warehouses_pkey PRIMARY KEY (id),
    CONSTRAINT warehouses_code_key UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS inventory (
    warehouse_id integer,
    product_id bigint,
    quantity integer DEFAULT 0 NOT NULL,
    CONSTRAINT inventory_pkey PRIMARY KEY (warehouse_id, product_id),
    CONSTRAINT inventory_product_id_fkey FOREIGN KEY (product_id) REFERENCES products (id),
    CONSTRAINT inventory_warehouse_id_fkey FOREIGN KEY (warehouse_id) REFERENCES warehouses (id),
    CONSTRAINT inventory_quantity_check CHECK (quantity >= 0)
);

CREATE TABLE IF NOT EXISTS shipments (
    id bigint GENERATED ALWAYS AS IDENTITY,
    order_id bigint NOT NULL,
    warehouse_id integer NOT NULL,
    tracking_no text,
    shipped_at timestamptz,
    delivered_at timestamptz,
    CONSTRAINT shipments_pkey PRIMARY KEY (id),
    CONSTRAINT shipments_order_id_fkey FOREIGN KEY (order_id) REFERENCES orders (id),
    CONSTRAINT shipments_warehouse_id_fkey FOREIGN KEY (warehouse_id) REFERENCES warehouses (id)
);

CREATE OR REPLACE FUNCTION log_order_status()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO order_status_history (order_id, old_status, new_status)
        VALUES (NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status);
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON customers
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_orders_status_history
    AFTER INSERT OR UPDATE OF status ON orders
    FOR EACH ROW
    EXECUTE FUNCTION log_order_status();

CREATE OR REPLACE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

CREATE OR REPLACE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_customer_ltv AS
 SELECT c.id AS customer_id,
    c.email,
    count(o.id) AS order_count,
    COALESCE(sum(o.total), 0::numeric) AS lifetime_value,
    max(o.ordered_at) AS last_order_at
   FROM customers c
     LEFT JOIN orders o ON o.customer_id = c.id AND o.status <> 'cancelled'::order_status
  GROUP BY c.id, c.email;

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_sales AS
 SELECT date_trunc('day'::text, o.ordered_at)::date AS day,
    count(DISTINCT o.id) AS orders,
    sum(oi.quantity::numeric * oi.unit_price) AS revenue
   FROM orders o
     JOIN order_items oi ON oi.order_id = o.id
  WHERE o.status <> 'cancelled'::order_status
  GROUP BY (date_trunc('day'::text, o.ordered_at)::date);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_daily_sales_day ON mv_daily_sales (day);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_product_ratings AS
 SELECT p.id AS product_id,
    p.name,
    count(r.id) AS review_count,
    round(avg(r.rating), 2) AS avg_rating
   FROM products p
     LEFT JOIN reviews r ON r.product_id = p.id
  GROUP BY p.id, p.name;

CREATE OR REPLACE VIEW v_order_summary AS
 SELECT o.id AS order_id,
    c.email,
    o.status,
    o.ordered_at,
    count(oi.product_id) AS item_count,
    COALESCE(sum(oi.quantity::numeric * oi.unit_price), 0::numeric) AS items_total
   FROM orders o
     JOIN customers c ON c.id = o.customer_id
     LEFT JOIN order_items oi ON oi.order_id = o.id
  GROUP BY o.id, c.email;

CREATE OR REPLACE VIEW v_product_stock AS
 SELECT p.id AS product_id,
    p.sku,
    p.name,
    COALESCE(sum(i.quantity), 0::bigint) AS total_quantity
   FROM products p
     LEFT JOIN inventory i ON i.product_id = p.id
  GROUP BY p.id;
