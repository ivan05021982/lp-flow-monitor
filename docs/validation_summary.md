# Monitoring Detector v2 — Descriptive Validation Summary

> **Note (v3):** this characterization was produced by the **v2** detector. v3 changes the
> replenishment rule to a **net cumulative** basis (new outflows count against recovery), so the
> exact FLAGGED counts below may differ under v3 — a v3 re-characterization is pending. The
> qualitative conclusion (large LP outflows are **not** stress-specific; no clean event/control
> separation) is expected to hold. v2 is retained in the repo as a historical artifact.

**Detector:** `detect_monitoring_v2` v2.0.0
**Source hash (SHA-256):** `035296e21284f98161415d6040ef3e946726712ec20ee486518f8c29f7c458da`
**Frozen:** 2026-06-05
**Frozen threshold:** `LEAKAGE_PCT_THRESHOLD = 0.01` (relative-only, size-aware)
**Harness:** `methodology/run_monitoring_validation_v2.py`

**Status: DESCRIPTIVE ONLY.** This is not a precision/recall/FPR claim. Event-adjacent
windows have FP=0 by construction; control windows were selected retrospectively —
retrospective calibration, not independent validation. No prediction claim.

---

## What this detector is (and is not)

`detect_monitoring_v2` is the monitoring-mode companion to the frozen event-study
detector `detect_strict_v2` (V1). It removes the event-anchor proximity gate and uses a
size-aware (relative-only) leakage rule, so it can be applied to a pool's recent window
with no known event. It returns a status: QUIET / RESOLVED / PENDING / FLAGGED.

**It is a detector of large, sustained, non-replenished LP outflows.** A FLAGGED status
means such an outflow signature was *observed* in the window. It is NOT a stress
predictor and makes NO claim that a flag corresponds to a catalogued event.

---

## Validation sweep (FLAGGED counts)

| threshold | A9 events (19) | A10 extended (30)¹ | Controls admitted (18) | Controls excluded (12) |
|-----------|----------------|--------------------|------------------------|------------------------|
| 0.005 | 4 | 11 | 3 | 3 |
| **0.010** | **4** | **10** | **3** | **3** |
| 0.020 | 1 | 8 | 3 | 3 |
| 0.030 | 1 | 8 | 3 | 3 |
| 0.050 | 1 | 8 | 3 | 2 |
| 0.100 | 1 | 4 | 2 | 2 |

¹ The harness runs all 30 files in `extended/results/`. The canonical A10 development set
is 26 cases; the 4 Balancer-v2 files are excluded from the dev set (event not
source-verified). At threshold 0.01 one Balancer file flags, so A10-dev would read 9/26.

**Threshold choice (0.01):** best operating point. It maximizes event sensitivity
(4 A9 + 10 A10 = 14 windows flag) at a fixed admitted-control flag rate (3/18). Higher
thresholds collapse A9 sensitivity (4→1) without reducing the control rate.

---

## FLAGGED windows at threshold 0.01

**A9 events (4/19):** `01_usdc_depeg_2023_USDC-WETH`, `03_euler_hack_2023_DAI-USDT`,
`04_euler_hack_2023_USDC-WETH`, `08_curve_hack_2023_DAI-USDT`

**A10 events (10/30):** `balancer_v2_exploit_2025_DAI-USDT`, `bybit_hack_2025_WETH-USDT`,
`bybit_hack_2025_wstETH-WETH`, `mango_markets_2022_DAI-USDT`,
`multichain_collapse_2023_DAI-USDT`, `prisma_finance_hack_2024_wstETH-WETH`,
`radiant_capital_hack_2024_AAVE-WETH`, `radiant_capital_hack_2024_DAI-USDT`,
`radiant_capital_hack_2024_USDC-WETH`, `radiant_capital_hack_2024_WBTC-WETH`

**Admitted controls (3/18):** `ctrl_2023q3_wstETH-WETH`, `ctrl_2024q2_DAI-USDT`,
`ctrl_2024q2_wstETH-WETH`

---

## Honest interpretation (do not overstate)

1. **Every event window that flags is a real, documented stress event.** When the detector
   fires on the event set, it is not firing randomly — all 14 are genuine events with real
   LP-flow movement in the named pool.

