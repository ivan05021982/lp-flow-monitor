#!/usr/bin/env python3
"""
run_detector_on_csv.py — run the fail-closed v3 LP-flow monitor on ANY pool's daily flow.

Usage:
    python examples/run_detector_on_csv.py <daily_lp_flow.csv> [--label "My Pool"] [--config pools.json]

Required CSV columns (one row per calendar day, ascending, no gaps, no duplicates):
    date (or day)         ISO date
    net_lp_flow_usd       float (negative = net outflow)
    gross_lp_inflow_usd   float (>= 0)
    n_unpriced_legs       int   Mint/Burn legs with amount != 0 but a missing price (from the
                                v3 SQL template). Any day > 0 => DATA_ERROR (fail-closed).

Each run records provenance: schema_version, artifact_digest (the pinned manifest digest), and
config_digest (SHA-256 of the pools.json used). Integrity is checked against the pinned manifest
(not tamper-proof; see manifest.py).

Exit codes: 0 classified (QUIET/RESOLVED/PENDING/FLAGGED); 2 DATA_ERROR (input rejected);
1 unexpected error.

Descriptive only. FLAGGED = a large, sustained, non-replenished LP outflow was OBSERVED — not a
prediction or stress claim. See docs/claims_and_limits.md and docs/validation_summary.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from detect_monitoring_v3 import detect_monitoring_v3, get_version_info, verify_integrity  # noqa: E402
from manifest import SCHEMA_VERSION, compute_manifest_digest, config_digest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the fail-closed v3 LP-flow monitor on a pool's daily-flow CSV."
    )
    ap.add_argument("csv", help="CSV with date, net_lp_flow_usd, gross_lp_inflow_usd, n_unpriced_legs")
    ap.add_argument("--label", default="(unnamed pool)", help="Human label for the pool")
    ap.add_argument("--config", default=None,
                    help="pools.json to record a config_digest for (default: repo pools.json)")
    args = ap.parse_args()

    verify_integrity()  # fail-closed once the manifest digest is frozen

    df = pd.read_csv(args.csv)
    if "date" not in df.columns and "day" in df.columns:
        df = df.rename(columns={"day": "date"})

    result = detect_monitoring_v3(df)

    window_start = window_end = None
    if "date" in df.columns:
        d = pd.to_datetime(df["date"], errors="coerce")
        if d.notna().any():
            window_start, window_end = str(d.min().date()), str(d.max().date())

    config_path = args.config or os.path.join(REPO_ROOT, "pools.json")
    out = {
        "pool": args.label,
        "rows": int(len(df)),
        "window_start": window_start,
        "window_end": window_end,
        "provenance": {
            "schema_version": SCHEMA_VERSION,
            "artifact_digest": compute_manifest_digest(),
            "config_digest": config_digest(config_path),
        },
        "result": result.to_dict(),
        "detector": get_version_info(),
    }
    print(json.dumps(out, indent=2, default=str))

    return 2 if result.status == "DATA_ERROR" else 0


if __name__ == "__main__":
    sys.exit(main())
