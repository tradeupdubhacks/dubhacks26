"""Rebuild the model-API BTC replay (src/tape.ts) on a 50 ms grid.

    python -m model.tape_grid

Market time 0 is the first book_ticker timestamp. Cell i holds the book and last trade as of time i * 50 ms
(the last row with timestamp <= that instant), so a cell never contains later information.
Only top of book exists in this tape, as in the server.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from model.backtest import GRID

RAW = GRID.parent.parent / "raw" / "BTCUSDT_20261001_"
CELL_US = 50_000
CELLS = 86_400_000 // 50


def build() -> None:
    parts, origin = [], None
    for chunk in pd.read_csv(str(RAW) + "book_ticker.csv.gz", chunksize=5_000_000,
                             usecols=["timestamp", "ask_amount", "ask_price", "bid_price", "bid_amount"]):
        if origin is None:
            origin = int(chunk.timestamp.iloc[0])
        chunk["k"] = (chunk.timestamp - origin) // CELL_US          # cell k covers [k, k+1) * 50 ms
        parts.append(chunk.groupby("k").last())
    book = pd.concat(parts).groupby(level=0).last()
    book = book[(book.index >= 0) & (book.index < CELLS)].reindex(np.arange(CELLS)).ffill().bfill()
    trades = pd.read_csv(str(RAW) + "trades.csv.gz", usecols=["timestamp", "side", "price", "amount"])
    trades["k"] = (trades.timestamp - origin) // CELL_US
    trades = trades[trades.k >= 0].groupby("k").last().reindex(np.arange(CELLS)).ffill()

    def asof(values: np.ndarray, fill) -> np.ndarray:
        """Cell i takes what was known at the start of the cell, i.e. the last row of cell i - 1."""
        out = np.empty_like(values)
        out[1:], out[0] = values[:-1], fill
        return out

    arrays = dict(
        bid=np.round(book.bid_price.values * 100).astype(np.int64),
        ask=np.round(book.ask_price.values * 100).astype(np.int64),
        bidsz=np.round(book.bid_amount.values / 0.001).astype(np.int64),
        asksz=np.round(book.ask_amount.values / 0.001).astype(np.int64),
        tpx=np.round(trades.price.fillna(0).values * 100).astype(np.int64),
        tsz=np.round(trades.amount.fillna(0).values / 0.001).astype(np.int64),
        tside=(trades.side.fillna("buy") == "buy").values.astype(np.int8),
    )
    arrays = {name: asof(values, values[0]) for name, values in arrays.items()}
    GRID.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(GRID, **arrays)
    print("wrote", GRID, CELLS, "cells; crossed quotes:", int((arrays["ask"] <= arrays["bid"]).sum()))


if __name__ == "__main__":
    build()
