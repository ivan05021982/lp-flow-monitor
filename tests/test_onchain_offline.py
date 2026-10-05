#!/usr/bin/env python3
"""
Offline tests of the on-chain extraction: no node is contacted.

A small synthetic chain (one block every 12 seconds, two pools, known Mint/Burn logs)
stands in for the RPC provider. Runs with pytest, or standalone:
`python test_onchain_offline.py` (exit != 0 on failure).

Each test encodes something that went wrong, or could silently go wrong, on real data:
  * a node that returns blockTimestamp 0x0 on logs (Arbitrum's official node) put every
    event outside every day while the totals still matched;
  * asking for a subset of the pools wiped the cache of the whole chain;
  * a second node that misses an event, changes an amount or moves a timestamp must stop
    the run, not pass.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE, HERE.parent / "onchain"):
    if (candidate / "onchain_extract.py").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import onchain_extract as oe  # noqa: E402
import onchain_valuation as ov  # noqa: E402

GENESIS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
PER_DAY = 7200                                  # 12-second blocks
HEAD = PER_DAY * 40
POOL_A = "0x" + "a" * 40
POOL_B = "0x" + "b" * 40
USDC, WETH = "0x" + "1" * 40, "0x" + "2" * 40
POOLS = [
    {"dataset": "a", "chain": "testchain", "label": "Test Chain USDC/WETH 0.05%", "pool_address": POOL_A,
     "token0_address": USDC, "token0_decimals": 6, "token1_address": WETH, "token1_decimals": 18},
    # token order reversed, as on Arbitrum and Base
    {"dataset": "b", "chain": "testchain", "label": "Test Chain USDC/WETH 0.30%", "pool_address": POOL_B,
     "token0_address": WETH, "token0_decimals": 18, "token1_address": USDC, "token1_decimals": 6},
]


def _word(x: int) -> str:
    return f"{x:064x}"


def _raw_log(pool: str, block: int, index: int, kind: str, amount0: int, amount1: int) -> dict:
    words = [1, 5, amount0, amount1] if kind == "mint" else [5, amount0, amount1]
    return {"address": pool, "topics": [oe.MINT_TOPIC if kind == "mint" else oe.BURN_TOPIC],
            "data": "0x" + "".join(_word(w) for w in words), "blockNumber": hex(block),
            "logIndex": hex(index), "transactionHash": "0x" + f"{block * 1000 + index:064x}",
            "blockTimestamp": hex(GENESIS + block * 12), "removed": False}


def _chain_logs() -> list[dict]:
    logs = []
    for day in range(5, 21):
        first = day * PER_DAY
        # exactly at midnight: belongs to this day, not the previous one
        logs.append(_raw_log(POOL_A, first, 0, "mint", 1_000_000 * day, 10 ** 18))
        # last block of the day
        logs.append(_raw_log(POOL_A, first + PER_DAY - 1, 3, "burn", 500_000 * day, 0))
        # two events in one transaction-less block, pool with reversed tokens
        logs.append(_raw_log(POOL_B, first + 100, 1, "mint", 2 * 10 ** 18, 3_000_000))
        logs.append(_raw_log(POOL_B, first + 100, 2, "burn", 0, 0))          # zero burn still counts
    return logs


LOGS = _chain_logs()


class FakeRpc:
    """Stands in for oe.Rpc. `tamper` rewrites the logs a provider returns."""
    getlogs_calls = 0
    tamper = {}
    no_timestamps = set()

    def __init__(self, name, url, max_span, batch=1, header_batch=1, pause=0.0):
        self.name, self.max_span, self.batch, self.header_batch = name, max_span, batch, header_batch
        self.requests_made = 0

    def head(self):
        return HEAD

    def block_timestamp(self, number):
        return GENESIS + number * 12

    def block_timestamps(self, numbers):
        return {n: self.block_timestamp(n) for n in numbers}

    def call(self, method, params):
        assert method == "eth_getLogs", method
        FakeRpc.getlogs_calls += 1
        flt = params[0]
        wanted = flt["address"] if isinstance(flt["address"], list) else [flt["address"]]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        out = [dict(log) for log in LOGS if log["address"] in wanted and lo <= int(log["blockNumber"], 16) <= hi]
        if self.name in FakeRpc.no_timestamps:
            for log in out:
                log["blockTimestamp"] = "0x0"
        return FakeRpc.tamper.get(self.name, lambda logs: logs)(out)


def _day(n: int) -> date:
    return date(2026, 1, 1) + timedelta(days=n)


class Sandbox:
    """Fresh cache folder + fake node for one test."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (oe.EVENTS_DIR, oe.Rpc)
        oe.EVENTS_DIR, oe.Rpc = Path(self._tmp.name), FakeRpc
        FakeRpc.getlogs_calls, FakeRpc.tamper, FakeRpc.no_timestamps = 0, {}, set()
        self._out = contextlib.redirect_stdout(io.StringIO())
        self._out.__enter__()
        return self

    def __exit__(self, *exc):
        self._out.__exit__(*exc)
        oe.EVENTS_DIR, oe.Rpc = self._saved
        self._tmp.cleanup()


