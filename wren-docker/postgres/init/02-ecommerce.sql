-- Wren 电商场景测试库（POC 第二阶段）
-- 在现有 test 库基础上新增 4 张表，与 customers/orders 构成 6 表电商模型
-- 目的：验证 Wren MDL 语义层在多表 JOIN / 枚举口径 / 维度退化场景下的能力

-- 1. 商品类目（维度表）
CREATE TABLE IF NOT EXISTS categories (
    category_id   INTEGER PRIMARY KEY,
    category_name VARCHAR(30)
);
INSERT INTO categories VALUES
    (1, '数码电器'),
    (2, '家居生活'),
    (3, '服饰鞋包');

-- 2. 商品（含 category_name 维度退化冗余列，模拟真实宽表）
CREATE TABLE IF NOT EXISTS products (
    product_id    INTEGER PRIMARY KEY,
    product_name  VARCHAR(60),
    category_id   INTEGER REFERENCES categories(category_id),
    category_name VARCHAR(30),   -- 维度退化：冗余存储类目名，避免每次 JOIN
    price         NUMERIC(10, 2),
    stock         INTEGER
);
INSERT INTO products VALUES
    (101, '无线蓝牙耳机', 1, '数码电器', 299.00, 120),
    (102, '智能手表',     1, '数码电器', 899.00,  60),
    (103, '北欧风台灯',   2, '家居生活', 159.00, 200),
    (104, '记忆棉枕头',   2, '家居生活',  89.00, 350),
    (105, '纯棉T恤',      3, '服饰鞋包',  59.00, 500),
    (106, '运动跑鞋',     3, '服饰鞋包', 399.00,  80);

-- 3. 订单明细（事实表，关联订单与商品）
CREATE TABLE IF NOT EXISTS order_items (
    item_id     INTEGER PRIMARY KEY,
    order_id    INTEGER REFERENCES orders(order_id),
    product_id  INTEGER REFERENCES products(product_id),
    quantity    INTEGER,
    unit_price  NUMERIC(10, 2),
    subtotal    NUMERIC(12, 2)
);
INSERT INTO order_items VALUES
    (1, 1001, 101, 1, 299.00, 299.00),
    (2, 1001, 103, 2, 159.00, 318.00),
    (3, 1002, 105, 3,  59.00, 177.00),
    (4, 1003, 102, 1, 899.00, 899.00),
    (5, 1003, 106, 1, 399.00, 399.00),
    (6, 1004, 104, 2,  89.00, 178.00),
    (7, 1005, 105, 1,  59.00,  59.00),
    (8, 1006, 101, 2, 299.00, 598.00),
    (9, 1006, 102, 1, 899.00, 899.00),
    (10, 1007, 103, 1, 159.00, 159.00),
    (11, 1007, 106, 1, 399.00, 399.00),
    (12, 1003, 104, 1,  89.00,  89.00);

-- 4. 退款（事实表，关联订单）
CREATE TABLE IF NOT EXISTS refunds (
    refund_id      INTEGER PRIMARY KEY,
    order_id       INTEGER REFERENCES orders(order_id),
    refund_date    DATE,
    refund_amount  NUMERIC(10, 2),
    reason         VARCHAR(60)
);
INSERT INTO refunds VALUES
    (1, 1005, '2025-08-20', 59.00, '尺码不合适'),
    (2, 1007, '2025-09-10', 399.00, '质量问题');

-- 注释：order 表的 status 枚举（供语义层口径说明）
-- 1=已下单 2=已发货 3=已完成 4=已退款
COMMENT ON COLUMN orders.status IS '订单状态码值：1=已下单 2=已发货 3=已完成 4=已退款';
