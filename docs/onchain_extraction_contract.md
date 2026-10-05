> This is the record of how the on-chain extraction was accepted against the Dune history. It was
> written in the working repository on 2026-10-05, sections 1 to 6 before the first run. File paths
> and the commands `validate_onchain_vs_dune.py` and `run_weekly_monitor.py` refer to that
> repository: the Dune extractions and the weekly runner are not published here. The extraction
> code itself is in [`onchain/`](../onchain/).

# On-chain LP-flow extraction — contract and acceptance tests

Written 2026-10-05, **before** the extractor and before any comparison was run.
Dune's free plan became view-only (no query execution since 2026-09-24), so the monitor's
extraction step moves from Dune SQL to raw chain logs read from public RPC nodes. The
detector, the window and the day definition do not change.

## 1. What counts as truth here

| # | Authority | Where | What it fixes |
|---|-----------|-------|---------------|
| A1 | Dune extractions already on disk | `data/processed/dune/<dataset>/<run_id>/rows.csv` | per day: `mint_count`, `burn_count`, `unique_txs` (exact integers), USD in / out / net. Last run 2026-09-17, window 2026-08-03 → 2026-09-16 |
| A2 | Cached results of the 4 dashboard queries | `data/processed/dune_cache_snapshot_2026-10-05/` | 90 days of daily net and gross inflow USD, 2026-06-19 → 2026-09-16 |
| A3 | The chain itself | two independent RPC providers per chain | the events |

Not truth: my reading of the event layout, a single provider's answer, documentation.
A1 is the test written by the owner of the boundary we are replacing: the new extractor is
accepted against it, not against a test of mine.

## 2. Inputs

| Input | Type | Produced by | Complete or in pieces | Re-readable | Consumed by |
|-------|------|-------------|-----------------------|-------------|-------------|
| `eth_getLogs` result | list of log objects | RPC provider | **in pieces**: block-range windows, limit differs per provider (measured 2026-10-05, see `onchain_sources.json`) | yes — only blocks up to the end of yesterday UTC are requested, hours past finality | decoder |
| block header | `timestamp` | RPC provider | one block per call | yes | range finder (first block of a UTC day) |
| pool config | addresses, decimals, token order | `pools.json` | complete | yes | decoder, valuation |
| daily USD price per token | float | **open decision, section 5** | — | — | valuation |

Log fields used: `address`, `topics[0]`, `data`, `blockNumber`, `logIndex`,
`transactionHash`, `blockTimestamp`, `removed` (must be false).

Event layouts assumed (Uniswap V3 pool), to be confirmed by T1 and T3, not by reading docs:

- `Mint(address sender, address indexed owner, int24 indexed tickLower, int24 indexed tickUpper, uint128 amount, uint256 amount0, uint256 amount1)` — data words: sender, amount, **amount0**, **amount1**
- `Burn(address indexed owner, int24 indexed tickLower, int24 indexed tickUpper, uint128 amount, uint256 amount0, uint256 amount1)` — data words: amount, **amount0**, **amount1**

