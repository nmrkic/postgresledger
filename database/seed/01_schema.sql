-- Demo "existing" database: a web shop with 14 tables, views, materialized views,
-- functions and triggers. Loaded only into the `dev` database (make seed).

CREATE TYPE order_status AS ENUM ('pending', 'paid', 'shipped', 'delivered', 'cancelled');

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TABLE customers (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    phone       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE addresses (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    line1        text NOT NULL,
    city         text NOT NULL,
    postal_code  text NOT NULL,
    country      char(2) NOT NULL DEFAULT 'RS',
    is_default   boolean NOT NULL DEFAULT false
);
CREATE INDEX idx_addresses_customer ON addresses (customer_id);

CREATE TABLE categories (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_id  integer REFERENCES categories(id),
    name       text NOT NULL,
    slug       text NOT NULL UNIQUE
);

CREATE TABLE suppliers (
    id       integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name     text NOT NULL,
    email    text,
    country  char(2) NOT NULL
);

CREATE TABLE products (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category_id  integer NOT NULL REFERENCES categories(id),
    sku          text NOT NULL UNIQUE,
    name         text NOT NULL,
    price        numeric(12,2) NOT NULL CHECK (price >= 0),
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_products_category ON products (category_id);

CREATE TABLE product_suppliers (
    product_id   bigint  NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    supplier_id  integer NOT NULL REFERENCES suppliers(id),
    cost         numeric(12,2) NOT NULL,
    PRIMARY KEY (product_id, supplier_id)
);

CREATE TABLE warehouses (
    id    integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code  text NOT NULL UNIQUE,
    city  text NOT NULL
);

CREATE TABLE inventory (
    warehouse_id  integer NOT NULL REFERENCES warehouses(id),
    product_id    bigint  NOT NULL REFERENCES products(id),
    quantity      integer NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (warehouse_id, product_id)
);

CREATE TABLE orders (
    id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id          bigint NOT NULL REFERENCES customers(id),
    shipping_address_id  bigint REFERENCES addresses(id),
    status               order_status NOT NULL DEFAULT 'pending',
    total                numeric(12,2) NOT NULL DEFAULT 0,
    ordered_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_ordered_at ON orders (ordered_at);

CREATE TABLE order_items (
    order_id    bigint NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id  bigint NOT NULL REFERENCES products(id),
    quantity    integer NOT NULL CHECK (quantity > 0),
    unit_price  numeric(12,2) NOT NULL,
    PRIMARY KEY (order_id, product_id)
);

CREATE TABLE payments (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id  bigint NOT NULL REFERENCES orders(id),
    amount    numeric(12,2) NOT NULL,
    method    text NOT NULL CHECK (method IN ('card', 'paypal', 'cash')),
    paid_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_payments_order ON payments (order_id);

CREATE TABLE shipments (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      bigint NOT NULL REFERENCES orders(id),
    warehouse_id  integer NOT NULL REFERENCES warehouses(id),
    tracking_no   text,
    shipped_at    timestamptz,
    delivered_at  timestamptz
);

CREATE TABLE reviews (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id   bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    customer_id  bigint NOT NULL REFERENCES customers(id),
    rating       smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body         text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_status_history (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    old_status  order_status,
    new_status  order_status NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now()
);

-- Triggers ----------------------------------------------------------------

CREATE TRIGGER trg_customers_updated_at BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_products_updated_at BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_orders_updated_at BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION log_order_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO order_status_history (order_id, old_status, new_status)
        VALUES (NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_orders_status_history AFTER INSERT OR UPDATE OF status ON orders
    FOR EACH ROW EXECUTE FUNCTION log_order_status();

-- Views ------------------------------------------------------------------

CREATE VIEW v_order_summary AS
SELECT o.id AS order_id,
       c.email,
       o.status,
       o.ordered_at,
       count(oi.product_id)                  AS item_count,
       coalesce(sum(oi.quantity * oi.unit_price), 0) AS items_total
FROM orders o
JOIN customers c ON c.id = o.customer_id
LEFT JOIN order_items oi ON oi.order_id = o.id
GROUP BY o.id, c.email;

CREATE VIEW v_product_stock AS
SELECT p.id AS product_id,
       p.sku,
       p.name,
       coalesce(sum(i.quantity), 0) AS total_quantity
FROM products p
LEFT JOIN inventory i ON i.product_id = p.id
GROUP BY p.id;

-- Materialized views --------------------------------------------------------

CREATE MATERIALIZED VIEW mv_daily_sales AS
SELECT date_trunc('day', o.ordered_at)::date AS day,
       count(DISTINCT o.id)               AS orders,
       sum(oi.quantity * oi.unit_price)   AS revenue
FROM orders o
JOIN order_items oi ON oi.order_id = o.id
WHERE o.status <> 'cancelled'
GROUP BY 1;
CREATE UNIQUE INDEX idx_mv_daily_sales_day ON mv_daily_sales (day);

CREATE MATERIALIZED VIEW mv_product_ratings AS
SELECT p.id AS product_id,
       p.name,
       count(r.id)          AS review_count,
       round(avg(r.rating), 2) AS avg_rating
FROM products p
LEFT JOIN reviews r ON r.product_id = p.id
GROUP BY p.id, p.name;

CREATE MATERIALIZED VIEW mv_customer_ltv AS
SELECT c.id AS customer_id,
       c.email,
       count(o.id)                  AS order_count,
       coalesce(sum(o.total), 0)    AS lifetime_value,
       max(o.ordered_at)            AS last_order_at
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id AND o.status <> 'cancelled'
GROUP BY c.id, c.email;
