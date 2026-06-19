#!/usr/bin/env python3
"""
detect_monitoring_v3 — Monitoring-mode LP-flow detector, fail-closed (V3 artifact).

Supersedes detect_monitoring_v2, which REMAINS in this repository unmodified as a historical
artifact. V3 is a deliberate BREAKING change. It adds:

  1. Strict, fail-closed INPUT VALIDATION (dates, finiteness, sign, single pool).
  2. A DATA_ERROR status and explicit PRICE-COVERAGE enforcement (no silent zero).
  3. SEPARATION of the qualifying run (the evidence for FLAGGED) from the latest run
     (the current tail) — they can no longer contradict each other.
  4. A NET-CUMULATIVE replenishment rule (new outflows count against recovery).

Integrity is enforced by a pinned manifest (manifest.py + external MANIFEST.sha256) covering the
detector, runner, SQL, schema, dependency lock and the snapshot builder, so changing a threshold,
a helper, or any pipeline file changes the digest. The check is fail-closed: a missing, malformed,
or mismatched digest is a terminal error (see manifest.verify_integrity).

STATUS TAXONOMY
  QUIET    — no leakage runs in the window.
  RESOLVED — leakage run(s) occurred but none qualified (replenished on a NET basis and/or
             no persistent memory).
  PENDING  — a leakage run is too recent to assess yet (honestly undetermined).
  FLAGGED  — at least one fully-assessable run failed NET replenishment AND showed memory.
  DATA_ERROR — input failed validation or price coverage was incomplete. TERMINAL and
             fail-closed: when it fires, NO QUIET/RESOLVED/PENDING/FLAGGED is emitted.
             Distinct from PENDING: PENDING = good data, too recent; DATA_ERROR = data not
             trustworthy, classification refused.

INPUT CONTRACT (any violation -> DATA_ERROR, never a silent classification)
  Required columns: date, net_lp_flow_usd, gross_lp_inflow_usd, n_unpriced_legs.
  - date: unique, ascending as given, strictly consecutive calendar days (no gaps/dupes).
  - net_lp_flow_usd, gross_lp_inflow_usd, n_unpriced_legs: finite (no NaN/inf).
  - gross_lp_inflow_usd >= 0.
  - n_unpriced_legs == 0 for EVERY day (exactly zero; any non-zero value, including negative,
    is rejected). A Mint/Burn leg with a non-zero amount but a missing/invalid price makes the
    window untrustworthy; the SQL emits this count, never COALESCE-to-0.
  - single pool: if a pool-identifying column is present, it must hold exactly one value.
  - minimum history: at least MIN_WINDOW_DAYS rows (shorter windows lack a meaningful baseline).
  The public `pct` argument must be a finite, positive number (else ValueError).

BASELINE & WARM-UP (exact semantics, descriptive — unchanged in v3)
  The leakage rule for day i compares |net_i| to the mean of the POSITIVE gross-inflow days in
  the up-to-7 calendar days immediately before i (a partial, left-truncated window near the
  start). If that lookback has no positive-inflow day the baseline is 0 and the relative rule is
  skipped for day i (it cannot flag). The earliest ~7 days therefore act as warm-up;
  MIN_WINDOW_DAYS guarantees enough history for the rule to be meaningful.

CLAIM DISCIPLINE (unchanged from v2)
  Descriptive only. NO prediction, NO stress-probability, NO advance-warning, NO causal
  attribution. FLAGGED means the (run + failed NET replenishment + memory) signature was
  OBSERVED in the window — nothing more.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date
from statistics import mean
from typing import Optional

import numpy as np
import pandas as pd

from manifest import (
    SCHEMA_VERSION,
    compute_manifest_digest,
    read_expected_digest,
    verify_integrity as _verify_manifest,
)

# ── Thresholds (part of the pinned manifest — changing one changes the digest) ─────────────
LEAKAGE_PCT_THRESHOLD: float = 0.01   # |net| / rolling_7d_avg_inflow > 1% → leakage day
MIN_LEAKAGE_RUN: int = 2              # >= 2 consecutive leakage days = a run
REPLENISHMENT_WINDOW_DAYS: int = 3    # days after run end to assess replenishment
REPLENISHMENT_RATIO: float = 0.50     # NET recovery < 50% of |outflow| → replenishment failed
MEMORY_WINDOW_DAYS: int = 7           # days after run end to check for persistent memory
MIN_WINDOW_DAYS: int = 14             # minimum history; shorter windows lack a meaningful baseline

# Threshold provenance: 0.01 is INHERITED from the v2 descriptive characterization and has NOT
# been re-validated under v3's net-replenishment rule (a v3 re-characterization is pending).
VERSION: str = "3.0.3"
FREEZE_DATE: Optional[str] = "2026-06-19"   # frozen; integrity via manifest.py + external MANIFEST.sha256

REQUIRED_COLUMNS = ("date", "net_lp_flow_usd", "gross_lp_inflow_usd", "n_unpriced_legs")
POOL_COLUMNS = ("pool_address", "pool_label", "pool")


# ── Input validation (fail-closed) ────────────────────────────────────────────────────────

def validate_input(df: pd.DataFrame) -> list[str]:
    """Return a list of contract violations. Empty list = input is trustworthy.
    Structural problems short-circuit (we cannot meaningfully check the rest)."""
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return [f"missing required columns: {missing}"]
    if len(df) == 0:
        return ["empty input (no rows)"]

    v: list[str] = []

    # Single pool.
    for col in POOL_COLUMNS:
        if col in df.columns and df[col].nunique(dropna=False) > 1:
            v.append(f"multiple pools in column '{col}' (exactly one expected)")

    # Dates: parseable, unique, ascending-as-given, strictly consecutive daily.
    dates = pd.to_datetime(df["date"], errors="coerce")
    if dates.isna().any():
        v.append("unparseable date(s)")
    else:
        d = dates.dt.normalize()
        if d.duplicated().any():
            v.append("duplicate date(s)")
        if not d.is_monotonic_increasing:
            v.append("dates not in ascending order")
        diffs = d.sort_values().diff().dropna()
        if len(diffs) and not (diffs == pd.Timedelta(days=1)).all():
            v.append("dates not strictly consecutive daily (gap or irregular frequency)")

    # Finite numerics.
    for col in ("net_lp_flow_usd", "gross_lp_inflow_usd", "n_unpriced_legs"):
        vals = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype="float64")
        if not np.isfinite(vals).all():
            v.append(f"non-finite or non-numeric values in '{col}'")

    # Sign + price coverage (only if the columns were numeric).
    inflow = pd.to_numeric(df["gross_lp_inflow_usd"], errors="coerce")
    if inflow.notna().all() and (inflow < 0).any():
        v.append("negative gross_lp_inflow_usd (gross inflow must be >= 0)")

    unpriced = pd.to_numeric(df["n_unpriced_legs"], errors="coerce")
    if unpriced.notna().all() and (unpriced != 0).any():
        n_bad = int((unpriced != 0).sum())
        v.append(f"price coverage not exactly zero on {n_bad} day(s) (n_unpriced_legs must equal 0)")

    if len(df) < MIN_WINDOW_DAYS:
        v.append(f"insufficient history: {len(df)} day(s) < MIN_WINDOW_DAYS = {MIN_WINDOW_DAYS}")

    return v


# ── Core primitive (helpers are inside the hashed artifact) ────────────────────────────────

def _rolling_7d_avg_inflow(inflows: list[float], i: int) -> float:
    start = max(0, i - 7)
    positives = [x for x in inflows[start:i] if x > 0]
    return mean(positives) if positives else 0.0


def _is_leakage_day(net: float, inflows: list[float], i: int, pct: float) -> bool:
    if net < 0:
        baseline = _rolling_7d_avg_inflow(inflows, i)
        if baseline > 0 and abs(net) / baseline > pct:
            return True
    return False


def _leakage_flags(nets: list[float], inflows: list[float], pct: float) -> list[bool]:
    return [_is_leakage_day(nets[i], inflows, i, pct) for i in range(len(nets))]


def _find_runs(flags: list[bool], nets: list[float]) -> list[dict]:
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


def _assess_replenishment_net(nets: list[float], run: dict, n: int) -> tuple[Optional[bool], bool]:
    """NET replenishment: recovery = NET cumulative flow over the post-run window (new outflows
    count against it). Returns (failed, assessable). Not assessable -> (None, False)."""
    outflow_abs = abs(run["outflow"])
    days_after = n - 1 - run["end"]
    assessable = days_after >= REPLENISHMENT_WINDOW_DAYS
    if outflow_abs == 0:
        return (False, assessable)
    if not assessable:
        return (None, False)
    post = nets[run["end"] + 1: run["end"] + 1 + REPLENISHMENT_WINDOW_DAYS]
    recovered_net = sum(post)
    return (recovered_net < REPLENISHMENT_RATIO * outflow_abs, True)


def _assess_memory(flags: list[bool], run: dict, n: int) -> tuple[Optional[bool], bool]:
    post_start = run["end"] + 1
    available = n - post_start
    window = flags[post_start: post_start + MEMORY_WINDOW_DAYS]
    if any(window):
        return (True, True)
    if available >= MEMORY_WINDOW_DAYS:
        return (False, True)
    return (None, False)


# ── Result type (qualifying run and latest run are SEPARATE) ───────────────────────────────

@dataclass
class MonitoringResultV3:
    status: str                                  # QUIET|RESOLVED|PENDING|FLAGGED|DATA_ERROR
    data_error_reason: Optional[str]
    n_leakage_runs: int
    window_contains_qualifying_run: bool
    # Evidence for FLAGGED (the qualifying run); None unless FLAGGED.
    qualifying_run_start: Optional[str]
    qualifying_run_end: Optional[str]
    qualifying_run_outflow_usd: Optional[float]
    qualifying_run_failed_replenishment: Optional[bool]
    qualifying_run_persistent_memory: Optional[bool]
    # The most recent run (current tail) — context only, may NOT be the qualifying one.
    latest_run_start: Optional[str]
    latest_run_end: Optional[str]
    latest_run_outflow_usd: Optional[float]
    latest_run_failed_replenishment: Optional[bool]
    latest_run_persistent_memory: Optional[bool]
    pct_threshold: float
    detector_version: str

    def to_dict(self) -> dict:
        return asdict(self)


def _data_error(reason: str, pct: float) -> MonitoringResultV3:
    return MonitoringResultV3(
        status="DATA_ERROR", data_error_reason=reason, n_leakage_runs=0,
        window_contains_qualifying_run=False,
        qualifying_run_start=None, qualifying_run_end=None, qualifying_run_outflow_usd=None,
        qualifying_run_failed_replenishment=None, qualifying_run_persistent_memory=None,
        latest_run_start=None, latest_run_end=None, latest_run_outflow_usd=None,
        latest_run_failed_replenishment=None, latest_run_persistent_memory=None,
        pct_threshold=pct, detector_version=VERSION,
    )


# ── Primary detector ──────────────────────────────────────────────────────────────────────

def detect_monitoring_v3(df: pd.DataFrame, pct: float = LEAKAGE_PCT_THRESHOLD) -> MonitoringResultV3:
    """Evaluate one pool's recent daily LP-flow window. Fail-closed: invalid input or
    incomplete price coverage returns DATA_ERROR, never a silent classification."""
    try:
        pct_ok = bool(np.isfinite(pct)) and pct > 0
    except TypeError:
        pct_ok = False
    if not pct_ok:
        raise ValueError(f"pct must be a finite positive number, got {pct!r}")

    violations = validate_input(df)
    if violations:
        return _data_error("; ".join(violations), pct)

    df = df.sort_values("date").reset_index(drop=True)
    dates = pd.to_datetime(df["date"]).dt.date.tolist()
    nets = [float(x) for x in df["net_lp_flow_usd"].tolist()]
    inflows = [float(x) for x in df["gross_lp_inflow_usd"].tolist()]
    n = len(nets)

    flags = _leakage_flags(nets, inflows, pct)
    runs = _find_runs(flags, nets)

    def detail(run: Optional[dict]) -> dict:
        if run is None:
            return dict(start=None, end=None, outflow=None, failed=None, mem=None)
        failed, _ = _assess_replenishment_net(nets, run, n)
        mem, _ = _assess_memory(flags, run, n)
        return dict(start=dates[run["start"]].isoformat(), end=dates[run["end"]].isoformat(),
                    outflow=round(run["outflow"], 2), failed=failed, mem=mem)

    if not runs:
        return MonitoringResultV3(
            status="QUIET", data_error_reason=None, n_leakage_runs=0,
            window_contains_qualifying_run=False,
            qualifying_run_start=None, qualifying_run_end=None, qualifying_run_outflow_usd=None,
            qualifying_run_failed_replenishment=None, qualifying_run_persistent_memory=None,
            latest_run_start=None, latest_run_end=None, latest_run_outflow_usd=None,
            latest_run_failed_replenishment=None, latest_run_persistent_memory=None,
            pct_threshold=pct, detector_version=VERSION,
        )

    qualifying_runs: list[dict] = []
    any_pending = False
    for run in runs:
        failed, repl_ok = _assess_replenishment_net(nets, run, n)
        mem, mem_ok = _assess_memory(flags, run, n)
        if repl_ok and mem_ok:
            if failed is True and mem is True:
                qualifying_runs.append(run)
        else:
            if (failed is None or failed is True) and (mem is None or mem is True):
                any_pending = True

    latest_d = detail(runs[-1])

    if qualifying_runs:
        status = "FLAGGED"
        q = detail(qualifying_runs[-1])   # most recent qualifying run = the evidence
    elif any_pending:
        status, q = "PENDING", detail(None)
    else:
        status, q = "RESOLVED", detail(None)

    return MonitoringResultV3(
        status=status, data_error_reason=None, n_leakage_runs=len(runs),
        window_contains_qualifying_run=bool(qualifying_runs),
        qualifying_run_start=q["start"], qualifying_run_end=q["end"],
        qualifying_run_outflow_usd=q["outflow"],
        qualifying_run_failed_replenishment=q["failed"], qualifying_run_persistent_memory=q["mem"],
        latest_run_start=latest_d["start"], latest_run_end=latest_d["end"],
        latest_run_outflow_usd=latest_d["outflow"],
        latest_run_failed_replenishment=latest_d["failed"],
        latest_run_persistent_memory=latest_d["mem"],
        pct_threshold=pct, detector_version=VERSION,
    )


# ── Integrity: hash the WHOLE file, verify against an expected digest (fail-closed) ─────────

def get_version_info() -> dict:
    expected = read_expected_digest()
    return {
        "detector": "detect_monitoring_v3",
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "freeze_date": FREEZE_DATE,
        "artifact_digest": compute_manifest_digest(),   # canonical PINNED-MANIFEST digest (multi-file)
        "expected_artifact_digest": expected,           # from external MANIFEST.sha256
        "integrity_enforced": expected is not None,
        "thresholds": {
            "LEAKAGE_PCT_THRESHOLD": LEAKAGE_PCT_THRESHOLD,
            "MIN_LEAKAGE_RUN": MIN_LEAKAGE_RUN,
            "REPLENISHMENT_WINDOW_DAYS": REPLENISHMENT_WINDOW_DAYS,
            "REPLENISHMENT_RATIO": REPLENISHMENT_RATIO,
            "MEMORY_WINDOW_DAYS": MEMORY_WINDOW_DAYS,
        },
    }


def verify_integrity() -> None:
    """Fail-closed integrity check, delegated to the pinned manifest (manifest.py + external
    MANIFEST.sha256). The digest anchor is MANDATORY: a missing, malformed, or mismatched
    MANIFEST.sha256 raises (terminal). Integrity-checked against a pinned manifest — NOT
    tamper-proof (a coordinated edit of code + MANIFEST.sha256 would pass; that needs an external
    signed anchor)."""
    _verify_manifest()


# ── Smoke test ────────────────────────────────────────────────────────────────────────────

def _window(overrides: dict, n: int = 30, base_net: float = 5_000.0,
            base_inflow: float = 1_000_000.0) -> pd.DataFrame:
    rows = []
    for i in range(n):
        d = date(2026, 1, 1).toordinal() + i
        net, inflow = overrides.get(i, (base_net, base_inflow))
        rows.append({"date": date.fromordinal(d), "net_lp_flow_usd": net,
                     "gross_lp_inflow_usd": inflow, "n_unpriced_legs": 0})
    return pd.DataFrame(rows)


def _run_smoke_test() -> None:
    import sys
    fails = []

    if detect_monitoring_v3(_window({})).status != "QUIET":
        fails.append("QUIET")

    flagged = {10: (-100_000.0, 1_000_000.0), 11: (-100_000.0, 1_000_000.0),
               12: (-100_000.0, 1_000_000.0), 14: (-100_000.0, 1_000_000.0)}
    if detect_monitoring_v3(_window(flagged)).status != "FLAGGED":
        fails.append("FLAGGED")

    resolved = {10: (-100_000.0, 1_000_000.0), 11: (-100_000.0, 1_000_000.0),
                12: (300_000.0, 1_300_000.0), 13: (300_000.0, 1_300_000.0),
                14: (300_000.0, 1_300_000.0)}
    if detect_monitoring_v3(_window(resolved)).status != "RESOLVED":
        fails.append("RESOLVED")

    pending = {28: (-100_000.0, 1_000_000.0), 29: (-100_000.0, 1_000_000.0)}
    if detect_monitoring_v3(_window(pending)).status != "PENDING":
        fails.append("PENDING")

    # DATA_ERROR: missing price coverage column value.
    bad = _window({}); bad.loc[5, "n_unpriced_legs"] = 2
    if detect_monitoring_v3(bad).status != "DATA_ERROR":
        fails.append("DATA_ERROR(price)")

    if fails:
        print("SMOKE TEST FAILED:", fails)
        sys.exit(1)
    print("PASS: All v3 smoke tests passed.")
    print(json.dumps(get_version_info(), indent=2))


if __name__ == "__main__":
    verify_integrity()
    _run_smoke_test()