A = {"name": "node-a", "url": "x", "max_span": 5000}
B = {"name": "node-b", "url": "x", "max_span": 20000}


def test_decode_layout_and_missing_timestamp():
    mint = oe.decode_log(_raw_log(POOL_A, 10, 0, "mint", 7, 9))
    burn = oe.decode_log(_raw_log(POOL_A, 10, 1, "burn", 3, 4))
    assert (mint["kind"], mint["amount0_raw"], mint["amount1_raw"]) == ("mint", 7, 9)
    assert (burn["kind"], burn["amount0_raw"], burn["amount1_raw"]) == ("burn", 3, 4)
    zero = _raw_log(POOL_A, 10, 0, "mint", 7, 9)
    zero["blockTimestamp"] = "0x0"
    assert oe.decode_log(zero)["block_timestamp"] is None        # 0x0 means "not provided"
    wrong = _raw_log(POOL_A, 10, 0, "mint", 7, 9)
    wrong["data"] = wrong["data"][:-64]                          # a Mint with Burn's layout
    for bad in (wrong, dict(_raw_log(POOL_A, 10, 0, "mint", 7, 9), removed=True)):
        try:
            oe.decode_log(bad)
        except oe.RpcError:
            continue
        raise AssertionError("a malformed or reorged log was accepted")


def test_first_block_of_a_day_is_exact():
    rpc = FakeRpc("node-a", "x", 5000)
    for n in (1, 5, 20, 39):
        ts = GENESIS + n * 86400
        assert oe.first_block_at_or_after(rpc, ts, HEAD) == n * PER_DAY
        assert oe.first_block_at_or_after(rpc, ts + 1, HEAD) == n * PER_DAY + 1


def test_daily_amounts_counts_days_and_decimals():
    with Sandbox():
        oe.extract_chain("testchain", POOLS, _day(5), _day(20), A)
        rows_a = {r["day"]: r for r in oe.daily_amounts(oe.load_events("node-a", "testchain", POOL_A),
                                                        POOLS[0], _day(4), _day(21))}
        rows_b = oe.daily_amounts(oe.load_events("node-a", "testchain", POOL_B), POOLS[1], _day(5), _day(20))
    assert len(rows_a) == 18                                      # every calendar day, also empty ones
    assert rows_a[_day(4).isoformat()]["mint_count"] == 0 and rows_a[_day(21).isoformat()]["burn_count"] == 0
    d7 = rows_a[_day(7).isoformat()]                              # midnight mint and last-block burn both in day 7
    assert (d7["mint_count"], d7["burn_count"], d7["unique_txs"]) == (1, 1, 2)
    assert d7["mint_amount0"] == 7.0 and d7["mint_amount1"] == 1.0 and d7["burn_amount0"] == 3.5
    b = rows_b[0]                                                 # reversed token order: token0 is WETH
    assert (b["mint_count"], b["burn_count"], b["unique_txs"]) == (1, 1, 2)
    assert b["mint_amount0"] == 2.0 and b["mint_amount1"] == 3.0


def test_node_without_log_timestamps_is_bucketed_from_headers():
    with Sandbox():
        FakeRpc.no_timestamps = {"node-a"}
        oe.extract_chain("testchain", POOLS, _day(5), _day(20), A)
        events = oe.load_events("node-a", "testchain", POOL_A)
        rows = oe.daily_amounts(events, POOLS[0], _day(5), _day(20))
    assert all(e["block_timestamp"] > GENESIS for e in events)
    assert sum(r["mint_count"] for r in rows) == 16 and all(r["mint_count"] == 1 for r in rows)


def test_cache_without_timestamps_refuses_to_split_into_days():
    with Sandbox():
        FakeRpc.no_timestamps = {"node-a"}
        oe.extract_chain("testchain", POOLS, _day(5), _day(20), A, header_timestamps=False)
        events = oe.load_events("node-a", "testchain", POOL_A)
        try:
            oe.daily_amounts(events, POOLS[0], _day(5), _day(20))
        except oe.RpcError:
            return
    raise AssertionError("events without a timestamp were put into days")


