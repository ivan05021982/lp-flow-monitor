# On-chain extraction

Reads Uniswap V3 pool `Mint` and `Burn` logs from public RPC nodes and turns them into the
daily LP-flow series shown on the [monitor page](https://ivan05021982.github.io/lp-flow-monitor/).
It replaces the Dune query for the weekly monitor: since September 2026 Dune's free plan no
longer runs queries.

**Status.** Added after release v3.0.3. These files are **outside the pinned manifest**:
`python manifest.py` neither covers nor checks them. They are the code that produces the
page's `data/daily_lp_flow.csv`. The weekly runner that writes the log and the Dune
extractions used to accept the switch are not in this repository.

## Run it

```bash
pip install -r onchain/requirements.txt

# Recompute the file the page publishes (four USDC/WETH 0.05% pools, 90 days)
python onchain/daily_flow.py --start 2026-07-07 --end 2026-10-04 --out daily_lp_flow.csv

# A subset, plus one CSV per pool in the input format of the v3 detector
python onchain/daily_flow.py --start 2026-08-21 --end 2026-10-04 --out daily.csv \
    --chains ethereum,optimism --detector-dir detector_inputs
python examples/run_detector_on_csv.py detector_inputs/usdc_weth_500_eth.csv --label "Ethereum USDC/WETH"

# Offline tests: a synthetic chain stands in for the node, nothing is contacted
python tests/test_onchain_offline.py
```

Ethereum and Optimism take about a minute, Arbitrum a few minutes. Base takes half an hour or
more the first time: its public nodes serve 500 to 1,000 blocks per request. Events are cached
in `onchain/_cache/`, so later runs read only the blocks that are new. `--datasets` selects
pools from [`pools.json`](../pools.json); without it the pools marked `dashboard_query_id` are
used.

## What it does

- **Events.** `eth_getLogs` on the pool address for the two event signatures, from the nodes
  listed in [`onchain_sources.json`](onchain_sources.json): two keyless public nodes per chain,
  with the request limits measured for them on 2026-10-05. Public nodes change their limits or
  disappear; that file is where to replace them.
- **Two nodes must agree.** After reading a range from one node, the last 14 days of it are
  read from the second and compared event by event (block, log index, transaction, kind,
  amounts, timestamp). If they differ, or the second node cannot be read, the chain is left
  out and the command exits non-zero. `--cross-check-days` widens or, with `0`, skips the
  comparison; a skipped comparison is printed, not silent.
- **Days.** UTC calendar date of the block timestamp. A node that leaves the timestamp off its
  logs (Arbitrum's official node returns `0x0`) is completed from block headers; a cache
  without timestamps is refused rather than bucketed.
- **USD.** Token amounts times one price per token per UTC day: the mean of the 24 hourly
  DefiLlama prices of the token on Ethereum mainnet. A day with fewer than 22 of the 24 hourly
  points is not priced and stops the run. USDC and USDT are priced like any other token, not
  assumed to be 1 dollar.
- **Output.** Per pool and day: `mint_count`, `burn_count`, `unique_txs`, the four token
  amounts, the two prices used, `gross_lp_inflow_usd`, `gross_lp_outflow_usd`,
  `net_lp_flow_usd`, `price_source`. With `--detector-dir`, also the four columns the v3
  detector reads; `n_unpriced_legs` is 0 by construction, because an unpriced day stops the run.

## How the switch from Dune was checked

Against the Dune extractions of the same 8 pools kept from June to September 2026, with the
tests and thresholds written before the first comparison
([`docs/onchain_extraction_contract.md`](../docs/onchain_extraction_contract.md)):

- **Event counts.** Mints, burns and distinct transactions per day identical on all 3,408
  pool-days (882,221 events, 2026-04-21 to 2026-09-16).
- **Amounts.** Dune reported dollars, not token amounts. Solving Dune's dollars against the
  chain amounts, one unknown price per token per day, leaves a relative residual of 3e-13 on
  Ethereum (8 equations, 4 prices) and 1e-14 on Arbitrum (4 equations, 3 prices).
- **Statuses.** On all 160 historical 45-day windows `detect_monitoring_v2` returns the same
  status and the same outflow runs from either source. The window net differs by at most
  0.0512% of the window's gross inflow; that difference is the price feed.

The Dune extractions are not in this repository, so those three checks cannot be re-run from
it. What can be re-run here is the two-node comparison, which every run performs, and the
recomputation of the published CSV.

## Limits

- **Public nodes.** No key, no guarantee. One company's gateway (Tenderly) appears on all four
  chains: as the working source on Ethereum and Arbitrum, as the second node on Base and
  Optimism.
- **The comparison covers the last 14 days of a range by default**, not the whole range.
- **On Arbitrum the second node gives no log timestamps.** Events are compared on everything
  else; timestamps were checked once on a sample of 300 blocks against that node's block
  headers, with no mismatch.
- **Dollars depend on an off-chain price feed.** Token amounts and event counts come from the
  chain; the USD figures also depend on DefiLlama's hourly prices. Against a Dune-priced
  figure the window net stays within 0.06% of the window's gross inflow.
- **Pool events only.** Mint and Burn at the pool contract, zero-amount burns included. No
  attribution to positions or owners, no swap flow, no TVL.
- **Four tokens, four chains.** Pools of USDC, USDT, WETH and WBTC on Ethereum, Arbitrum, Base
  and Optimism. Another token needs a reference price and its decimals in the code; another
  chain needs two nodes in `onchain_sources.json`.
- **Not the v3 release.** No manifest digest, no schema file, no version tag covers this code.
