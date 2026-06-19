#!/usr/bin/env python3
"""
detect_monitoring_v2 — Monitoring-mode LP-flow stress detector (V2 artifact).

PURPOSE
-------
Forward-looking companion to the frozen event-study detector detect_strict_v2
(V1). It answers "does this pool's RECENT window show a stress signature?"
WITHOUT requiring a known event anchor date. Intended to power a weekly
prospective monitoring log (one status per pool per run).

RELATIONSHIP TO detect_strict_v2 (V1, FROZEN — NOT MODIFIED BY THIS FILE)
------------------------------------------------------------------------
Shared primitive: leakage-day -> leakage-run -> failed-replenishment ->
persistent-memory. Two deliberate, documented differences:

  1. NO proximity/anchor gate.
     detect_strict_v2 only fires within +/-PROXIMITY_DAYS of a catalogued
     event. Monitoring has no known event, so that gate is removed. Removing it
     loosens selectivity, which is compensated by (2).

  2. Size-aware (relative-only) leakage rule.
     detect_strict_v2 flags a day as leakage when net < -$1,000 (absolute floor)
     OR a relative rule fires. On very large pools the -$1,000 floor is almost
     always true (a pool moving $20-70M/day "leaks" on any day it loses > $1,000
     net), making the per-day flag near-noise. Monitoring drops the absolute
     floor and uses the RELATIVE rule only, so the leakage flag scales with each
     pool's own typical inflow.

STATUS TAXONOMY (the monitoring output)
---------------------------------------
  QUIET    — no leakage runs in the window.
  RESOLVED — leakage run(s) occurred but all assessable runs replenished or
             lacked persistent memory (no qualifying signature).
  PENDING  — a leakage run is too close to the window's end to assess
             replenishment/memory yet (genuinely undetermined — reported
             honestly, never as a positive).
  FLAGGED  — at least one fully-assessable run failed replenishment AND showed
             persistent memory (the stress signature was observed).

CLAIM DISCIPLINE
----------------
Descriptive only. NO prediction, NO stress-probability, NO advance-warning,
NO causal attribution. FLAGGED means the (run + failed-replenishment + memory)
signature was OBSERVED in the window — nothing more. Thresholds are selected by
descriptive validation on the development set and frozen with a source hash.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, asdict
from datetime import date
from statistics import mean
from typing import Optional

import pandas as pd


# ── Thresholds ────────────────────────────────────────────────────────────────
# PROVISIONAL default below; the frozen value is selected by
# run_monitoring_validation_v2.py and recorded in
# methodology/results/monitoring_validation_v2_summary.md before freeze.

# Frozen 2026-06-05 after descriptive validation on the development set.
# Threshold 0.01 selected as the best operating point: maximum event sensitivity
# (14 A9+A10 windows flag) at a fixed admitted-control flag rate (3/18). Higher
# thresholds lose event sensitivity without reducing controls. See
# methodology/results/monitoring_validation_v2_summary.md.
LEAKAGE_PCT_THRESHOLD: float = 0.01   # |net| / rolling_7d_avg_inflow > 1% → leakage day
MIN_LEAKAGE_RUN: int = 2              # ≥ 2 consecutive leakage days = a run
REPLENISHMENT_WINDOW_DAYS: int = 3    # days after run end to assess replenishment
REPLENISHMENT_RATIO: float = 0.50     # recovery < 50% of |outflow| → replenishment failed
MEMORY_WINDOW_DAYS: int = 7           # days after run end to check for persistent memory

VERSION: str = "2.0.0"
FREEZE_DATE: Optional[str] = "2026-06-05"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _rolling_7d_avg_inflow(inflows: list[float], i: int) -> float:
    """Mean of positive gross inflows in the 7-day window ending before day i.
    Returns 0.0 when no positive inflow exists in the lookback (relative rule
    is then skipped for that day). Identical convention to detect_strict_v2."""
    start = max(0, i - 7)
    positives = [v for v in inflows[start:i] if v > 0]
    return mean(positives) if positives else 0.0


def _is_leakage_day(net: float, inflows: list[float], i: int, pct: float) -> bool:
    """Relative-only, size-aware leakage rule:
    net < 0 AND |net| / rolling_7d_avg_inflow > pct.
    Skipped (False) when the rolling baseline is 0 (no inflow history)."""
    if net < 0:
        baseline = _rolling_7d_avg_inflow(inflows, i)
        if baseline > 0 and abs(net) / baseline > pct:
            return True
    return False


def _leakage_flags(nets: list[float], inflows: list[float], pct: float) -> list[bool]:
    return [_is_leakage_day(nets[i], inflows, i, pct) for i in range(len(nets))]


def _find_runs(flags: list[bool], nets: list[float]) -> list[dict]:
    """All runs of ≥ MIN_LEAKAGE_RUN consecutive leakage days.
    Returns dicts: {start, end (inclusive), outflow (sum of nets in run, ≤ 0)}."""
    n = len(flags)
    runs: list[dict] = []
    i = 0
    while i < n:
        if flags[i]:
            j = i
            while j < n and flags[j]:
                j += 1
            if j - i >= MIN_LEAKAGE_RUN:
                runs.append({"start": i, "end": j - 1, "outflow": sum(nets[i:j])})
            i = j
        else:
            i += 1
    return runs


def _assess_replenishment(nets: list[float], run: dict, n: int) -> tuple[Optional[bool], bool]:
    """Returns (failed, assessable).
    assessable = at least REPLENISHMENT_WINDOW_DAYS days exist after the run end.
    failed = positive flows in that window < REPLENISHMENT_RATIO × |outflow|.
    When not assessable, failed = None (honestly undetermined)."""
    outflow_abs = abs(run["outflow"])
    days_after = n - 1 - run["end"]
    assessable = days_after >= REPLENISHMENT_WINDOW_DAYS
    if outflow_abs == 0:
        return (False, assessable)
    if not assessable:
        return (None, False)
    post = nets[run["end"] + 1: run["end"] + 1 + REPLENISHMENT_WINDOW_DAYS]
    positive = sum(v for v in post if v > 0)
    return (positive < REPLENISHMENT_RATIO * outflow_abs, True)


def _assess_memory(flags: list[bool], run: dict, n: int) -> tuple[Optional[bool], bool]:
    """Returns (present, assessable).
    present = True if a leakage day appears in the next MEMORY_WINDOW_DAYS days.
    If found, definitive True. If none found but the window is truncated
    (< MEMORY_WINDOW_DAYS days available), present = None (inconclusive)."""
    post_start = run["end"] + 1
    available = n - post_start
    window = flags[post_start: post_start + MEMORY_WINDOW_DAYS]
    if any(window):
        return (True, True)
    if available >= MEMORY_WINDOW_DAYS:
        return (False, True)
    return (None, False)


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class MonitoringResult:
    status: str                       # QUIET | RESOLVED | PENDING | FLAGGED
    n_leakage_runs: int
    any_qualifying_run: bool
    latest_run_start: Optional[str]   # ISO date of most recent run onset
    latest_run_end: Optional[str]
    latest_run_outflow_usd: Optional[float]
    latest_run_failed_replenishment: Optional[bool]
    latest_run_persistent_memory: Optional[bool]
    pct_threshold: float

    def to_dict(self) -> dict:
        return asdict(self)


# ── Primary detector ──────────────────────────────────────────────────────────

def detect_monitoring_v2(df: pd.DataFrame, pct: float = LEAKAGE_PCT_THRESHOLD) -> MonitoringResult:
    """
    Evaluate a pool's recent LP-flow window and return a monitoring status.

    Parameters
    ----------
    df : pd.DataFrame
        Daily LP-flow for ONE pool. Required columns:
            date                — date / parseable datetime, one row per day, ascending.
            net_lp_flow_usd     — float, daily net LP flow (negative = net outflow).
            gross_lp_inflow_usd — float, daily gross LP inflow (≥ 0).
    pct : float
        Relative leakage threshold (default = frozen LEAKAGE_PCT_THRESHOLD).

    Returns
    -------
    MonitoringResult
    """
    required = {"date", "net_lp_flow_usd", "gross_lp_inflow_usd"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"detect_monitoring_v2: missing required columns: {missing}")

    df = df.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(df["date"]).dt.date.tolist()
    nets = df["net_lp_flow_usd"].tolist()
    inflows = df["gross_lp_inflow_usd"].tolist()
    n = len(nets)

    flags = _leakage_flags(nets, inflows, pct)
    runs = _find_runs(flags, nets)

    if not runs:
        return MonitoringResult("QUIET", 0, False, None, None, None, None, None, pct)

    any_qualifying = False
    any_pending = False
    for run in runs:
        failed, repl_assessable = _assess_replenishment(nets, run, n)
        mem, mem_assessable = _assess_memory(flags, run, n)
        if repl_assessable and mem_assessable:
            if failed is True and mem is True:
                any_qualifying = True
        else:
            # not fully assessable, and not yet disqualified → genuinely pending
            if (failed is None or failed is True) and (mem is None or mem is True):
                any_pending = True

    latest = runs[-1]
    l_failed, _ = _assess_replenishment(nets, latest, n)
    l_mem, _ = _assess_memory(flags, latest, n)

    if any_qualifying:
        status = "FLAGGED"
    elif any_pending:
        status = "PENDING"
    else:
        status = "RESOLVED"

    return MonitoringResult(
        status=status,
        n_leakage_runs=len(runs),
        any_qualifying_run=any_qualifying,
        latest_run_start=dates[latest["start"]].isoformat(),
        latest_run_end=dates[latest["end"]].isoformat(),
        latest_run_outflow_usd=round(latest["outflow"], 2),
        latest_run_failed_replenishment=l_failed,
        latest_run_persistent_memory=l_mem,
        pct_threshold=pct,
    )


# ── Version / audit ───────────────────────────────────────────────────────────

def get_version_info() -> dict:
    source = inspect.getsource(detect_monitoring_v2)
    return {
        "function": "detect_monitoring_v2",
        "version": VERSION,
        "freeze_date": FREEZE_DATE,
        "source_hash_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "thresholds": {
            "LEAKAGE_PCT_THRESHOLD": LEAKAGE_PCT_THRESHOLD,
            "MIN_LEAKAGE_RUN": MIN_LEAKAGE_RUN,
            "REPLENISHMENT_WINDOW_DAYS": REPLENISHMENT_WINDOW_DAYS,
            "REPLENISHMENT_RATIO": REPLENISHMENT_RATIO,
            "MEMORY_WINDOW_DAYS": MEMORY_WINDOW_DAYS,
        },
    }


# ── Smoke test ────────────────────────────────────────────────────────────────

def _window(overrides: dict, n: int = 30, base_net: float = 5_000.0,
            base_inflow: float = 1_000_000.0) -> pd.DataFrame:
    rows = []
    for i in range(n):
        d = date(2026, 1, 1).toordinal() + i
        net, inflow = overrides.get(i, (base_net, base_inflow))
        rows.append({"date": date.fromordinal(d), "net_lp_flow_usd": net,
                     "gross_lp_inflow_usd": inflow})
    return pd.DataFrame(rows)


def _run_smoke_test() -> None:
    import sys
    fails = []

    # QUIET — all positive flows.
    r = detect_monitoring_v2(_window({}))
    if r.status != "QUIET":
        fails.append(f"QUIET: got {r.status}")

    # FLAGGED — run of 3 big outflows mid-window, no replenishment, memory present.
    ov = {
        10: (-100_000.0, 1_000_000.0),
        11: (-100_000.0, 1_000_000.0),
        12: (-100_000.0, 1_000_000.0),
        14: (-100_000.0, 1_000_000.0),  # memory day (within 7), also keeps replenishment low
    }
    r = detect_monitoring_v2(_window(ov))
    if r.status != "FLAGGED":
        fails.append(f"FLAGGED: got {r.status} ({r})")

    # RESOLVED — run then strong replenishment, no later leakage.
    ov = {
        10: (-100_000.0, 1_000_000.0),
        11: (-100_000.0, 1_000_000.0),
        12: (300_000.0, 1_300_000.0),
        13: (300_000.0, 1_300_000.0),
        14: (300_000.0, 1_300_000.0),
    }
    r = detect_monitoring_v2(_window(ov))
    if r.status != "RESOLVED":
        fails.append(f"RESOLVED: got {r.status} ({r})")

    # PENDING — run at the very end of the window (cannot assess replenishment).
    ov = {
        28: (-100_000.0, 1_000_000.0),
        29: (-100_000.0, 1_000_000.0),
    }
    r = detect_monitoring_v2(_window(ov))
    if r.status != "PENDING":
        fails.append(f"PENDING: got {r.status} ({r})")

    if fails:
        print("SMOKE TEST FAILED:")
        for f in fails:
            print(f"  FAIL: {f}")
        sys.exit(1)
    print("PASS: All monitoring smoke tests passed.")
    print()
    print(json.dumps(get_version_info(), indent=2))


if __name__ == "__main__":
    _run_smoke_test()
