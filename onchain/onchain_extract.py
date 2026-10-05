#!/usr/bin/env python3
"""
On-chain LP-flow extraction: Uniswap V3 Mint/Burn events read from public RPC nodes.

Replaces the Dune SQL extraction of the weekly monitor (Dune's free plan stopped
executing queries on 2026-09-24). How it was accepted against the Dune history is in the
extraction contract. This module only produces token amounts and event counts per UTC
day; USD valuation is a separate step (onchain_valuation.py).

Usage:
    python onchain_extract.py --start 2026-08-03 --end 2026-09-16
    python onchain_extract.py --start ... --end ... --chains base --provider 1
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Optional. Some networks intercept TLS with a certificate authority that OpenSSL rejects
# (antivirus, corporate proxy); truststore verifies through the operating system's store
# instead. Without it, on such a network, requests fail with an SSL error.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests

MON = Path(__file__).resolve().parent
SOURCES = MON / "onchain_sources.json"
# Where the pool list and the local cache live is configuration, not code, so the same
# module runs unchanged from different folder layouts. Both are relative to this folder.
_PATHS = json.loads(SOURCES.read_text(encoding="utf-8"))
POOLS = (MON / _PATHS.get("pools_file", "pools.json")).resolve()
EVENTS_DIR = (MON / _PATHS.get("cache_dir", "_cache")).resolve()

# keccak256 of the Uniswap V3 pool event signatures (checked empirically by contract T1:
# a wrong topic cannot reproduce Dune's per-day event counts).
MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"

EVENT_FIELDS = ["block_number", "log_index", "tx_hash", "block_timestamp", "kind",
                "amount0_raw", "amount1_raw"]
MAX_ATTEMPTS = 8
SEGMENT_REQUESTS = 40          # HTTP requests between two saves of the cache


class RpcError(RuntimeError):
    pass


class RpcReplyError(RpcError):
    """The provider answered, with a JSON-RPC error (as opposed to not answering)."""


def _is_rate_limit(body) -> bool:
    items = body if isinstance(body, list) else [body]
    for item in items:
        err = item.get("error") if isinstance(item, dict) else None
        if isinstance(err, dict):
            text = str(err.get("message", "")).lower()
            if err.get("code") in (-32005, 429) or "rate limit" in text or "too many requests" in text:
                return True
    return False


class Rpc:
    """Minimal JSON-RPC client for one provider: retry with backoff on transport errors,
    HTTP 429/5xx and rate-limit replies; everything else surfaces as RpcError."""

    def __init__(self, name: str, url: str, max_span: int, batch: int = 1, header_batch: int = 1,
                 pause: float = 0.15):
        self.name, self.url, self.max_span, self.batch, self.pause = name, url, max_span, batch, pause
        self.header_batch = header_batch
        self.session = requests.Session()
        self.requests_made = 0

    def _post(self, payload):
        delay = 1.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.requests_made += 1
            try:
                resp = self.session.post(self.url, json=payload, timeout=60)
            except requests.RequestException as exc:
                err = type(exc).__name__
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    err = f"HTTP {resp.status_code}"
                else:
                    try:
                        body = resp.json()
                    except ValueError:
                        err = f"HTTP {resp.status_code} non-JSON"
                    else:
                        if not _is_rate_limit(body):
                            time.sleep(self.pause)
                            return body
                        err = "rate limited"
            if attempt == MAX_ATTEMPTS:
                raise RpcError(f"{self.name}: {err} after {attempt} attempts")
            time.sleep(delay)
            delay = min(delay * 2, 60.0)

    def call(self, method: str, params: list):
        body = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(body, dict) or body.get("error") or "result" not in body:
            raise RpcReplyError(f"{self.name} {method}: {str(body)[:300]}")
        return body["result"]

    def call_many(self, calls: list[tuple[str, list]]) -> list:
        """Results in call order. Uses JSON-RPC batching when the provider allows it."""
        if self.batch <= 1 or len(calls) == 1:
            return [self.call(m, p) for m, p in calls]
        out: list = [None] * len(calls)
        for i in range(0, len(calls), self.batch):
            chunk = calls[i:i + self.batch]
            body = self._post([{"jsonrpc": "2.0", "id": i + k, "method": m, "params": p}
                               for k, (m, p) in enumerate(chunk)])
            if not isinstance(body, list):
                raise RpcError(f"{self.name} batch rejected: {str(body)[:300]}")
            by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
            for k in range(len(chunk)):
                item = by_id.get(i + k)
                if item is None or item.get("error") or "result" not in item:
                    raise RpcError(f"{self.name} batch item {i + k}: {str(item)[:300]}")
                out[i + k] = item["result"]
        return out

    def head(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, number: int) -> int:
        block = self.call("eth_getBlockByNumber", [hex(number), False])
        if not block:
            raise RpcError(f"{self.name}: block {number} not found")
        return int(block["timestamp"], 16)

    def block_timestamps(self, numbers: list[int]) -> dict[int, int]:
        """Header timestamps of many blocks, batched when the provider allows it."""
        out: dict[int, int] = {}
        size = max(1, self.header_batch)
        for i in range(0, len(numbers), size):
            chunk = numbers[i:i + size]
            if size == 1:
                out[chunk[0]] = self.block_timestamp(chunk[0])
                continue
            body = self._post([{"jsonrpc": "2.0", "id": k, "method": "eth_getBlockByNumber",
                                "params": [hex(n), False]} for k, n in enumerate(chunk)])
            if not isinstance(body, list):
                raise RpcError(f"{self.name} header batch rejected: {str(body)[:300]}")
            by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
            for k, n in enumerate(chunk):
                item = by_id.get(k)
                if not item or not item.get("result"):
                    raise RpcError(f"{self.name}: header of block {n} not returned")
                out[n] = int(item["result"]["timestamp"], 16)
        return out


def first_block_at_or_after(rpc: Rpc, ts: int, head: int) -> int:
    """Lowest block whose timestamp is >= ts. Block timestamps never decrease, so the
    search is exact. It walks back from the head in doubling steps and then bisects, so
    it only reads recent headers: some public nodes do not serve very old blocks
    (Tenderly's Optimism gateway returns nothing before the Bedrock upgrade)."""
    if rpc.block_timestamp(head) < ts:
        raise RpcError(f"{rpc.name}: chain head is older than the requested time {ts}")
    hi, step = head, 4096
    lo = max(0, hi - step)
    while lo > 0 and rpc.block_timestamp(lo) >= ts:
        hi, step = lo, step * 2
        lo = max(0, hi - step)
    if lo == 0 and rpc.block_timestamp(0) >= ts:
        return 0
    while hi - lo > 1:                      # invariant: ts(lo) < ts <= ts(hi)
        mid = (lo + hi) // 2
        if rpc.block_timestamp(mid) >= ts:
            hi = mid
        else:
            lo = mid
    return hi


def _day_ts(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def decode_log(log: dict) -> dict:
    """One Mint/Burn log -> flat event. Fails loudly if the data layout is not the one
    the contract assumes, instead of silently reading the wrong words."""
    if log.get("removed"):
        raise RpcError(f"removed (reorged) log returned: {log.get('transactionHash')}")
    topic = log["topics"][0].lower()
    data = log["data"][2:]
    words = [int(data[i:i + 64], 16) for i in range(0, len(data), 64)]
    if topic == MINT_TOPIC and len(words) == 4:        # sender, amount, amount0, amount1
        kind, amount0, amount1 = "mint", words[2], words[3]
    elif topic == BURN_TOPIC and len(words) == 3:      # amount, amount0, amount1
        kind, amount0, amount1 = "burn", words[1], words[2]
    else:
        raise RpcError(f"unexpected log layout: topic {topic[:12]} with {len(words)} data words")
    # Not every node fills this field: Arbitrum's official RPC returns "0x0". Zero or
    # absent means "not provided"; the caller then reads the block header.
    ts = int(log["blockTimestamp"], 16) if log.get("blockTimestamp") else 0
    return {
        "address": log["address"].lower(),
        "block_number": int(log["blockNumber"], 16),
        "log_index": int(log["logIndex"], 16),
        "tx_hash": log["transactionHash"].lower(),
        "block_timestamp": ts or None,
        "kind": kind,
        "amount0_raw": amount0,
        "amount1_raw": amount1,
    }


def fetch_window(rpc: Rpc, addresses: list[str], a: int, b: int) -> list[dict]:
    """Logs in [a, b]. If the provider refuses the window (too many results, range
    limit changed), split it in half and try again down to a single block."""
    flt = {"address": addresses if len(addresses) > 1 else addresses[0],
           "topics": [[MINT_TOPIC, BURN_TOPIC]], "fromBlock": hex(a), "toBlock": hex(b)}
    try:
        return rpc.call("eth_getLogs", [flt])
    except RpcReplyError:
        if a == b:
            raise
        mid = (a + b) // 2
        return fetch_window(rpc, addresses, a, mid) + fetch_window(rpc, addresses, mid + 1, b)


def fetch_range(rpc: Rpc, addresses: list[str], windows: list[tuple[int, int]]) -> list[dict]:
    if rpc.batch > 1:
        calls = [("eth_getLogs", [{"address": addresses if len(addresses) > 1 else addresses[0],
                                   "topics": [[MINT_TOPIC, BURN_TOPIC]],
                                   "fromBlock": hex(a), "toBlock": hex(b)}]) for a, b in windows]
        return [log for res in rpc.call_many(calls) for log in res]
    return [log for a, b in windows for log in fetch_window(rpc, addresses, a, b)]


# --- cache: events per pool + covered block intervals per (provider, chain) ---

def _merge(intervals: list[list[int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _gaps(covered: list[list[int]], a: int, b: int) -> list[tuple[int, int]]:
    gaps, cursor = [], a
    for lo, hi in _merge(covered):
        if hi < cursor or lo > b:
            continue
        if lo > cursor:
            gaps.append((cursor, lo - 1))
        cursor = max(cursor, hi + 1)
    if cursor <= b:
        gaps.append((cursor, b))
    return gaps


def _events_path(provider: str, chain: str, pool_address: str) -> Path:
    return EVENTS_DIR / provider / f"{chain}_{pool_address.lower()}.csv"


def load_events(provider: str, chain: str, pool_address: str) -> list[dict]:
    path = _events_path(provider, chain, pool_address)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["block_number"], r["log_index"] = int(r["block_number"]), int(r["log_index"])
        r["block_timestamp"] = int(r["block_timestamp"])
        r["amount0_raw"], r["amount1_raw"] = int(r["amount0_raw"]), int(r["amount1_raw"])
    return rows


def _save_events(provider: str, chain: str, pool_address: str, events: list[dict]) -> None:
    path = _events_path(provider, chain, pool_address)
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = {(e["block_number"], e["log_index"]): e for e in events}
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EVENT_FIELDS)
        w.writeheader()
        for key in sorted(unique):
            w.writerow({k: unique[key][k] for k in EVENT_FIELDS})
    os.replace(tmp, path)          # a reader never sees a half-written cache


def _coverage_path(provider: str) -> Path:
    return EVENTS_DIR / provider / "coverage.json"


def _load_coverage(provider: str) -> dict:
    path = _coverage_path(provider)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_coverage(provider: str, coverage: dict) -> None:
    path = _coverage_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(coverage, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _fill_timestamps(rpc: Rpc, events: list[dict]) -> int:
    """Give every event without a block timestamp the one in its block header.
    Returns how many events were completed."""
    missing = sorted({e["block_number"] for e in events if not e["block_timestamp"]})
    if not missing:
        return 0
    if len(missing) > 2000:
        print(f"    [{rpc.name}] reading {len(missing)} block headers for timestamps", flush=True)
    ts_by_block = rpc.block_timestamps(missing)
    filled = 0
    for e in events:
        if not e["block_timestamp"]:
            e["block_timestamp"] = ts_by_block[e["block_number"]]
            filled += 1
    return filled


def extract_chain(chain: str, pools: list[dict], start: date, end: date, provider: dict,
                  header_timestamps: bool = True) -> dict:
    """Make the cache of `provider` cover [start, end] (UTC days, inclusive) for every
    pool of `chain`, fetching only the block ranges not covered yet. Returns the block
    range of the request.

    header_timestamps=False leaves events whose log carries no timestamp at 0 instead of
    reading their block headers. Such a cache can be compared event by event with another
    provider (contract T2) but cannot be bucketed into days: daily_amounts refuses it."""
    rpc = Rpc(provider["name"], provider["url"], int(provider["max_span"]), int(provider.get("batch", 1)),
              int(provider.get("header_batch", 1)))
    addresses = sorted(p["pool_address"].lower() for p in pools)
    coverage = _load_coverage(rpc.name)
    cov = _chain_coverage(coverage, chain)
    per_pool = {a: cov["pools"].setdefault(a, {"intervals": [], "day_ranges": []}) for a in addresses}

    head = rpc.head()
    day_blocks = cov.setdefault("day_blocks", {})
    for day in (start, end + timedelta(days=1)):
        if day.isoformat() not in day_blocks:
            day_blocks[day.isoformat()] = first_block_at_or_after(rpc, _day_ts(day), head)
    b_start = day_blocks[start.isoformat()]
    b_end = day_blocks[(end + timedelta(days=1)).isoformat()] - 1

    events = {addr: load_events(rpc.name, chain, addr) for addr in addresses}
    for addr in addresses:                      # repair a cache written without timestamps
        if header_timestamps and _fill_timestamps(rpc, events[addr]):
            _save_events(rpc.name, chain, addr, events[addr])
    # A block range missing for any requested pool is read for all of them: one request
    # serves the whole pool list, and events already cached are de-duplicated on save.
    gaps = _merge([list(g) for a in addresses for g in _gaps(per_pool[a]["intervals"], b_start, b_end)])
    for gap_a, gap_b in gaps:
        windows = [(a, min(a + rpc.max_span - 1, gap_b)) for a in range(gap_a, gap_b + 1, rpc.max_span)]
        per_segment = SEGMENT_REQUESTS * rpc.batch
        print(f"  [{chain}/{rpc.name}] blocks {gap_a}..{gap_b}: {len(windows)} windows", flush=True)
        for i in range(0, len(windows), per_segment):
            segment = windows[i:i + per_segment]
            decoded = [decode_log(log) for log in fetch_range(rpc, addresses, segment)]
            if header_timestamps:
                _fill_timestamps(rpc, decoded)
            for e in decoded:
                e["block_timestamp"] = e["block_timestamp"] or 0
                events[e["address"]].append(e)
            # Save after every segment so an interrupted run keeps what it fetched.
            for addr in addresses:
                _save_events(rpc.name, chain, addr, events[addr])
                per_pool[addr]["intervals"] = _merge(per_pool[addr]["intervals"]
                                                     + [[segment[0][0], segment[-1][1]]])
            coverage[chain] = cov
            _save_coverage(rpc.name, coverage)
            done = min(i + per_segment, len(windows))
            print(f"    {done}/{len(windows)} windows, {sum(len(v) for v in events.values())} events cached, "
                  f"{rpc.requests_made} requests", flush=True)
    # Only now is [start, end] known to be complete: record it as a covered range of days.
    for addr in addresses:
        per_pool[addr]["day_ranges"] = _merge(per_pool[addr].get("day_ranges", [])
                                              + [[start.toordinal(), end.toordinal()]])
    coverage[chain] = cov
    _save_coverage(rpc.name, coverage)
    return {"from_block": b_start, "to_block": b_end, "requests": rpc.requests_made}


def _chain_coverage(coverage: dict, chain: str) -> dict:
    """Coverage of one chain: the first block of each UTC day seen so far and, per pool,
    the block intervals and the day ranges its cache holds completely. It is kept per pool
    so that asking for a subset of the pools never invalidates what the others have."""
    cov = coverage.get(chain) or {}
    if "pools" not in cov:        # layout used on 2026-10-05: one set of intervals per chain
        cov = {"day_blocks": cov.get("day_blocks", {}),
               "pools": {a: {"intervals": [list(i) for i in cov.get("intervals", [])],
                             "day_ranges": [list(r) for r in cov.get("day_ranges", [])]}
                         for a in cov.get("addresses", [])}}
    return cov


def covered_intervals(provider: str, chain: str, pool_address: str) -> list[list[int]]:
    """Block intervals the cache of `provider` holds for one pool."""
    cov = _chain_coverage(_load_coverage(provider), chain)
    return cov["pools"].get(pool_address.lower(), {}).get("intervals", [])


def days_covered(provider: str, chain: str, start: date, end: date, pool_address: str) -> bool:
    """True when the cache of `provider` holds every event of [start, end] for the pool."""
    cov = _chain_coverage(_load_coverage(provider), chain)
    ranges = cov["pools"].get(pool_address.lower(), {}).get("day_ranges", [])
    return any(a <= start.toordinal() and end.toordinal() <= b for a, b in ranges)


def cross_check(chain: str, pools: list[dict], primary: dict, secondary: dict,
                start: date, end: date) -> list[str]:
    """Contract T2 as a gate. Read [start, end] from the second provider as well and
    compare, event by event, with the working provider. Returns the problems found; an
    empty list means the two nodes agree. Since 2026-09-17 there is no Dune figure to
    compare new data with: this agreement is the only check left on it.

    The second provider is read without header timestamps (a node that leaves the
    timestamp off its logs stores 0); an event is then compared on block, log index,
    transaction, kind and amounts, and on the timestamp only when both nodes gave one."""
    extract_chain(chain, pools, start, end, primary)          # boundaries of [start, end]
    extract_chain(chain, pools, start, end, secondary, header_timestamps=False)
    cov_a = _load_coverage(primary["name"])[chain]["day_blocks"]
    cov_b = _load_coverage(secondary["name"])[chain]["day_blocks"]
    k0, k1 = start.isoformat(), (end + timedelta(days=1)).isoformat()
    problems = [f"first block of {k}: {primary['name']} says {cov_a[k]}, {secondary['name']} says {cov_b[k]}"
                for k in (k0, k1) if cov_a[k] != cov_b[k]]
    lo, hi = cov_a[k0], cov_a[k1] - 1
    for pool in pools:
        def keyed(name: str) -> dict:
            return {(e["block_number"], e["log_index"]):
                    (e["tx_hash"], e["kind"], e["amount0_raw"], e["amount1_raw"], e["block_timestamp"])
                    for e in load_events(name, chain, pool["pool_address"]) if lo <= e["block_number"] <= hi}
        a, b = keyed(primary["name"]), keyed(secondary["name"])
        only_a, only_b = len(set(a) - set(b)), len(set(b) - set(a))
        differ = sum(1 for k in set(a) & set(b)
                     if a[k][:4] != b[k][:4] or (a[k][4] and b[k][4] and a[k][4] != b[k][4]))
        if only_a or only_b or differ:
            problems.append(f"{pool['label']}: {only_a} events only on {primary['name']}, {only_b} only on "
                            f"{secondary['name']}, {differ} with different content ({len(a)} vs {len(b)} events)")
    return problems


def extract_checked(chain: str, pools: list[dict], start: date, end: date, providers: list[dict],
                    cross_check_days: int = 14) -> dict:
    """Read [start, end] from the first provider that answers, then compare the trailing
    `cross_check_days` of it with another provider. Returns the provider used, the one it
    was compared with, the first day compared and the problems found: an empty list means
    the two nodes agree. cross_check_days=0 skips the comparison and says so in the
    problems. Raises RpcError when no provider can be read."""
    used, errors = None, []
    for provider in providers:
        try:
            extract_chain(chain, pools, start, end, provider)
            used = provider
            break
        except RpcError as exc:
            errors.append(f"{provider['name']}: {exc}")
    if used is None:
        raise RpcError("no provider could be read: " + "; ".join(errors))
    other = next((p for p in providers if p["name"] != used["name"]), None)
    check_start = max(start, end - timedelta(days=max(cross_check_days, 1) - 1))
    if cross_check_days <= 0 or other is None:
        problems = ["not compared with a second node"]
    else:
        try:
            problems = cross_check(chain, pools, used, other, check_start, end)
        except RpcError as exc:
            problems = [f"second provider {other['name']} could not be read: {exc}"]
    return {"provider": used["name"], "compared_with": other["name"] if other else None,
            "check_start": check_start, "problems": problems, "failed_over": errors}


def daily_amounts(events: list[dict], pool: dict, start: date, end: date) -> list[dict]:
    """Per UTC day in [start, end]: event counts and token amounts in token units.
    Every calendar day is present, also the ones with no event."""
    d0, d1 = 10 ** int(pool["token0_decimals"]), 10 ** int(pool["token1_decimals"])
    days: dict[str, dict] = {}
    day = start
    while day <= end:
        days[day.isoformat()] = {"mint0": 0, "mint1": 0, "burn0": 0, "burn1": 0,
                                 "mint_count": 0, "burn_count": 0, "txs": set()}
        day += timedelta(days=1)
    for e in events:
        if not e["block_timestamp"]:
            raise RpcError("this cache holds events without a block timestamp (cross-check only): "
                           "it cannot be split into days")
        key = datetime.fromtimestamp(e["block_timestamp"], tz=timezone.utc).date().isoformat()
        bucket = days.get(key)
        if bucket is None:
            continue
        bucket[f"{e['kind']}0"] += e["amount0_raw"]
        bucket[f"{e['kind']}1"] += e["amount1_raw"]
        bucket[f"{e['kind']}_count"] += 1
        bucket["txs"].add(e["tx_hash"])
    return [{
        "day": key,
        "mint_count": b["mint_count"], "burn_count": b["burn_count"], "unique_txs": len(b["txs"]),
        "mint_amount0": b["mint0"] / d0, "mint_amount1": b["mint1"] / d1,
        "burn_amount0": b["burn0"] / d0, "burn_amount1": b["burn1"] / d1,
    } for key, b in days.items()]


TOKEN_DECIMALS = {"USDC": 6, "USDT": 6, "WETH": 18, "WBTC": 8}


def token_symbols(pools: list[dict]) -> dict[tuple[str, str], str]:
    """(chain, token address) -> symbol, read from the pair in each pool label.
    token0/token1 order differs per chain, so the two symbols are assigned by decimals;
    only when both tokens have the same decimals (USDC/USDT) does the label order decide."""
    out: dict[tuple[str, str], str] = {}
    for p in pools:
        first, second = next(t for t in p["label"].split() if "/" in t).split("/")
        d0, d1 = int(p["token0_decimals"]), int(p["token1_decimals"])
        if d0 != d1 and (TOKEN_DECIMALS[first], TOKEN_DECIMALS[second]) != (d0, d1):
            first, second = second, first
        if (TOKEN_DECIMALS[first], TOKEN_DECIMALS[second]) != (d0, d1):
            raise ValueError(f"cannot map label to tokens for {p['label']}")
        out[(p["chain"], p["token0_address"].lower())] = first
        out[(p["chain"], p["token1_address"].lower())] = second
    return out


def load_config() -> tuple[dict, dict]:
    return (json.loads(POOLS.read_text(encoding="utf-8")),
            json.loads(SOURCES.read_text(encoding="utf-8")))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="First UTC day, YYYY-MM-DD.")
    ap.add_argument("--end", required=True, help="Last UTC day, YYYY-MM-DD (must be fully in the past).")
    ap.add_argument("--chains", help="Comma-separated subset of chains (default: all in pools.json).")
    ap.add_argument("--provider", type=int, default=0,
                    help="Index into the chain's provider list (0 = working source, 1 = cross-check).")
    ap.add_argument("--skip-header-timestamps", action="store_true",
                    help="Do not read block headers for logs that carry no timestamp. The cache is then "
                         "good for the event-by-event cross-check only.")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end >= datetime.now(timezone.utc).date():
        print("error: --end must be before today (UTC): a partial day is not a day.", file=sys.stderr)
        return 2

    pools_cfg, sources = load_config()
    chains = args.chains.split(",") if args.chains else sorted({p["chain"] for p in pools_cfg["pools"]})
    failures = 0
    for chain in chains:
        pools = [p for p in pools_cfg["pools"] if p["chain"] == chain]
        provider = sources["chains"][chain]["providers"][args.provider]
        t0 = time.time()
        try:
            info = extract_chain(chain, pools, start, end, provider, not args.skip_header_timestamps)
        except RpcError as exc:
            failures += 1
            print(f"  ERROR {chain}/{provider['name']}: {exc}", file=sys.stderr)
            continue
        for p in pools:
            n = len(load_events(provider["name"], chain, p["pool_address"]))
            print(f"  [{chain}/{provider['name']}] {p['label']}: {n} events cached")
        print(f"  [{chain}/{provider['name']}] blocks {info['from_block']}..{info['to_block']}, "
              f"{info['requests']} requests, {time.time() - t0:.0f}s", flush=True)
    print(f"done. chains={len(chains)} failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