Day = UTC calendar date of the block timestamp (Dune's `evt_block_date`).
Zero-amount burns are events like any other: they count in `burn_count` and `unique_txs`.

## 3. Output

Same columns as `lp_flow_template.sql`, so `run_weekly_monitor.process()` is untouched:
`day, pool_label, pool_address, gross_lp_inflow_usd, gross_lp_outflow_usd, net_lp_flow_usd,
mint_count, burn_count, unique_txs` — plus the raw token amounts per day
(`mint_amount0, mint_amount1, burn_amount0, burn_amount1`, in token units) so every USD
figure can be recomputed from chain data and a stated price.

## 4. Acceptance tests (thresholds fixed before running)

**T1 — event counts, exact.** For every pool and every day of the last A1 window:
`mint_count`, `burn_count`, `unique_txs` identical to Dune. Pass = 100% of pool-days.
One mismatch = fail, investigated before anything else.

**T2 — two providers agree.** Same set of `(blockNumber, logIndex, transactionHash, data)`
from both providers. Full range on Ethereum, Arbitrum, Optimism; on Base (500–1000 block
windows) a declared sample of days. Pass = identical.

**T3 — raw amounts, without Dune's raw amounts.** Dune gives USD in and USD out per day;
with our token amounts that is two equations in the two daily prices Dune used. Solve them.
Expected, as a diagnostic: implied stablecoin price within ±0.5% of 1 (median), implied
WETH price from the four chains' USDC/WETH pools agreeing with each other. Wrong decimals,
wrong token order or wrong data words cannot produce sane prices on four chains at once.

**T4 — what gets published.** Re-run `detect_monitoring_v2` on every historical window on
disk using the new series. Pass = same status and same latest-run dates for every
(pool, window). Window-net differences are reported, not hidden. The USD tolerance is
**measured and then declared**; it is not asserted here. A status that differs is not
auto-accepted: it goes to the owner.

Order of rigor: T1 and T4 carry the consequence (they decide what is published); T2 and T3
are supporting evidence.

## 5. Open decision — the daily price

Dune valued each event with `prices.day` (one USD price per token per day). Token amounts
can be reproduced exactly; that price feed cannot. Candidates are compared against the
prices implied by T3 before choosing; the residual becomes the declared tolerance.

## 6. Out of scope

No change to the detector, the 45-day window, the pool set, or the meaning of a day.
The Real-Yield scorecard is a separate migration.

## 7. Outcome — 2026-10-05

Sections 1–6 are as written before the first run. This section was added after.
All four tests passed on the 8 pools (`py methodology/monitoring/validate_onchain_vs_dune.py t1|t2|t2h|t3|t4`).

| Test | Result |
|------|--------|
| T1 | 3,408 / 3,408 pool-day cells identical: 882,221 events, every day present in a Dune extraction on disk (2026-04-21 → 2026-09-16) |
| T2 | Same events from both providers on every pool. Ethereum 148,926 and Optimism 29,672 events over 2026-04-21 → 2026-10-04; Arbitrum 138,240 over 2026-08-03 → 2026-10-04; Base 50,936 over 21 days (2026-08-10, 08-28, 09-15 and all of 2026-09-17 → 2026-10-04) |
| T3 | Largest relative residual 3.0e-13 on Ethereum (8 equations, 4 prices per day) and 1.0e-14 on Arbitrum (4 equations, 3 prices). Implied stablecoin prices 0.9990–1.0003. Implied WETH price identical on the four chains |
| T4 | 160 / 160 historical windows: same status, same runs. Window net differs from the Dune figure by at most 0.0512% of the window gross inflow (median about 0.002%) |

**Declared tolerance.** Against a Dune-priced figure, the window net is within 0.06% of the
window gross inflow. Token amounts and event counts carry no tolerance: they are identical.

**The daily price (section 5), decided by the owner on 2026-10-05.** The prices implied by
T3 show that Dune's `prices.day` is a day average, the same for a token on every chain. The
mean of the 24 hourly DefiLlama prices of the Ethereum-mainnet token reproduces it with a
median error of 0.017% on WETH (largest 0.30%), 0.03% on WBTC and under 0.03% on USDC and
USDT; an open or close price misses by up to 12% on the most volatile days. A day is priced only with at least
22 of the 24 hourly points: 3 token-days out of 668 had 22 or 23, none fewer.

**What did not go as written.**

- *T1 failed on Arbitrum at the first run.* The official Arbitrum node returns
  `blockTimestamp: 0x0` on logs, so every event fell outside every day while the totals
  matched Dune. The extractor now treats a zero or absent timestamp as missing and reads the
  block header; Tenderly, which fills the field, became Arbitrum's working source.
- *T2 on Arbitrum is weaker than specified.* The official node rate-limits header reads, so
  its events are compared on block, log index, transaction, kind and amounts, without the
  timestamp. Timestamps are covered separately by `t2h`: 300 blocks sampled across the cache,
  Tenderly's log timestamp against the official header, 0 mismatches.
- *T3's "well-conditioned" filter* uses the condition number of the column-scaled system;
  token amounts on very different scales made the raw one meaningless.

**Standing check, enforced by the monitor.** From 2026-09-17 there is no Dune figure to
compare with: agreement between the two nodes is the only check left on new data. It is
therefore a gate inside `run_weekly_monitor.py`, not a step to remember. On every run the
last 14 days of the window are read from the second node as well
(`onchain_extract.cross_check`) and a chain is classified only if both return the same
events. If they disagree, or the second node cannot be read, the chain's pools get no row;
`--allow-unchecked` overrides this and logs the rows with a source ending in `_unchecked`.
The gate was tested by tampering with the second node's events in memory: one event
missing, one amount changed and one timestamp moved are each reported.

**First rows written with this extraction (2026-10-05):** windows ending 2026-09-20, 09-23,
09-27 and 09-30 as `backfill_onchain` (their scheduled runs were missed), and the window
ending 2026-10-04 as `run_onchain`.
