-- Wren 测试用示例数据：简易零售库
-- 用途：验证 Wren MCP 的 Text-to-SQL / MDL 语义层能力

CREATE TABLE customers (
    customer_id  INTEGER PRIMARY KEY,
    first_name   VARCHAR(50),
    last_name    VARCHAR(50),
    segment      VARCHAR(20),        -- customer_type: 消费者 / 公司
    created_at   TIMESTAMP
);

CREATE TABLE orders (
    order_id     INTEGER PRIMARY KEY,
    customer_id  INTEGER REFERENCES customers(customer_id),
    order_date   DATE,
    status       INTEGER,            -- 1=已下单 2=已发货 3=已完成 4=已退款
    amount       NUMERIC(10, 2)
);

INSERT INTO customers VALUES
    (1, 'Tiffany', 'Zhang',  'consumer', '2024-01-15 09:30:00'),
    (2, 'Lukas',   'Wang',   'company',  '2024-02-20 14:00:00'),
    (3, 'Jennifer','Li',     'consumer', '2024-03-01 11:20:00'),
    (4, 'Wei',     'Chen',   'company',  '2024-05-10 16:45:00'),
    (5, 'Ming',    'Zhao',   'consumer', '2025-01-08 10:05:00');

INSERT INTO orders VALUES
    (1001, 1, '2025-06-01', 3, 1245.67),
    (1002, 1, '2025-07-15', 3,  89.90),
    (1003, 2, '2025-06-20', 3, 1102.30),
    (1004, 3, '2025-07-02', 2,  108.45),
    (1005, 3, '2025-08-11', 4,  256.00),
    (1006, 4, '2025-08-30', 1,  780.00),
    (1007, 5, '2025-09-05', 1,  320.50);

COMMENT ON COLUMN orders.status IS '订单状态码值：1=已下单 2=已发货 3=已完成 4=已退款';
