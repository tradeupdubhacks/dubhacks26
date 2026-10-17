"""Convert one Tardis day and replay a join-the-touch quote on Binance BTCUSDT.

Reported fills use the risk-averse queue: cancellations do not improve queue place.
Fees are Binance USD-M regular-user rates from the futures fee FAQ: 0.02% maker,
0.05% taker. Funding is applied afterwards from Tardis settlement rows, because the
replay engine does not book perpetual funding.
"""

import csv
import gzip
from pathlib import Path

import numpy as np
from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest, Recorder
from hftbacktest.data.utils import tardis

from bot import join_the_touch

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "data" / "out"
SYMBOL = "BTCUSDT"
STAMP = "20261001"
TICK_SIZE = 0.1
LOT_SIZE = 0.001
# Regular user, USD-M futures. https://www.binance.com/en/support/faq/detail/360033544231
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
# 10 ms entry and 10 ms response. Public-internet scale, not colocation.
ORDER_ENTRY_LATENCY_NS = 10_000_000
ORDER_RESPONSE_LATENCY_NS = 10_000_000


def raw_path(data_type: str) -> Path:
    return RAW / f"{SYMBOL}_{STAMP}_{data_type}.csv.gz"


def convert() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    destination = OUT / f"{SYMBOL}_{STAMP}.npz"
    if destination.exists():
        print(f"using {destination}")
        return destination
    print("converting trades, depth, and book ticker")
    tardis.convert_fuse(
        str(raw_path("trades")),
        str(raw_path("incremental_book_L2")),
        str(raw_path("book_ticker")),
        TICK_SIZE,
        LOT_SIZE,
        output_filename=str(destination),
        snapshot_mode="process",
    )
    print(f"wrote {destination}")
    return destination


def settlements(path: Path) -> list[tuple[int, float, float]]:
    """Last mark and rate at or before each funding time inside this file.

    Binance publishes the next funding timestamp on every derivative ticker update.
    An exact print at the funding instant is not always present, so the settlement
    used here is the last update whose timestamp is still at or before that instant.
    """
    latest: dict[int, tuple[int, float, float]] = {}
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            if not row["timestamp"] or not row["funding_timestamp"]:
                continue
            if not row["funding_rate"] or not row["mark_price"]:
                continue
            funding_us = int(row["funding_timestamp"])
            timestamp_us = int(row["timestamp"])
            if timestamp_us > funding_us:
                continue
            latest[funding_us] = (
                timestamp_us,
                float(row["funding_rate"]),
                float(row["mark_price"]),
            )
    return [
        (funding_us * 1000, rate, mark)
        for funding_us, (_timestamp_us, rate, mark) in sorted(latest.items())
    ]


def apply_funding(records: np.ndarray, funding_path: Path) -> float:
    """Cash from funding. A positive rate charges a long position."""
    position_at = records["timestamp"]
    positions = records["position"]
    cash = 0.0
    for timestamp_ns, rate, mark in settlements(funding_path):
        idx = np.searchsorted(position_at, timestamp_ns, side="right") - 1
        if idx < 0:
            position = 0.0
        elif timestamp_ns > position_at[-1]:
            continue
        else:
            position = positions[idx]
        payment = -position * mark * rate
        cash += payment
        print(
            f"funding {timestamp_ns} position={position:.4f} "
            f"rate={rate:.8f} mark={mark:.2f} cash={payment:.6f}"
        )
    return cash


def main() -> None:
    data_file = convert()
    asset = (
        BacktestAsset()
        .data([str(data_file)])
        .linear_asset(1.0)
        .constant_order_latency(ORDER_ENTRY_LATENCY_NS, ORDER_RESPONSE_LATENCY_NS)
        .risk_adverse_queue_model()
        .partial_fill_exchange()
        .trading_value_fee_model(MAKER_FEE, TAKER_FEE)
        .tick_size(TICK_SIZE)
        .lot_size(LOT_SIZE)
    )
    hbt = HashMapMarketDepthBacktest([asset])
    recorder = Recorder(1, 1_000_000)
    print("replaying")
    join_the_touch(hbt, recorder.recorder)
    hbt.close()

    records = recorder.get(0)
    last = records[-1]
    equity_before_fee = last["balance"] + last["position"] * last["price"]
    equity_after_fee = equity_before_fee - last["fee"]
    funding = apply_funding(records, raw_path("derivative_ticker"))
    print(f"trades {int(last['num_trades'])}")
    print(f"trading_value {last['trading_value']:.4f}")
    print(f"fees {last['fee']:.6f}")
    print(f"end_position {last['position']:.4f}")
    print(f"equity_after_fees {equity_after_fee:.6f}")
    print(f"funding_cash {funding:.6f}")
    print(f"equity_after_fees_and_funding {equity_after_fee + funding:.6f}")
    print("flat baseline equity 0")
    recorder.to_npz(str(OUT / f"{SYMBOL}_{STAMP}_record.npz"))


if __name__ == "__main__":
    main()
