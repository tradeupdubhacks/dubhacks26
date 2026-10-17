"""Download one free Tardis day of Binance USDS-M BTCUSDT.

The first UTC day of each month is public, with no API key.
https://docs.tardis.dev/faq/billing-and-subscriptions
"""

import urllib.request
from pathlib import Path

DATE = "2026-10-01"
SYMBOL = "BTCUSDT"
EXCHANGE = "binance-futures"
DATA_TYPES = (
    "trades",
    "incremental_book_L2",
    "book_ticker",
    "derivative_ticker",
)


def url_for(data_type: str) -> str:
    year, month, day = DATE.split("-")
    return (
        f"https://datasets.tardis.dev/v1/{EXCHANGE}/{data_type}/"
        f"{year}/{month}/{day}/{SYMBOL}.csv.gz"
    )


def download() -> None:
    out = Path(__file__).resolve().parent / "data" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    stamp = DATE.replace("-", "")
    for data_type in DATA_TYPES:
        dest = out / f"{SYMBOL}_{stamp}_{data_type}.csv.gz"
        if dest.exists() and dest.stat().st_size > 0:
            print(f"skip {dest.name} ({dest.stat().st_size} bytes)")
            continue
        print(f"downloading {data_type}")
        request = urllib.request.Request(url_for(data_type), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as handle:
            while True:
                chunk = response.read(8 * 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        print(f"wrote {dest.name} ({dest.stat().st_size} bytes)")


if __name__ == "__main__":
    download()
