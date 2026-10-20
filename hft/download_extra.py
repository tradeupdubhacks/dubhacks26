"""Download extra public Binance USDS-M data from data.binance.vision (no API key).

    python download_extra.py            # klines for 6 symbols + one BTCUSDT top-of-book day per month
    python download_extra.py klines     # only the 1-minute klines (~1 GB)
    python download_extra.py aggtrades  # trade-by-trade data (aggTrades) for the same 10 top-of-book days (~0.4 GB)
    python download_extra.py coins      # daily metrics (open interest, long/short ratios) for ETH, SOL, BNB, XRP, DOGE (~0.1 GB)
    python download_extra.py research   # BTCUSDT metrics (open interest, long/short ratios), bookDepth, basis, funding, spot klines (~1 GB)

Files go to data/ext/ (git-ignored) and are checked against the published SHA-256 .CHECKSUM files.
Top-of-book (bookTicker) is only published from 2023-05 to 2024-04; each day is ~90 MB zipped.
"""

import hashlib
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://data.binance.vision/data/futures/um"
OUT = Path(__file__).resolve().parent / "data" / "ext"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
FIRST = {"SOLUSDT": (2020, 9), "DOGEUSDT": (2020, 7), "BNBUSDT": (2020, 2)}
LAST = (2026, 9)
BOOK_DAYS = [f"{y}-{m:02d}-15" for y, m in [(2023, 6), (2023, 7), (2023, 8), (2023, 9), (2023, 10), (2023, 11),
                                            (2023, 12), (2024, 1), (2024, 2), (2024, 3)]]


def fetch(url: str, dest: Path) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
            while chunk := response.read(4 * 1024 * 1024):
                handle.write(chunk)
    except urllib.error.HTTPError as error:
        dest.unlink(missing_ok=True)
        return f"missing ({error.code})"
    return "ok"


def verified(url: str, dest: Path) -> bool:
    try:
        text = urllib.request.urlopen(urllib.request.Request(url + ".CHECKSUM", headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read().decode()
    except urllib.error.URLError:
        return False
    expected = text.split()[0]
    digest = hashlib.sha256()
    with dest.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == expected


def job(item: tuple[str, Path]) -> str:
    url, dest = item
    if dest.exists() and dest.stat().st_size > 0:
        return f"skip {dest.name}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    status = fetch(url, dest)
    if status != "ok":
        return f"{dest.name}: {status}"
    if not verified(url, dest):
        dest.unlink()
        return f"{dest.name}: checksum mismatch, removed"
    return f"ok {dest.name} {dest.stat().st_size // 1024} KB"


def klines() -> list[tuple[str, Path]]:
    items = []
    for symbol in SYMBOLS:
        y, m = FIRST.get(symbol, (2020, 1))
        while (y, m) <= LAST:
            name = f"{symbol}-1m-{y}-{m:02d}.zip"
            items.append((f"{BASE}/monthly/klines/{symbol}/1m/{name}", OUT / "klines" / symbol / name))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return items


def book_tickers() -> list[tuple[str, Path]]:
    return [(f"{BASE}/daily/bookTicker/BTCUSDT/BTCUSDT-bookTicker-{d}.zip",
             OUT / "bookTicker" / f"BTCUSDT-bookTicker-{d}.zip") for d in BOOK_DAYS]


def agg_trades() -> list[tuple[str, Path]]:
    return [(f"{BASE}/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{d}.zip",
             OUT / "aggTrades" / f"BTCUSDT-aggTrades-{d}.zip") for d in BOOK_DAYS]


def research_extras() -> list[tuple[str, Path]]:
    """Information that is not in the price series: positioning, depth, basis, funding, spot."""
    from datetime import date, timedelta
    items = []
    day, end = date(2020, 9, 1), date(2026, 10, 15)
    while day <= end:
        d = day.isoformat()
        items.append((f"{BASE}/daily/metrics/BTCUSDT/BTCUSDT-metrics-{d}.zip", OUT / "metrics" / f"BTCUSDT-metrics-{d}.zip"))
        if day >= date(2023, 1, 1):
            items.append((f"{BASE}/daily/bookDepth/BTCUSDT/BTCUSDT-bookDepth-{d}.zip", OUT / "bookDepth" / f"BTCUSDT-bookDepth-{d}.zip"))
        day += timedelta(days=1)
    y, m = 2020, 1
    while (y, m) <= LAST:
        ym = f"{y}-{m:02d}"
        items.append((f"{BASE}/monthly/premiumIndexKlines/BTCUSDT/1m/BTCUSDT-1m-{ym}.zip", OUT / "premium" / f"BTCUSDT-1m-{ym}.zip"))
        items.append((f"{BASE}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-{ym}.zip", OUT / "funding" / f"BTCUSDT-fundingRate-{ym}.zip"))
        items.append((f"https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-{ym}.zip", OUT / "spot" / f"BTCUSDT-1m-{ym}.zip"))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return items


def coin_metrics() -> list[tuple[str, Path]]:
    from datetime import date, timedelta
    items = []
    for symbol in SYMBOLS[1:]:
        day = date(*FIRST.get(symbol, (2020, 9))[:2], 1) if symbol in FIRST else date(2020, 9, 1)
        while day <= date(2026, 10, 15):
            d = day.isoformat()
            items.append((f"{BASE}/daily/metrics/{symbol}/{symbol}-metrics-{d}.zip", OUT / "metrics_coins" / symbol / f"{symbol}-metrics-{d}.zip"))
            day += timedelta(days=1)
    return items


if __name__ == "__main__":
    if sys.argv[1:] == ["coins"]:
        items = coin_metrics()
    elif sys.argv[1:] == ["research"]:
        items = research_extras()
    elif sys.argv[1:] == ["aggtrades"]:
        items = agg_trades()
    else:
        items = klines() + ([] if sys.argv[1:] == ["klines"] else book_tickers())
    with ThreadPoolExecutor(8) as pool:
        for line in pool.map(job, items):
            print(line, flush=True)
