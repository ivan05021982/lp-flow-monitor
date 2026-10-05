# LP-Flow Monitor — a versioned, fail-closed Uniswap V3 liquidity-outflow primitive

A frozen, versioned detector + a generic Dune query template + an open prospective dataset for
**observing large, sustained, non-replenished LP outflows** on Uniswap V3 pools. Built only on
direct on-chain Mint/Burn events.

**Monitor page:** https://ivan05021982.github.io/lp-flow-monitor/ — daily net LP flow of the four
pools and the weekly log, rebuilt after each weekly run (not live; a raw-flow view with v2 statuses —
see *Status & known limitations*). The earlier Dune dashboard,
https://dune.com/ivan_nania/lp-flow-monitor-uniswap-v3, is frozen at 2026-09-16.

---

## What this is NOT (read this first)

The discipline below *is* the project. Stating the boundaries up front is the point — not a disclaimer.

This monitor:

- is **NOT a stress predictor or early-warning system.** A `FLAGGED` status means a large
  non-replenished LP outflow *was observed* in the window — it is not a forecast.
- makes **NO precision / recall / F1 / false-positive-rate / signal-to-noise claim.**
- does **NOT distinguish a catalogued stress event from a quiet window.** In the development-set
  characterization there was **no clean separation** — event and control windows flagged at
  similar rates, and the largest, most persistent outflow sat in a *control* window. (See
  [`docs/validation_summary.md`](docs/validation_summary.md); those figures are from the prior
  detector version — a v3 re-characterization is pending, see *Status & known limitations*.)
- makes **NO causal-attribution, event-discrimination, cross-protocol, or cross-chain
  generalization claim.**

Why lead with the negatives? Because large LP outflows happen for many reasons — rebalancing, a
single large LP departing, yield migration — **not just stress**. Full statement:
[`docs/claims_and_limits.md`](docs/claims_and_limits.md).

## What this is

- A **descriptive, fail-closed LP-outflow monitor.** Given a pool's recent daily LP-flow it
  returns one status: `QUIET` / `RESOLVED` / `PENDING` / `FLAGGED`, or **`DATA_ERROR`** if the
  input fails validation or price coverage — it refuses to classify rather than guess.
