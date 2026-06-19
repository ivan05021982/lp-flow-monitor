#!/usr/bin/env python3
"""
Adversarial + golden tests for detect_monitoring_v3.

Runs with pytest, or standalone: `python tests/test_detect_monitoring_v3.py` (exit != 0 on
failure). Encodes the exact defects found in review so they can never silently regress:
duplicate/gap/unordered dates, negative inflow, NaN, incomplete price coverage, FLAGGED-vs-
latest-run incoherence, and the gross-vs-net replenishment bug.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detect_monitoring_v3 import detect_monitoring_v3  # noqa: E402
from manifest import (  # noqa: E402
    REPO_ROOT,
    SCHEMA_VERSION,
    compute_manifest_digest,
    config_digest,
)


def _window(overrides: dict, n: int = 36, base_net: float = 5_000.0,
            base_inflow: float = 1_000_000.0, extra: dict | None = None) -> pd.DataFrame:
    """Build a clean (all-priced) daily window; `overrides[i] = (net, inflow)`.
    `extra` injects extra columns: {colname: {i: value}} (defaults applied elsewhere)."""
    rows = []
    for i in range(n):
        d = date(2026, 1, 1).toordinal() + i
        net, inflow = overrides.get(i, (base_net, base_inflow))
        rows.append({"date": date.fromordinal(d), "net_lp_flow_usd": net,
                     "gross_lp_inflow_usd": inflow, "n_unpriced_legs": 0})
    df = pd.DataFrame(rows)
    if extra:
        for col, mapping in extra.items():
            df[col] = [mapping.get(i, 0) for i in range(n)]
    return df


# ── Adversarial: every one must be DATA_ERROR (fail-closed) ─────────────────────────────────

def test_duplicate_date():
    df = _window({})
    df.loc[5, "date"] = df.loc[4, "date"]
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


def test_gap_dates():
    df = _window({})
    # shift the tail forward by 10 days -> a gap between row 9 and row 10
    df.loc[10:, "date"] = df.loc[10:, "date"] + pd.Timedelta(days=10)
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


def test_unordered_dates():
    df = _window({})
    df.loc[3, "date"], df.loc[4, "date"] = df.loc[4, "date"], df.loc[3, "date"]
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


def test_negative_inflow():
    df = _window({})
    df.loc[7, "gross_lp_inflow_usd"] = -1.0
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


def test_nan_net():
    df = _window({})
    df.loc[7, "net_lp_flow_usd"] = float("nan")
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


def test_incomplete_price_coverage():
    df = _window({})
    df.loc[8, "n_unpriced_legs"] = 2
    r = detect_monitoring_v3(df)
    assert r.status == "DATA_ERROR" and "price coverage" in (r.data_error_reason or "")


def test_multiple_pools():
    df = _window({})
    df["pool_label"] = ["A"] * 18 + ["B"] * (len(df) - 18)
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


def test_missing_required_column():
    df = _window({}).drop(columns=["n_unpriced_legs"])
    assert detect_monitoring_v3(df).status == "DATA_ERROR"


# ── Golden: behaviour locked on canonical inputs ───────────────────────────────────────────

def test_quiet():
    assert detect_monitoring_v3(_window({})).status == "QUIET"


def test_flagged_basic():
    ov = {10: (-100_000.0, 1e6), 11: (-100_000.0, 1e6), 12: (-100_000.0, 1e6),
          14: (-100_000.0, 1e6)}
    r = detect_monitoring_v3(_window(ov))
    assert r.status == "FLAGGED"
    assert r.window_contains_qualifying_run is True
    assert r.qualifying_run_failed_replenishment is True
    assert r.qualifying_run_persistent_memory is True


def test_resolved():
    ov = {10: (-100_000.0, 1e6), 11: (-100_000.0, 1e6),
          12: (300_000.0, 1.3e6), 13: (300_000.0, 1.3e6), 14: (300_000.0, 1.3e6)}
    assert detect_monitoring_v3(_window(ov)).status == "RESOLVED"


def test_pending():
    ov = {34: (-100_000.0, 1e6), 35: (-100_000.0, 1e6)}
    assert detect_monitoring_v3(_window(ov)).status == "PENDING"


def test_net_replenishment_regression():
    """+110k then -1M must NOT count as recovery. v2 returned RESOLVED here (the known bug);
    v3 must return FLAGGED on a net basis."""
    ov = {5: (-100_000.0, 1e6), 6: (-100_000.0, 1e6),
          7: (110_000.0, 1e6), 8: (-1_000_000.0, 1e6)}
    r = detect_monitoring_v3(_window(ov))
    assert r.status == "FLAGGED", r.status
    assert r.qualifying_run_failed_replenishment is True


def test_flagged_latest_run_separation():
    """An earlier run qualifies; the latest run is replenished. Status must be FLAGGED with the
    QUALIFYING run carrying both criteria, while the latest run may show failed=False."""
    ov = {
        5: (-100_000.0, 1e6), 6: (-100_000.0, 1e6),   # run A (qualifies)
        10: (-50_000.0, 1e6),                          # lone leakage day -> memory for run A
        20: (-100_000.0, 1e6), 21: (-100_000.0, 1e6),  # run B (latest)
        22: (300_000.0, 1.3e6), 23: (300_000.0, 1.3e6), 24: (300_000.0, 1.3e6),  # B replenished
    }
    r = detect_monitoring_v3(_window(ov))
    assert r.status == "FLAGGED"
    assert r.window_contains_qualifying_run is True
    assert r.qualifying_run_failed_replenishment is True
    assert r.qualifying_run_persistent_memory is True
    # The latest run is the replenished one — it does NOT carry the qualifying signature.
    assert r.latest_run_failed_replenishment is False
    assert r.qualifying_run_start != r.latest_run_start


# ── Manifest / integrity ───────────────────────────────────────────────────────────────────

def test_manifest_digest_deterministic():
    assert compute_manifest_digest() == compute_manifest_digest()
    assert len(compute_manifest_digest()) == 64


def test_schema_version_consistency():
    with open(os.path.join(REPO_ROOT, "schema.json"), encoding="utf-8") as fh:
        sj = json.load(fh)
    assert sj["schema_version"] == SCHEMA_VERSION


def test_config_digest_behaviour():
    assert config_digest(None) is None
    d = config_digest(os.path.join(REPO_ROOT, "pools.json"))
    assert d is not None and len(d) == 64


# ── v3.0.1 validation hardening ─────────────────────────────────────────────────────────────

def test_n_unpriced_negative_is_data_error():
    df = _window({})
    df.loc[6, "n_unpriced_legs"] = -1
    r = detect_monitoring_v3(df)
    assert r.status == "DATA_ERROR" and "n_unpriced_legs must equal 0" in (r.data_error_reason or "")


def test_pct_must_be_finite_positive():
    base = _window({})
    for bad in (float("nan"), float("inf"), 0, -0.5, "x", None):
        try:
            detect_monitoring_v3(base, pct=bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"pct={bad!r} should have raised")


def test_insufficient_history_is_data_error():
    df = _window({}, n=5)  # valid data but below MIN_WINDOW_DAYS
    r = detect_monitoring_v3(df)
    assert r.status == "DATA_ERROR" and "insufficient history" in (r.data_error_reason or "")


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed.append(f"{t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append(f"{t.__name__}: UNEXPECTED {type(e).__name__}: {e}")
    if failed:
        print(f"FAILED {len(failed)}/{len(tests)}:")
        for f in failed:
            print("  -", f)
        return 1
    print(f"PASS: {len(tests)}/{len(tests)} v3 tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
