#!/usr/bin/env python3
"""Build the v3 prospective-log snapshot + data manifest from extracted daily-flow CSVs.

FAIL-CLOSED. Requires exactly the expected 4-pool roster, known chains, and a clean (non
DATA_ERROR) classification for every pool; exits non-zero otherwise. Reads provenance (Dune
query/execution IDs, window, extraction timestamp) from data/_v3_extract/_extraction.json,
hashes every source CSV and the snapshot, and writes data/DATA_MANIFEST.json.

run_utc is the deterministic extraction timestamp from the extraction record (not wall-clock),
so re-running this builder on the same extracts reproduces an identical snapshot. Integrity-checked:
refuses to run unless the pinned code manifest verifies.
"""
import csv
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402
from detect_monitoring_v3 import detect_monitoring_v3  # noqa: E402
from manifest import (  # noqa: E402
    SCHEMA_VERSION,
    compute_manifest_digest,
    config_digest,
    verify_integrity,
)

EXPECTED_DATASETS = {
    "usdc_weth_500_eth", "usdc_weth_500_arb", "usdc_weth_500_base", "usdc_weth_500_op",
}
KNOWN_CHAINS = {"ethereum", "arbitrum", "base", "optimism"}

EXTRACT_DIR = os.path.join(ROOT, "data", "_v3_extract")
SNAPSHOT = os.path.join(ROOT, "data", "monitoring_log_snapshot_v3_2026-06-18.csv")
DATA_MANIFEST = os.path.join(ROOT, "data", "DATA_MANIFEST.json")


def die(msg: str) -> None:
    print("BUILD FAILED:", msg)
    sys.exit(1)


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main() -> None:
    verify_integrity()  # the frozen code manifest must verify (fail-closed)

    exrec_path = os.path.join(EXTRACT_DIR, "_extraction.json")
    if not os.path.exists(exrec_path):
        die(f"missing extraction record: {exrec_path}")
    exrec = json.load(open(exrec_path, encoding="utf-8"))
    pool_list = exrec.get("pools", [])
    datasets = [p["dataset"] for p in pool_list]
    if len(datasets) != len(set(datasets)):
        die(f"duplicate extraction records: {datasets}")
    pool_records = {p["dataset"]: p for p in pool_list}

    if set(pool_records) != EXPECTED_DATASETS:
        die(f"roster mismatch: got {sorted(pool_records)} expected {sorted(EXPECTED_DATASETS)}")

    run_utc = exrec["extraction_utc"]
    adigest = compute_manifest_digest()
    cdigest = config_digest(os.path.join(ROOT, "pools.json"))
    if not cdigest:
        die("config_digest is None (pools.json missing or unreadable)")

    rows, manifest_pools = [], []
    for ds in sorted(EXPECTED_DATASETS):
        rec = pool_records[ds]
        chain = rec["chain"]
        if chain not in KNOWN_CHAINS:
            die(f"{ds}: unknown chain {chain!r}")
        csv_path = os.path.join(EXTRACT_DIR, rec["csv_file"])
        if not os.path.exists(csv_path):
            die(f"{ds}: missing source CSV {csv_path}")
        df = pd.read_csv(csv_path)
        r = detect_monitoring_v3(df)
        if r.status == "DATA_ERROR":
            die(f"{ds}: classification is DATA_ERROR — {r.data_error_reason}")
        addr = str(df["pool_address"].iloc[0]).lower()
        if addr != str(rec["pool_address"]).lower():
            die(f"{ds}: pool_address CSV {addr} != record {rec['pool_address']}")
        dates = pd.to_datetime(df["date"])
        rows.append({
            "run_utc": run_utc, "pool_label": str(df["pool_label"].iloc[0]), "chain": chain,
            "pool_address": addr, "window_start": str(dates.min().date()),
            "window_end": str(dates.max().date()), "days": len(df), "status": r.status,
            "data_error_reason": r.data_error_reason,
            "window_net_usd": round(float(df["net_lp_flow_usd"].sum()), 2),
            "window_gross_inflow_usd": round(float(df["gross_lp_inflow_usd"].sum()), 2),
            "n_unpriced_legs": int(df["n_unpriced_legs"].sum()),
            "n_leakage_runs": r.n_leakage_runs,
            "window_contains_qualifying_run": r.window_contains_qualifying_run,
            "qualifying_run_start": r.qualifying_run_start, "qualifying_run_end": r.qualifying_run_end,
            "qualifying_run_outflow_usd": r.qualifying_run_outflow_usd,
            "qualifying_run_failed_replenishment": r.qualifying_run_failed_replenishment,
            "qualifying_run_persistent_memory": r.qualifying_run_persistent_memory,
            "latest_run_start": r.latest_run_start, "latest_run_end": r.latest_run_end,
            "latest_run_outflow_usd": r.latest_run_outflow_usd,
            "latest_run_failed_replenishment": r.latest_run_failed_replenishment,
            "latest_run_persistent_memory": r.latest_run_persistent_memory,
            "schema_version": SCHEMA_VERSION, "detector_version": r.detector_version,
            "artifact_digest": adigest, "config_digest": cdigest,
        })
        manifest_pools.append({
            "dataset": ds, "chain": chain, "pool_address": addr,
            "window_start": str(dates.min().date()), "window_end": str(dates.max().date()),
            "days": len(df), "status": r.status,
            "query_id": rec.get("query_id"), "execution_id": rec.get("execution_id"),
            "source_csv": rec["csv_file"], "source_csv_sha256": sha256_file(csv_path),
        })

    with open(SNAPSHOT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    data_manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_digest": adigest,
        "config_digest": cdigest,
        "extraction_utc": run_utc,
        "window": {"start": exrec.get("window_start"), "end": exrec.get("window_end")},
        "expected_roster": sorted(EXPECTED_DATASETS),
        "pools": manifest_pools,
        "snapshot_file": os.path.basename(SNAPSHOT),
        "snapshot_sha256": sha256_file(SNAPSHOT),
    }
    with open(DATA_MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data_manifest, fh, indent=2)
        fh.write("\n")

    print(f"OK: snapshot ({len(rows)} pools) + DATA_MANIFEST written.")
    print(f"  snapshot_sha256 = {data_manifest['snapshot_sha256']}")
    for row in rows:
        print(f"  {row['chain']:9} {row['status']:8} net={row['window_net_usd']:>16} "
              f"qrun={row['qualifying_run_start']}")