def test_subset_request_keeps_the_other_pools():
    with Sandbox():
        oe.extract_chain("testchain", POOLS, _day(5), _day(20), A)
        before = len(oe.load_events("node-a", "testchain", POOL_B))
        oe.extract_chain("testchain", POOLS[:1], _day(8), _day(12), A)          # pool A only
        after = len(oe.load_events("node-a", "testchain", POOL_B))
        still = oe.days_covered("node-a", "testchain", _day(5), _day(20), POOL_B)
        calls = FakeRpc.getlogs_calls
        oe.extract_chain("testchain", POOLS, _day(5), _day(20), A)              # everything again
        refetched = FakeRpc.getlogs_calls - calls
    assert before == after == 32 and still, "a subset request damaged another pool's cache"
    assert refetched == 0, "a fully cached range was read from the node again"


def test_extending_the_range_reads_only_the_gap():
    with Sandbox():
        oe.extract_chain("testchain", POOLS, _day(8), _day(12), A)
        calls = FakeRpc.getlogs_calls
        oe.extract_chain("testchain", POOLS, _day(8), _day(14), A)
        extra = FakeRpc.getlogs_calls - calls
        n = len(oe.load_events("node-a", "testchain", POOL_A))
    assert extra == 3, f"two more days are 14,400 blocks = 3 windows of 5,000, got {extra}"
    assert n == 14                                                # 7 days, 2 events each, no duplicates


def test_cross_check_passes_when_nodes_agree_and_names_what_differs():
    def drop(logs):
        return [log for log in logs if not (log["address"] == POOL_A and int(log["blockNumber"], 16) == 10 * PER_DAY)]

    def change_amount(logs):
        out = [dict(log) for log in logs]
        for log in out:
            if log["address"] == POOL_B and int(log["logIndex"], 16) == 1 and int(log["blockNumber"], 16) == 9 * PER_DAY + 100:
                log["data"] = log["data"][:-1] + "9"
        return out

    def move_timestamp(logs):
        out = [dict(log) for log in logs]
        for log in out:
            if int(log["blockNumber"], 16) == 11 * PER_DAY:
                log["blockTimestamp"] = hex(int(log["blockTimestamp"], 16) + 86400)
        return out

    for name, tamper, expect_problem in (("agree", None, False), ("missing event", drop, True),
                                         ("changed amount", change_amount, True),
                                         ("moved timestamp", move_timestamp, True)):
        with Sandbox():
            if tamper:
                FakeRpc.tamper = {"node-b": tamper}
            problems = oe.cross_check("testchain", POOLS, A, B, _day(8), _day(12))
        assert bool(problems) == expect_problem, f"{name}: {problems}"


def test_cross_check_ignores_only_the_timestamp_a_node_does_not_give():
    with Sandbox():
        FakeRpc.no_timestamps = {"node-b"}
        assert oe.cross_check("testchain", POOLS, A, B, _day(8), _day(12)) == []
    with Sandbox():
        FakeRpc.no_timestamps = {"node-b"}
        FakeRpc.tamper = {"node-b": lambda logs: logs[1:]}
        assert oe.cross_check("testchain", POOLS, A, B, _day(8), _day(12))


def test_extract_checked_fails_over_and_reports_an_unreadable_second_node():
    class Down(FakeRpc):
        def head(self):
            if self.name == "node-a":
                raise oe.RpcError("node-a: HTTP 503 after 8 attempts")
            return HEAD

    with Sandbox():
        oe.Rpc = Down
        res = oe.extract_checked("testchain", POOLS, _day(8), _day(12), [A, B], 5)
    assert res["provider"] == "node-b" and res["failed_over"], res
    assert res["problems"], "the cross-check cannot pass when the other node is down"


def test_token_symbols_and_usd_valuation():
    symbols = oe.token_symbols(POOLS)
    assert symbols[("testchain", USDC)] == "USDC" and symbols[("testchain", WETH)] == "WETH"
    same_decimals = [{"dataset": "s", "chain": "c", "label": "X USDC/USDT 0.01%", "pool_address": POOL_A,
                      "token0_address": USDC, "token0_decimals": 6, "token1_address": WETH, "token1_decimals": 6}]
    assert oe.token_symbols(same_decimals)[("c", USDC)] == "USDC"   # label order decides
    with Sandbox():
        oe.extract_chain("testchain", POOLS, _day(7), _day(7), A)
        events = oe.load_events("node-a", "testchain", POOL_A)
    day = _day(7).isoformat()
    prices = {"USDC": {day: 1.0}, "WETH": {day: 2000.0}}
    row = ov.valued_rows(POOLS[0], events, _day(7), _day(7), prices, symbols)[0]
    assert row["gross_lp_inflow_usd"] == 7.0 * 1.0 + 1.0 * 2000.0
    assert row["gross_lp_outflow_usd"] == 3.5
    assert row["net_lp_flow_usd"] == row["gross_lp_inflow_usd"] - row["gross_lp_outflow_usd"]
    assert (row["price_token0_usd"], row["price_token1_usd"]) == (1.0, 2000.0)


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001 — report every failing test, then exit non-zero
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
