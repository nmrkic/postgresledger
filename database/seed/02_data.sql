-- A little test data so the materialized views have something to show.

INSERT INTO customers (email, full_name, phone) VALUES
    ('ana@example.com',   'Ana Petrovic',   '+381601111111'),
    ('marko@example.com', 'Marko Jovanovic', NULL),
    ('jelena@example.com','Jelena Ilic',    '+381602222222');

INSERT INTO addresses (customer_id, line1, city, postal_code, is_default) VALUES
    (1, 'Knez Mihailova 1', 'Belgrade', '11000', true),
    (2, 'Bulevar oslobodjenja 5', 'Novi Sad', '21000', true),
    (3, 'Obilicev venac 3', 'Nis', '18000', true);

INSERT INTO categories (name, slug) VALUES ('Electronics', 'electronics'), ('Books', 'books');
INSERT INTO categories (parent_id, name, slug) VALUES (1, 'Laptops', 'laptops');

INSERT INTO suppliers (name, email, country) VALUES
    ('TechDist', 'sales@techdist.example', 'DE'),
    ('Bookstore Plus', 'info@bookstoreplus.example', 'RS');

INSERT INTO products (category_id, sku, name, price) VALUES
    (3, 'LAP-001', 'Laptop 14"', 899.00),
    (3, 'LAP-002', 'Laptop 16"', 1299.00),
    (2, 'BK-001',  'PostgreSQL book', 39.90);

INSERT INTO product_suppliers VALUES (1, 1, 700), (2, 1, 1050), (3, 2, 25);

INSERT INTO warehouses (code, city) VALUES ('BG1', 'Belgrade'), ('NS1', 'Novi Sad');
INSERT INTO inventory VALUES (1, 1, 10), (1, 2, 4), (2, 3, 50), (2, 1, 3);

INSERT INTO orders (customer_id, shipping_address_id, total, ordered_at) VALUES
    (1, 1, 938.90, now() - interval '3 days'),
    (2, 2, 1299.00, now() - interval '2 days'),
    (3, 3, 79.80, now() - interval '1 day');

INSERT INTO order_items VALUES (1, 1, 1, 899.00), (1, 3, 1, 39.90), (2, 2, 1, 1299.00), (3, 3, 2, 39.90);

UPDATE orders SET status = 'paid' WHERE id IN (1, 2);
UPDATE orders SET status = 'shipped' WHERE id = 1;

INSERT INTO payments (order_id, amount, method) VALUES (1, 938.90, 'card'), (2, 1299.00, 'paypal');
INSERT INTO shipments (order_id, warehouse_id, tracking_no, shipped_at) VALUES (1, 1, 'RS123456789', now());

INSERT INTO reviews (product_id, customer_id, rating, body) VALUES
    (1, 1, 5, 'Excellent'), (3, 1, 4, 'Useful'), (3, 3, 5, NULL);

REFRESH MATERIALIZED VIEW mv_daily_sales;
REFRESH MATERIALIZED VIEW mv_product_ratings;
REFRESH MATERIALIZED VIEW mv_customer_ltv;
