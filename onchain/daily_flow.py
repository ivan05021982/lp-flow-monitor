#!/usr/bin/env python3
"""
Daily LP-flow CSV from chain logs.

For each pool and UTC day: Mint/Burn counts, token amounts, the two daily prices used and
the USD inflow, outflow and net. This is the file the monitor page publishes as
data/daily_lp_flow.csv; run it to recompute that file, or the same series for other dates.

    python daily_flow.py --start 2026-07-07 --end 2026-10-04 --out daily_lp_flow.csv
    python daily_flow.py --start ... --end ... --out x.csv --chains ethereum,optimism

It fails closed: a chain is written only if a second, independent node returns the same
events for the last days of the range (--cross-check-days, default 14). With
--cross-check-days 0 the comparison is skipped and the run says so.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import onchain_extract as oe  # noqa: E402
import onchain_valuation as ov  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", required=True, help="First UTC day, YYYY-MM-DD.")
    ap.add_argument("--end", required=True, help="Last UTC day, YYYY-MM-DD (must be over in UTC).")
    ap.add_argument("--out", required=True, help="CSV to write.")
    ap.add_argument("--chains", help="Comma-separated subset of chains.")
    ap.add_argument("--datasets", help="Comma-separated pool datasets from the pool list "
                    "(default: the pools marked with dashboard_query_id, or all if none is marked).")
    ap.add_argument("--cross-check-days", type=int, default=14,
                    help="Trailing days compared with the second node (0 = skip, not recommended).")
    ap.add_argument("--detector-dir", help="Also write one CSV per pool in the input format of "
                    "detect_monitoring_v3 (date, net_lp_flow_usd, gross_lp_inflow_usd, n_unpriced_legs).")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end >= datetime.now(timezone.utc).date():
        print("error: --end must be before today (UTC): a partial day is not a day.", file=sys.stderr)
        return 2

    pools_cfg, sources = oe.load_config()
    pools = pools_cfg["pools"]
    if args.datasets:
        wanted = set(args.datasets.split(","))
        pools = [p for p in pools if p["dataset"] in wanted]
    elif any(p.get("dashboard_query_id") for p in pools):
        pools = [p for p in pools if p.get("dashboard_query_id")]
    if args.chains:
        pools = [p for p in pools if p["chain"] in args.chains.split(",")]
    if not pools:
        print("error: no pool selected.", file=sys.stderr)
        return 2

    symbols = oe.token_symbols(pools_cfg["pools"])
    prices = ov.daily_prices(sorted({symbols[(p["chain"], p[k].lower())] for p in pools
                                     for k in ("token0_address", "token1_address")}), start, end)
    rows, failures = [], 0
    for chain in dict.fromkeys(p["chain"] for p in pools):
        chain_pools = [p for p in pools if p["chain"] == chain]
        try:
            res = oe.extract_checked(chain, chain_pools, start, end,
                                     sources["chains"][chain]["providers"], args.cross_check_days)
        except oe.RpcError as exc:
            print(f"  [{chain}] {exc}", file=sys.stderr)
            failures += 1
            continue
        if res["problems"] and args.cross_check_days > 0:
            for line in res["problems"]:
                print(f"  [{chain}] CROSS-CHECK: {line}", file=sys.stderr)
            print(f"  [{chain}] left out: {res['provider']} and {res['compared_with']} do not agree on "
                  f"{res['check_start']}..{end}", file=sys.stderr)
            failures += 1
            continue
        print(f"  [{chain}] {res['provider']}"
              + (f" = {res['compared_with']} on {res['check_start']}..{end}" if not res["problems"]
                 else " (NOT compared with a second node)"))
        for pool in chain_pools:
            pool_rows = ov.valued_rows(pool, oe.load_events(res["provider"], chain, pool["pool_address"]),
                                       start, end, prices, symbols)
            for r in pool_rows:
                r["chain"], r["dataset"] = chain, pool["dataset"]
            rows += pool_rows
    if not rows:
        print("error: nothing to write.", file=sys.stderr)
        return 1
    ov.write_daily_csv(Path(args.out), rows)
    if args.detector_dir:
        # n_unpriced_legs is 0 by construction: a day that cannot be priced stops the run
        # (onchain_valuation raises), it is never written with a missing or zero price.
        out_dir = Path(args.detector_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for dataset in dict.fromkeys(r["dataset"] for r in rows):
            with (out_dir / f"{dataset}.csv").open("w", encoding="utf-8", newline="") as fh:
                w = csv.writer(fh, lineterminator="\n")
                w.writerow(["date", "net_lp_flow_usd", "gross_lp_inflow_usd", "n_unpriced_legs"])
                w.writerows([r["day"], r["net_lp_flow_usd"], r["gross_lp_inflow_usd"], 0]
                            for r in rows if r["dataset"] == dataset)
        print(f"wrote detector inputs to {out_dir}")
    print(f"wrote {args.out}: {len(rows)} rows, {start}..{end}, chains left out: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
