"""Binance Vision bookTicker day (top of book, ~40M updates) -> 200 ms grid: last bid/ask/sizes at or before each grid instant.
usage: python 11_bookticker_grid.py YYYY-MM-DD"""
import sys, zipfile, numpy as np, pandas as pd
from datetime import datetime, timezone
day=sys.argv[1]; z=zipfile.ZipFile(f'../data/ext/bookTicker/BTCUSDT-bookTicker-{day}.zip')
t0=int(datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000); N=432000
parts=[]
with z.open(z.namelist()[0]) as f:
    for c in pd.read_csv(f,usecols=['best_bid_price','best_bid_qty','best_ask_price','best_ask_qty','transaction_time'],chunksize=4_000_000):
        c['k']=(c.transaction_time-t0)//200; parts.append(c.groupby('k').last())
b=pd.concat(parts).groupby(level=0).last()
b=b[(b.index>=0)&(b.index<N)].reindex(np.arange(N)).ffill().bfill()
# cell k holds the last update inside [k, k+1)*200 ms; shift by one so cell i is known at the START of i (no look-ahead)
arr={n:np.concatenate([[v[0]],v[:-1]]) for n,v in dict(bid=b.best_bid_price.values,ask=b.best_ask_price.values,bq=b.best_bid_qty.values,aq=b.best_ask_qty.values).items()}
np.savez_compressed(f'../data/ext/grid200_{day}.npz',**arr); print(day,'done',len(b),'crossed',(arr['ask']<=arr['bid']).sum())
