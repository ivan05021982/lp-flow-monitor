-- LP-flow monitoring TEMPLATE (v3, fail-closed) — fill the placeholders per pool/window
-- (see README, "Use it on your own pool"). Generic Uniswap V3 daily LP-flow (Mint/Burn), any chain.
-- Placeholders: __CHAIN__ __START_DATE__ __END_DATE__ __POOL_ADDRESS__ __TOKEN0_ADDRESS__
--               __TOKEN0_DECIMALS__ __TOKEN1_ADDRESS__ __TOKEN1_DECIMALS__ __POOL_LABEL__
--
-- v3 PRICE POLICY (fail-closed): prices are NOT coerced to zero. A Mint/Burn leg with a non-zero
-- token amount but a missing OR non-positive price (price IS NULL OR price <= 0) is counted in
-- n_unpriced_legs, and its USD value is left NULL (excluded from the sums). The v3 detector returns
-- DATA_ERROR for any window where n_unpriced_legs <> 0 on any day, so a price-coverage gap can never
-- be silently misread as "no flow". Output column is "date" (matches the schema). Descriptive only.
WITH
params AS (
    SELECT DATE '__START_DATE__' AS start_date, DATE '__END_DATE__' AS end_date
),
calendar AS (
    SELECT CAST(day AS DATE) AS block_date
    FROM params
    CROSS JOIN UNNEST(sequence(start_date, end_date, INTERVAL '1' day)) AS t(day)
),
pool_events AS (
    SELECT evt_block_date AS block_date, 'pool_mint' AS event_type,
           CAST(amount0 AS DOUBLE) AS amount0_raw, CAST(amount1 AS DOUBLE) AS amount1_raw,
           CAST(evt_tx_hash AS VARCHAR) AS tx_hash
    FROM uniswap_v3___CHAIN__.uniswapv3pool_evt_mint
    CROSS JOIN params prm
    WHERE contract_address = __POOL_ADDRESS__
      AND evt_block_date >= prm.start_date AND evt_block_date <= prm.end_date
    UNION ALL
    SELECT evt_block_date AS block_date, 'pool_burn' AS event_type,
           CAST(amount0 AS DOUBLE) AS amount0_raw, CAST(amount1 AS DOUBLE) AS amount1_raw,
           CAST(evt_tx_hash AS VARCHAR) AS tx_hash
    FROM uniswap_v3___CHAIN__.uniswapv3pool_evt_burn
    CROSS JOIN params prm
    WHERE contract_address = __POOL_ADDRESS__
      AND evt_block_date >= prm.start_date AND evt_block_date <= prm.end_date
),
price_token0 AS (
    SELECT CAST(timestamp AS DATE) AS price_date, price
    FROM prices.day
    WHERE blockchain = '__CHAIN__' AND contract_address = __TOKEN0_ADDRESS__
),
price_token1 AS (
    SELECT CAST(timestamp AS DATE) AS price_date, price
    FROM prices.day
    WHERE blockchain = '__CHAIN__' AND contract_address = __TOKEN1_ADDRESS__
),
valued AS (
    -- Valid price = strictly positive. Invalid (NULL or <= 0) -> USD value NULL (excluded from
    -- sums) AND counted in unpriced_legs, so the day is flagged rather than silently understated.
    SELECT e.block_date, e.event_type, e.tx_hash,
           CASE WHEN p0.price > 0 THEN e.amount0_raw / POWER(10, __TOKEN0_DECIMALS__) * p0.price END AS amount0_usd,
           CASE WHEN p1.price > 0 THEN e.amount1_raw / POWER(10, __TOKEN1_DECIMALS__) * p1.price END AS amount1_usd,
           (CASE WHEN e.amount0_raw <> 0 AND (p0.price IS NULL OR p0.price <= 0) THEN 1 ELSE 0 END
          + CASE WHEN e.amount1_raw <> 0 AND (p1.price IS NULL OR p1.price <= 0) THEN 1 ELSE 0 END) AS unpriced_legs
    FROM pool_events e
    LEFT JOIN price_token0 p0 ON p0.price_date = e.block_date
    LEFT JOIN price_token1 p1 ON p1.price_date = e.block_date
)
SELECT
    c.block_date AS "date",
    '__POOL_LABEL__' AS pool_label, '__POOL_ADDRESS__' AS pool_address,
    SUM(CASE WHEN v.event_type='pool_mint' THEN v.amount0_usd+v.amount1_usd ELSE 0 END) AS gross_lp_inflow_usd,
    SUM(CASE WHEN v.event_type='pool_burn' THEN v.amount0_usd+v.amount1_usd ELSE 0 END) AS gross_lp_outflow_usd,
    SUM(CASE WHEN v.event_type='pool_mint' THEN v.amount0_usd+v.amount1_usd
             WHEN v.event_type='pool_burn' THEN -(v.amount0_usd+v.amount1_usd)
             ELSE 0 END) AS net_lp_flow_usd,
    COALESCE(SUM(v.unpriced_legs), 0) AS n_unpriced_legs,
    COUNT_IF(v.event_type='pool_mint') AS mint_count,
    COUNT_IF(v.event_type='pool_burn') AS burn_count,
    COUNT(DISTINCT v.tx_hash) AS unique_txs
FROM calendar c
LEFT JOIN valued v ON v.block_date = c.block_date
GROUP BY 1, 2, 3
ORDER BY c.block_date
