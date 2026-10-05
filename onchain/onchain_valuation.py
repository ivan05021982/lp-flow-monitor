#!/usr/bin/env python3
"""
USD valuation of the on-chain LP-flow amounts: one price per token per UTC day.

Dune valued every event with `prices.day`. Solving Dune's own dollars against the chain
amounts (contract T3) showed that this is a DAY-AVERAGE price, the same for a token on
every chain: the mean of 24 hourly prices reproduces it to a few hundredths of a percent,
while an open or close price misses by percent on volatile days. So the replacement is
the mean of the 24 hourly DefiLlama prices of the Ethereum-mainnet token.

Hourly points are cached on disk; a past hour never changes.
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

MON = Path(__file__).resolve().parent
if str(MON) not in sys.path:
    sys.path.insert(0, str(MON))

import onchain_extract as oe  # noqa: E402

PRICE_DIR = oe.EVENTS_DIR / "prices_llama_hourly"
LLAMA_CHART = "https://coins.llama.fi/chart/ethereum:{address}"
HOURS_PER_CALL = 400
# Measured 2026-04-21..2026-10-04: 3 token-days out of 668 lack an hourly point (22, 23 and
# 23 of 24), none has fewer than 22. Below this floor the day is not priced: fail, do not guess.
MIN_HOURS = 22
PRICE_SOURCE = "defillama_hourly_mean_v1"

# Ethereum-mainnet contracts used as the price reference for each symbol on every chain.
REFERENCE_TOKEN = {
    "USDC": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDT": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "WETH": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "WBTC": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",
}


# Columns of the daily CSV, in order. One writer, so every copy of that file is identical.
DAILY_FIELDS = ["day", "chain", "pool_label", "pool_address", "mint_count", "burn_count", "unique_txs",
                "mint_amount0", "mint_amount1", "burn_amount0", "burn_amount1",
                "price_token0_usd", "price_token1_usd", "gross_lp_inflow_usd", "gross_lp_outflow_usd",
                "net_lp_flow_usd", "price_source"]


class PriceError(RuntimeError):
    pass


def _hour_ts(day: date, hour: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()) + hour * 3600


def _cache_path(symbol: str) -> Path:
    return PRICE_DIR / f"{symbol}.csv"


def _load_cache(symbol: str) -> dict[int, float]:
    path = _cache_path(symbol)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {int(r["hour_ts"]): float(r["price"]) for r in csv.DictReader(fh)}


def _save_cache(symbol: str, hourly: dict[int, float]) -> None:
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    with _cache_path(symbol).open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["hour_ts", "hour_utc", "price"])
        for ts in sorted(hourly):
            w.writerow([ts, datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z"), hourly[ts]])


def _fetch_hourly(symbol: str, first_ts: int, last_ts: int) -> dict[int, float]:
    """Hourly point prices in [first_ts, last_ts], keyed by the exact hour."""
    out: dict[int, float] = {}
    url = LLAMA_CHART.format(address=REFERENCE_TOKEN[symbol])
    cursor = first_ts
    while cursor <= last_ts:
        span = min(HOURS_PER_CALL, (last_ts - cursor) // 3600 + 1)
        delay = 2.0
        for attempt in range(1, 6):
            try:
                resp = requests.get(url, params={"start": cursor, "span": span, "period": "1h"}, timeout=60)
                if resp.status_code == 200:
                    coins = resp.json().get("coins", {})
                    break
                err = f"HTTP {resp.status_code}"
            except (requests.RequestException, ValueError) as exc:
                err = type(exc).__name__
            if attempt == 5:
                raise PriceError(f"DefiLlama {symbol}: {err} after {attempt} attempts")
            time.sleep(delay)
            delay *= 2
        for coin in coins.values():
            for p in coin.get("prices", []):
                hour = round(p["timestamp"] / 3600) * 3600
                if abs(p["timestamp"] - hour) <= 600:       # a point more than 10 min off is not that hour
                    out[hour] = float(p["price"])
        cursor += span * 3600
        time.sleep(0.3)
    return out


def daily_prices(symbols: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """{symbol: {day: mean of the 24 hourly prices of that UTC day}} for [start, end]."""
    out: dict[str, dict[str, float]] = {}
    for symbol in symbols:
        hourly = _load_cache(symbol)
        wanted = [_hour_ts(start) + k * 3600 for k in range(((end - start).days + 1) * 24)]
        missing = [ts for ts in wanted if ts not in hourly]
        if missing:
            hourly.update(_fetch_hourly(symbol, min(missing), max(missing)))
            _save_cache(symbol, hourly)
        out[symbol] = {}
        day = start
        while day <= end:
            hours = [hourly.get(_hour_ts(day, h)) for h in range(24)]
            have = [x for x in hours if x is not None]
            if len(have) < MIN_HOURS:
                raise PriceError(f"{symbol} {day}: only {len(have)}/24 hourly prices available")
            out[symbol][day.isoformat()] = sum(have) / len(have)
            day += timedelta(days=1)
    return out


def valued_rows(pool: dict, events: list[dict], start: date, end: date,
                prices: dict[str, dict[str, float]], symbols: dict[tuple[str, str], str]) -> list[dict]:
    """Daily rows in the schema of lp_flow_template.sql, plus the token amounts and the
    two prices used, so every dollar figure can be recomputed."""
    sym0 = symbols[(pool["chain"], pool["token0_address"].lower())]
    sym1 = symbols[(pool["chain"], pool["token1_address"].lower())]
    rows = []
    for r in oe.daily_amounts(events, pool, start, end):
        p0, p1 = prices[sym0][r["day"]], prices[sym1][r["day"]]
        inflow = r["mint_amount0"] * p0 + r["mint_amount1"] * p1
        outflow = r["burn_amount0"] * p0 + r["burn_amount1"] * p1
        rows.append({
            "day": r["day"], "pool_label": pool["label"], "pool_address": pool["pool_address"],
            "gross_lp_inflow_usd": inflow, "gross_lp_outflow_usd": outflow,
            "net_lp_flow_usd": inflow - outflow,
            "mint_count": r["mint_count"], "burn_count": r["burn_count"], "unique_txs": r["unique_txs"],
            "mint_amount0": r["mint_amount0"], "mint_amount1": r["mint_amount1"],
            "burn_amount0": r["burn_amount0"], "burn_amount1": r["burn_amount1"],
            "price_token0_usd": p0, "price_token1_usd": p1, "price_source": PRICE_SOURCE,
        })
    return rows


def write_daily_csv(path: Path, rows: list[dict]) -> None:
    """Rows of valued_rows (each with a "chain" key added) -> the daily CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=DAILY_FIELDS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
