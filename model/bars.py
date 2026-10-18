"""Build 200ms bars from the local Tardis day and from Binance aggTrades.

Binance does not publish 2026 bookTicker files, and Tardis now answers 404.
aggTrades for dates before the held-out slice are the extra days. Those days
have trade prices and aggressor side, and no top-of-book sizes.
"""

from __future__ import annotations

import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

BAR_MS = 200
DAY_MS = 86_400_000
BARS = DAY_MS // BAR_MS
CACHE_VERSION = 1

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "hft" / "data" / "raw"
EXTRA = ROOT / "hft" / "data" / "extra"
TARDIS_BOOK = RAW / "BTCUSDT_20261001_book_ticker.csv.gz"
TARDIS_TRADES = RAW / "BTCUSDT_20261001_trades.csv.gz"

# September 2026 is entirely before the 2026-10-01 holdout in docs/model-issues.md.
SEPTEMBER = tuple(f"2026-09-{day:02d}" for day in range(1, 31))


def agg_url(date: str) -> str:
    return (
        "https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT/"
        f"BTCUSDT-aggTrades-{date}.zip"
    )


def agg_path(date: str) -> Path:
    return EXTRA / f"BTCUSDT-aggTrades-{date}.zip"


def ensure_agg_days(dates: tuple[str, ...] = SEPTEMBER) -> list[Path]:
    EXTRA.mkdir(parents=True, exist_ok=True)
    missing = [date for date in dates if not (agg_path(date).exists() and agg_path(date).stat().st_size > 100_000)]

    def fetch(date: str) -> str:
        dest = agg_path(date)
        try:
            urllib.request.urlretrieve(agg_url(date), dest)
        except Exception as error:
            dest.unlink(missing_ok=True)
            return f"FAIL {date} {error}"
        return f"OK {date} {dest.stat().st_size}"

    if missing:
        print(f"downloading {len(missing)} Binance aggTrades days")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for result in as_completed([pool.submit(fetch, date) for date in missing]):
                print(result.result())
    ready = [agg_path(date) for date in dates if agg_path(date).exists() and agg_path(date).stat().st_size > 100_000]
    print(f"aggTrades days on disk: {len(ready)}")
    return ready


def _cache_path(key: str) -> Path:
    path = EXTRA / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{key}.v{CACHE_VERSION}.npz"


def _ffill(values: np.ndarray, seen: np.ndarray) -> np.ndarray:
    index = np.where(seen, np.arange(values.shape[0]), 0)
    np.maximum.accumulate(index, out=index)
    return values[index]


def _empty() -> dict[str, np.ndarray]:
    return {
        "bid": np.zeros(BARS, dtype=np.int32),
        "ask": np.zeros(BARS, dtype=np.int32),
        "bid_sz": np.zeros(BARS, dtype=np.int32),
        "ask_sz": np.zeros(BARS, dtype=np.int32),
        "mid": np.zeros(BARS, dtype=np.int32),
        "trade_sign": np.zeros(BARS, dtype=np.int8),
        "trade_sz": np.zeros(BARS, dtype=np.int32),
        "book": np.zeros(BARS, dtype=np.int8),
        "seen": np.zeros(BARS, dtype=np.bool_),
    }


def _finalize(raw: dict[str, np.ndarray], has_book: bool) -> dict[str, np.ndarray]:
    seen = raw["seen"]
    if not seen.any():
        raise ValueError("day has no prices")
    mid = _ffill(raw["mid"], seen)
    if has_book:
        bid = _ffill(raw["bid"], seen)
        ask = _ffill(raw["ask"], seen)
        bid_sz = _ffill(raw["bid_sz"], seen)
        ask_sz = _ffill(raw["ask_sz"], seen)
        book = _ffill(raw["book"].astype(np.int32), seen).astype(np.int8)
    else:
        bid = mid.copy()
        ask = mid.copy()
        bid_sz = np.zeros(BARS, dtype=np.int32)
        ask_sz = np.zeros(BARS, dtype=np.int32)
        book = np.zeros(BARS, dtype=np.int8)
    first = int(np.argmax(seen))
    mid[:first] = 0
    bid[:first] = 0
    ask[:first] = 0
    return {
        "bid": bid,
        "ask": ask,
        "bid_sz": bid_sz,
        "ask_sz": ask_sz,
        "mid": mid,
        "trade_sign": raw["trade_sign"],
        "trade_sz": raw["trade_sz"],
        "book": book,
        "day_start_ms": np.asarray([raw["day_start_ms"]], dtype=np.int64),
    }