- **Versioned & integrity-checked:** `detect_monitoring_v3` v3.0.3, threshold `0.01` (relative,
  size-aware; **inherited from v2, not re-validated under v3's net rule** — see *The method*). A
  canonical **pinned manifest** (`manifest.py` + external `MANIFEST.sha256`, digest `7fca459f…`)
  covers the detector, runner, SQL, schema, dependency lock and the snapshot builder; `python
  manifest.py` verifies it and **exits non-zero** on a missing, malformed, or mismatched digest.
  This is **integrity-checked against a pinned manifest — not tamper-proof** (see *Provenance & integrity*).
- Covers **4 USDC/WETH pools** (Ethereum, Arbitrum, Base, Optimism) in the included prospective
  log. `pools.json` configures a wider set; expansion to 8 pools is in progress, not yet in the data.
- The previous version (`detect_monitoring_v2.py`) remains in the repo, **unmodified, as a
  historical artifact**.

## Why it might be useful

- Reconstructs an **auditable LP-flow primitive** (gross inflow / gross outflow / net) per pool/day.
- **Separates LP movements (Mint/Burn) from swap activity** at the measurement level.
- A **fail-closed, explicitly-bounded** building block you can **fork and point at your own
  pools** — it validates its inputs and refuses bad data rather than emitting a misleading result.

---

## Quick start

```bash
pip install -r requirements.txt              # pinned closure; requires Python >= 3.11

python manifest.py                           # verify the pinned manifest (exit != 0 if broken)
python detect_monitoring_v3.py               # integrity check + smoke tests + version info
python tests/test_detect_monitoring_v3.py    # adversarial + golden tests (20)

# Classify a pool's daily-flow CSV (synthetic examples included):
python examples/run_detector_on_csv.py examples/sample_daily_flow_synthetic.csv --label "Demo"
python examples/run_detector_on_csv.py examples/sample_daily_flow_data_error.csv   # -> DATA_ERROR, exit 2
```

## Use it on your own pool

1. **Fork** [`sql/lp_flow_template.sql`](sql/lp_flow_template.sql) on Dune. Fill the placeholders
   for your chain / pool address / token addresses & decimals / window. [`pools.json`](pools.json)
   provides 8 example pool configs (4 are in the current log).
2. **Run** the query and export the daily result as CSV. (Since September 2026 running a query on
   Dune needs a paid plan; its free plan is view-only.)
3. **Classify:** `python examples/run_detector_on_csv.py your_export.csv --label "Your pool"`.

**Without Dune.** [`onchain/`](onchain/) reads the same Mint/Burn events from public RPC nodes and
writes the detector's input directly: `python onchain/daily_flow.py --start 2026-08-21 --end
2026-10-04 --out daily.csv --detector-dir inputs`. It covers pools of USDC, USDT, WETH and WBTC on
the four chains above and sits outside the pinned manifest; see [`onchain/README.md`](onchain/README.md).

The detector requires four columns: `date`, `net_lp_flow_usd`, `gross_lp_inflow_usd`,
`n_unpriced_legs` (all emitted by the v3 SQL template). Any day with `n_unpriced_legs` not exactly
`0` — a Mint/Burn leg with a missing or non-positive price — yields `DATA_ERROR` (no silent zero).
Windows shorter than 14 days are rejected as `DATA_ERROR` (insufficient history; the first ~7 days
are baseline warm-up).

## The method, in one paragraph

A **leakage day** is a day with net outflow whose magnitude exceeds 1% of the pool's own rolling
7-day average gross inflow — where that baseline is the mean of the **positive-inflow days** in the
prior up-to-7 days (partial near the start; the first ~7 days are warm-up). Two or more consecutive
leakage days form a **run**. A run has **failed replenishment** if the **net cumulative flow** over
the following 3 days recovers less than 50% of the outflow (new outflows count against it), and
**persistent memory** if another leakage day appears within 7 days. A window is `FLAGGED` only when a
fully-assessable run both fails replenishment *and* shows memory — and the output reports that
**qualifying run separately** from the latest run, so status and detail cannot contradict. `PENDING`
when a recent run can't be assessed yet; `DATA_ERROR` when input is untrustworthy; `RESOLVED` /
`QUIET` otherwise. The threshold (`0.01`) is **inherited from the v2 descriptive characterization
and has NOT been re-validated under v3's net-replenishment rule** — a v3 re-characterization is
pending (see *Status & known limitations*).

## Pool selection

Pools are selected **per chain, by TVL** (descending), **independent of the detector's signal** —
selecting by LP activity would introduce selection bias and undermine the honesty of the
prospective log. Re-ranked monthly. Uniswap V3 liquidity beyond the anchor pair concentrates on
**Ethereum and Arbitrum**; on Base/Optimism the material liquidity lives on Aerodrome/Velodrome (a
different, Solidly-fork architecture — out of scope). See [`pools.json`](pools.json).

## The dataset

[`data/monitoring_log_snapshot_v3_2026-06-18.csv`](data/monitoring_log_snapshot_v3_2026-06-18.csv)
— the prospective record under the **v3 fail-closed pipeline**: one row per pool (4 USDC/WETH pools,
45-day window ending 2026-06-18), classified by `detect_monitoring_v3`, with full price coverage
(`n_unpriced_legs = 0` every day) and per-run provenance (`schema_version`, `artifact_digest`,
`config_digest`). Raw daily-flow inputs are in [`data/_v3_extract/`](data/_v3_extract); the snapshot
is rebuilt **fail-closed** by [`scripts/build_v3_snapshot.py`](scripts/build_v3_snapshot.py) (exact
4-pool roster, known chains, non-`DATA_ERROR` required, deterministic timestamp).
[`data/DATA_MANIFEST.json`](data/DATA_MANIFEST.json) records the SHA-256 of every source CSV and of
the snapshot, the Dune query/execution IDs, and the expected roster.

All four pools are `FLAGGED` in this window, and in every case the **qualifying run differs from the
latest run** (e.g. Ethereum: qualifying run 2026-06-01 failed replenishment with memory; latest run
2026-06-04 did not) — exactly the case v2 reported incoherently and v3 reports separately. Optimism
is `FLAGGED` despite a roughly flat net over the window: the status reflects an observed outflow
episode, not the sign of the net.

## Provenance & integrity

- **Pinned manifest:** `manifest.py` builds a canonical JSON (sorted keys, normalized relative
  paths, raw-byte SHA-256) over the detector, runner, SQL, `schema.json`, `requirements.lock` and
  the snapshot builder. Its digest is stored **externally** in `MANIFEST.sha256` (`7fca459f…`) —
  deliberately *not* part of the manifest, so the digest is not self-referential.
- **Fail-closed check:** the digest anchor is **mandatory** — `verify_integrity()` refuses to run on
  a missing, malformed, or mismatched `MANIFEST.sha256`, and `python manifest.py` exits non-zero.
  The detector, the CLI and the data pipeline all call it before doing anything.
- **Per-run provenance:** every run records `schema_version`, `artifact_digest`, and `config_digest`
  (SHA-256 of the dynamic `pools.json`); the dataset also has its own `DATA_MANIFEST.json`.
- **Honest scope:** because `MANIFEST.sha256` lives in this repo, this proves the artifact is
  **integrity-checked against a pinned manifest** (drift and single-file edits are caught) — it is
  **not tamper-proof**: a coordinated edit of code + `MANIFEST.sha256` would pass. Tamper resistance
  needs an external signed anchor (a signed release/tag), which is a future step.

## Status & known limitations

**Fixed / enforced in v3** — these were real gaps:
- **Inputs are validated, fail-closed** — duplicate/gap/unordered dates, non-finite values, negative
  inflow, multiple pools, `n_unpriced_legs ≠ 0`, and windows shorter than 14 days all yield `DATA_ERROR`.
- **Prices are never coerced to zero** — a missing or non-positive price yields `DATA_ERROR`, not "no flow".
- **Integrity is mandatory and fail-closed** — a missing/malformed/mismatched digest is terminal
  (the manifest also covers the data-pipeline code).
- **The data pipeline is covered** — `build_v3_snapshot.py` is fail-closed (roster, known chains,
  non-`DATA_ERROR`, deterministic) and emits a `DATA_MANIFEST.json` with source/snapshot hashes and
  Dune execution IDs.
- **`FLAGGED` no longer contradicts the shown detail** — qualifying run reported separately.
- **Replenishment is net** — new outflows count against recovery.

**Remaining** (stated plainly):
- **Not tamper-proof** without a signed release/tag (see *Provenance & integrity*).
- **Coverage is 4 pools, not 8.**
- **Reproducible scope:** the detector, example, integrity check and snapshot rebuild reproduce from
  this repo; the threshold-selection sweep does not (the labelled dev set is not included here).
  The monitor page's daily CSV reproduces from `onchain/`, public nodes permitting; its weekly
  log does not: the runner that writes it and the Dune extractions behind the rows up to the
  window ending 2026-09-16 are not included.
- **The threshold `0.01` is inherited from v2, not re-validated under v3's net rule** — a v3 dev-set
  re-characterization is pending (the qualitative "no clean separation" finding is expected to hold).
- **The monitor page is a raw-flow view with statuses from v2, not v3** — it shows the daily net
  series and the weekly log as classified by the frozen `detect_monitoring_v2`, the detector the log
  has used since June 2026; it carries no `n_unpriced_legs`, schema or digest. Its daily series is
  computed from chain logs read from public RPC nodes and valued with the daily mean of hourly
  DefiLlama prices, not with the SQL template in this repo; that extraction code is in
  [`onchain/`](onchain/), outside the pinned manifest. Weekly-log rows up to the window ending
  2026-09-16 were computed from Dune at the time.
  The Dune dashboard the page replaces is frozen at 2026-09-16.

## Repository layout

```
detect_monitoring_v3.py       The active detector — fail-closed, versioned (needs pandas)
detect_monitoring_v2.py       Previous version, kept UNMODIFIED as a historical artifact
manifest.py                   Pinned integrity manifest (canonical JSON + mandatory fail-closed verify)
MANIFEST.sha256               External frozen digest (deliberately not part of the manifest)
schema.json                   Input/output contract (schema_version 3.0.1)
requirements.lock             Pinned closure (Python >= 3.11); requirements.txt -> this
pools.json                    8 example pool configs (4 are in the log) — dynamic, not manifested
sql/lp_flow_template.sql      Generic v3 Uniswap V3 daily LP-flow query (emits date, n_unpriced_legs)
examples/                     Runnable example + synthetic CSVs (incl. a DATA_ERROR demo)
tests/                        Adversarial + golden tests (20); offline tests of onchain/ (11)
onchain/                      Mint/Burn extraction from public RPC nodes + daily CSV — NOT in the manifest
scripts/build_v3_snapshot.py  Rebuilds the snapshot fail-closed (in the manifest)
data/                         v3 snapshot + DATA_MANIFEST.json + raw extracts (_v3_extract/, _extraction.json)
docs/claims_and_limits.md     What may / may not be claimed (the discipline, self-contained)
docs/validation_summary.md    Descriptive dev-set validation (prior detector; v3 re-run pending)
docs/onchain_extraction_contract.md  How the on-chain extraction was accepted against the Dune history
```

## License

[MIT](LICENSE). Built and maintained by Ivan Nania.
