-- =============================================================================
-- sample_queries.sql
-- Athena SQL queries for the Data Engineering & Analytics Platform
--
-- Workgroup : de-analytics-workgroup
-- Database  : de_project_db
-- Table     : (auto-named by the Glue crawler from the raw-data S3 prefix)
--             Assumed name: raw_data  — adjust if the crawler produces a
--             different table name (check the Glue catalog after the first
--             crawler run).
--
-- All queries use partition pruning via the Hive-style columns
-- (year, month, day, hour) that Firehose writes to the S3 prefix.
-- Always filter on these columns to avoid full-table scans.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Query 1: Total purchases and revenue per calendar day
--
-- Answers: "How much did we sell each day, and how many purchase events
--           were recorded?"
-- Partitions used: year, month, day
-- -----------------------------------------------------------------------------
SELECT
    year,
    month,
    day,
    COUNT(*)                                    AS total_purchases,
    SUM(CAST(json_extract_scalar(
        "$path_col", '$.amount') AS DOUBLE))    AS total_revenue,
    AVG(CAST(json_extract_scalar(
        "$path_col", '$.amount') AS DOUBLE))    AS avg_order_value
FROM de_project_db.raw_data
WHERE event_type = 'purchase'
  AND year  = CAST(year(current_date)  AS VARCHAR)
  AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
GROUP BY year, month, day
ORDER BY year DESC, month DESC, day DESC;


-- -----------------------------------------------------------------------------
-- Query 2: Top 20 users by total spend (current month)
--
-- Answers: "Who are our highest-value customers this month?"
-- Partitions used: year, month
-- -----------------------------------------------------------------------------
SELECT
    user_id,
    COUNT(*)                                            AS purchase_count,
    SUM(CAST(amount AS DOUBLE))                        AS total_spend,
    ROUND(AVG(CAST(amount AS DOUBLE)), 2)              AS avg_order_value,
    MAX(CAST(amount AS DOUBLE))                        AS largest_order,
    ARBITRARY(currency)                                AS currency
FROM de_project_db.raw_data
WHERE event_type = 'purchase'
  AND year  = CAST(year(current_date)  AS VARCHAR)
  AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
GROUP BY user_id
ORDER BY total_spend DESC
LIMIT 20;


-- -----------------------------------------------------------------------------
-- Query 3: Average order value and conversion funnel by product category
--
-- Answers: "Which categories drive the most revenue, and what percentage
--           of views convert to purchases?"
-- Partitions used: year, month
-- -----------------------------------------------------------------------------
WITH events_this_month AS (
    SELECT
        product_category,
        event_type,
        CAST(amount AS DOUBLE) AS amount
    FROM de_project_db.raw_data
    WHERE year  = CAST(year(current_date)  AS VARCHAR)
      AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
),
category_stats AS (
    SELECT
        product_category,
        COUNT(*) FILTER (WHERE event_type = 'view')     AS view_count,
        COUNT(*) FILTER (WHERE event_type = 'cart_add') AS cart_add_count,
        COUNT(*) FILTER (WHERE event_type = 'purchase') AS purchase_count,
        SUM(amount) FILTER (WHERE event_type = 'purchase')  AS total_revenue,
        AVG(amount) FILTER (WHERE event_type = 'purchase')  AS avg_order_value
    FROM events_this_month
    GROUP BY product_category
)
SELECT
    product_category,
    view_count,
    cart_add_count,
    purchase_count,
    ROUND(total_revenue, 2)                                         AS total_revenue,
    ROUND(avg_order_value, 2)                                       AS avg_order_value,
    ROUND(100.0 * cart_add_count / NULLIF(view_count, 0), 1)        AS view_to_cart_pct,
    ROUND(100.0 * purchase_count / NULLIF(view_count, 0), 1)        AS view_to_purchase_pct
FROM category_stats
ORDER BY total_revenue DESC;


-- -----------------------------------------------------------------------------
-- Query 4: Hourly event volume and revenue heatmap (last 7 days)
--
-- Answers: "When is our platform busiest, and when do purchases peak?"
--          Useful for capacity planning and promotion scheduling.
-- Partitions used: year, month, day, hour
-- -----------------------------------------------------------------------------
SELECT
    year,
    month,
    day,
    hour,
    COUNT(*)                                                  AS total_events,
    COUNT(*) FILTER (WHERE event_type = 'purchase')           AS purchase_events,
    COUNT(*) FILTER (WHERE event_type = 'view')               AS view_events,
    COUNT(*) FILTER (WHERE event_type = 'cart_add')           AS cart_add_events,
    ROUND(SUM(CAST(amount AS DOUBLE))
        FILTER (WHERE event_type = 'purchase'), 2)            AS hourly_revenue
FROM de_project_db.raw_data
WHERE date_parse(year || '-' || month || '-' || day, '%Y-%m-%d')
        >= date_add('day', -7, current_date)
GROUP BY year, month, day, hour
ORDER BY year DESC, month DESC, day DESC, hour DESC;


-- -----------------------------------------------------------------------------
-- Query 5: Platform and country breakdown for purchases (current month)
--
-- Answers: "Where are our customers buying from, and on which device?"
--          Supports marketing attribution and localisation decisions.
-- Partitions used: year, month
-- -----------------------------------------------------------------------------
SELECT
    JSON_EXTRACT_SCALAR(client, '$.platform')       AS platform,
    JSON_EXTRACT_SCALAR(client, '$.country_code')   AS country_code,
    currency,
    COUNT(*)                                        AS purchase_count,
    ROUND(SUM(CAST(amount AS DOUBLE)), 2)           AS total_revenue,
    ROUND(AVG(CAST(amount AS DOUBLE)), 2)           AS avg_order_value,
    ROUND(AVG(CAST(discount_pct AS DOUBLE)) * 100, 1) AS avg_discount_pct
FROM de_project_db.raw_data
WHERE event_type = 'purchase'
  AND year  = CAST(year(current_date)  AS VARCHAR)
  AND month = LPAD(CAST(month(current_date) AS VARCHAR), 2, '0')
GROUP BY
    JSON_EXTRACT_SCALAR(client, '$.platform'),
    JSON_EXTRACT_SCALAR(client, '$.country_code'),
    currency
ORDER BY total_revenue DESC
LIMIT 50;