def _save(key: str, bars: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    np.savez_compressed(_cache_path(key), **bars)
    return bars


def _load_cache(key: str) -> dict[str, np.ndarray] | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    loaded = np.load(path)
    return {name: loaded[name] for name in loaded.files}


def load_tardis_day() -> dict[str, np.ndarray]:
    cached = _load_cache("2026-10-01")
    if cached is not None:
        print("tape bars 2026-10-01 from cache")
        return cached
    if not TARDIS_BOOK.exists() or not TARDIS_TRADES.exists():
        raise SystemExit(f"Missing {TARDIS_BOOK.name} or {TARDIS_TRADES.name}")
    import gzip

    print("parsing 2026-10-01 book ticker and trades")
    raw = _empty()
    day_start_ms = 0
    with gzip.open(TARDIS_BOOK, "rt", newline="") as handle:
        next(handle)
        for line in handle:
            parts = line.split(",")
            if len(parts) < 8:
                continue
            ts_us = int(parts[2])
            if day_start_ms == 0:
                day_start_ms = (ts_us // 1_000_000 // 86_400) * 86_400 * 1_000
                raw["day_start_ms"] = day_start_ms
            index = (ts_us // 1_000 - day_start_ms) // BAR_MS
            if index < 0 or index >= BARS:
                continue
            ask = int(round(float(parts[5]) * 100))
            bid = int(round(float(parts[6]) * 100))
            if bid <= 0 or ask <= bid:
                continue
            raw["bid"][index] = bid
            raw["ask"][index] = ask
            raw["bid_sz"][index] = max(0, int(round(float(parts[7]) * 1000)))
            raw["ask_sz"][index] = max(0, int(round(float(parts[4]) * 1000)))
            raw["mid"][index] = (bid + ask) // 2
            raw["book"][index] = 1
            raw["seen"][index] = True
    with gzip.open(TARDIS_TRADES, "rt", newline="") as handle:
        next(handle)
        for line in handle:
            parts = line.split(",")
            if len(parts) < 8:
                continue
            ts_us = int(parts[2])
            index = (ts_us // 1_000 - day_start_ms) // BAR_MS
            if index < 0 or index >= BARS:
                continue
            side = parts[5]
            if side not in ("buy", "sell"):
                continue
            raw["trade_sign"][index] = 1 if side == "buy" else -1
            raw["trade_sz"][index] = max(0, int(round(float(parts[7]) * 1000)))
    print(f"tape bars with a book: {int(raw['seen'].sum())}")
    return _save("2026-10-01", _finalize(raw, has_book=True))


def load_agg_day(date: str) -> dict[str, np.ndarray] | None:
    cached = _load_cache(date)
    if cached is not None:
        return cached
    path = agg_path(date)
    if not path.exists():
        return None
    print(f"parsing aggTrades {date}")
    raw = _empty()
    day_start_ms = 0
    with zipfile.ZipFile(path) as archive:
        name = archive.namelist()[0]
        with archive.open(name) as handle:
            first = True
            for line in handle:
                if first:
                    first = False
                    if line.startswith(b"agg_trade_id"):
                        continue
                parts = line.split(b",")
                if len(parts) < 7:
                    continue
                ts_ms = int(parts[5])
                if day_start_ms == 0:
                    day_start_ms = (ts_ms // DAY_MS) * DAY_MS
                    raw["day_start_ms"] = day_start_ms
                index = (ts_ms - day_start_ms) // BAR_MS
                if index < 0 or index >= BARS:
                    continue
                price = int(round(float(parts[1]) * 100))
                if price <= 0:
                    continue
                buyer_maker = parts[6].strip().lower().startswith(b"t")
                raw["mid"][index] = price
                raw["seen"][index] = True
                raw["trade_sign"][index] = -1 if buyer_maker else 1
                raw["trade_sz"][index] = max(0, int(round(float(parts[2]) * 1000)))
    if "day_start_ms" not in raw:
        print(f"no rows in {date}")
        return None
    return _save(date, _finalize(raw, has_book=False))
