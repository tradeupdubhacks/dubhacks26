"""Download extra public Binance USDS-M data from data.binance.vision (no API key).

    python download_extra.py            # klines for 6 symbols + one BTCUSDT top-of-book day per month
    python download_extra.py klines     # only the 1-minute klines (~1 GB)

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


if __name__ == "__main__":
    items = klines() + ([] if sys.argv[1:] == ["klines"] else book_tickers())
    with ThreadPoolExecutor(6) as pool:
        for line in pool.map(job, items):
            print(line, flush=True)