2. **There is NO clean separation between events and controls.** A9 events flag at 21%
   (4/19); admitted controls flag at 17% (3/18). The detector does not, on its own,
   distinguish "catalogued stress event" from "quiet window."

3. **Most event windows do NOT flag** (15/19 A9, ~16/26 A10-dev). This is consistent with
   the (unverified) exposure-taxonomy hypothesis: events with indirect exposure do not
   drain the specific monitored pool, so no LP-outflow signature appears.

4. **Two of the three flagging controls are the same ones the frozen detector flagged.**
   `ctrl_2023q3_wstETH-WETH` corresponds to a documented ~$17.9M LP exit (a real movement
   in a "no-catalogued-event" window); `ctrl_2024q2_DAI-USDT` was previously assessed as
   algorithm sensitivity on a thin pool. **This must not be used as vindication** — that
   reasoning is the circularity already disclosed for Analysis 11. `ctrl_2024q2_wstETH-WETH`
   is newly flagged and unverified.

**Bottom line:** the detector reliably identifies large non-replenished LP outflows, but
"large non-replenished LP outflow" is neither necessary nor sufficient for a catalogued
stress event. The honest product framing is a **LP-outflow monitor**, not a stress oracle.

---

## Claim boundaries (frozen)

- No prediction claim. No stress-probability claim. No advance-warning claim.
- No precision, recall, F1, or false-positive-rate claim.
- No event-discrimination claim (the detector does not separate events from controls).
- No causal-attribution claim. No all-Uniswap / cross-protocol / cross-chain claim.
- A FLAGGED status is a descriptive observation of a non-replenished LP-outflow signature.

---

## Severity analysis (follow-on, 2026-06-05)

To test whether a magnitude/severity dimension could separate genuine events from
control/healthy windows, per-qualifying-run metrics were computed for every FLAGGED
window: intensity (|run outflow| / rolling avg daily inflow = "days-equivalent of normal
inflow drained"), run length, absolute outflow, and memory-day count.

**Result: severity does NOT separate events from non-events.** Representative figures
(intensity / outflow):

| Window | Group | intensity | outflow$ | len | mem |
|--------|-------|-----------|----------|-----|-----|
| usdc_depeg USDC-WETH | event | 0.13 | 0.36M | 2 | 3 |
| euler USDC-WETH | event | 0.13 | 0.36M | 2 | 3 |
| bybit WETH-USDT | event | 0.14 | 16.2M | 2 | 1 |
| radiant AAVE-WETH | event | 8.66 | 4.49M | 2 | 2 |
| **ctrl_2023q3 wstETH-WETH** | **control** | **2.74** | **23.0M** | **4** | **5** |
| **ctrl_2024q2 wstETH-WETH** | **control** | **1.69** | **13.8M** | **3** | **3** |
| USDC-WETH_500 (healthy) | pilot ref | 0.47 | 3.60M | 2 | 2 |

Key observations:
1. **The largest and most persistent LP outflows in the entire dataset are in CONTROL
   windows** (ctrl_2023q3 wstETH-WETH: $23M, len 4, memory 5 — the highest persistence of
   any window). Genuine events overlap healthy pilot pools at the low end (0.13–0.47).
2. **No severity threshold separates the groups.** A cut at intensity > 1 would catch a few
   events but also both wstETH controls, and miss most events.
3. **The intensity metric is numerically unstable** on low-activity pools (mango_markets
   DAI-USDT → intensity 1035 because rolling avg inflow ≈ 0). Any production use needs a
   baseline floor.

**Conclusion:** Large/persistent LP outflows are real and detectable, but they are **not
specific to catalogued stress events**. Big LP exits occur for many reasons (rebalancing,
a single large LP departing, yield migration). Severity is useful only as a **magnitude
ranking** for the "LP-outflow monitor" use case — NOT as an event/stress classifier.
This sharpens, and does not relax, the claim boundaries above: **this is an LP-outflow
monitor, not a stress or event detector.**

---

## Reproduction

The frozen detector reproduces its smoke tests and source hash standalone:

```
python detect_monitoring_v2.py        # smoke tests + version/source hash
```

The validation sweep above was produced by an internal harness over a development set of
event/control windows; that labelled dataset is not redistributed in this repository. The
detector itself is self-contained; its source hash is a partial, informational identifier (it covers the orchestration function only — see the repository README).