def verify_data_manifest() -> None:
    """Read-only integrity check for the dataset: recompute the snapshot and every source-CSV
    SHA-256 and compare to DATA_MANIFEST.json. Writes nothing; exits non-zero on any mismatch."""
    verify_integrity()  # code manifest first
    if not os.path.exists(DATA_MANIFEST):
        die(f"missing {DATA_MANIFEST}")
    dm = json.load(open(DATA_MANIFEST, encoding="utf-8"))
    if not os.path.exists(SNAPSHOT):
        die(f"missing snapshot {SNAPSHOT}")
    actual_snap = sha256_file(SNAPSHOT)
    if actual_snap != dm.get("snapshot_sha256"):
        die(f"snapshot hash mismatch: {actual_snap} != {dm.get('snapshot_sha256')}")
    seen = set()
    for p in dm.get("pools", []):
        ds = p["dataset"]
        if ds in seen:
            die(f"duplicate dataset in data manifest: {ds}")
        seen.add(ds)
        csv_path = os.path.join(EXTRACT_DIR, p["source_csv"])
        if not os.path.exists(csv_path):
            die(f"{ds}: missing source CSV {csv_path}")
        actual = sha256_file(csv_path)
        if actual != p.get("source_csv_sha256"):
            die(f"{ds}: source CSV hash mismatch {actual} != {p.get('source_csv_sha256')}")
    if seen != EXPECTED_DATASETS:
        die(f"data manifest roster mismatch: {sorted(seen)} != {sorted(EXPECTED_DATASETS)}")
    print(f"DATA INTEGRITY OK — snapshot + {len(seen)} source CSVs match DATA_MANIFEST.json")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--verify"]:
        verify_data_manifest()
    else:
        main()
