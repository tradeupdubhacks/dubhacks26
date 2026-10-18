"""Bin trade-by-trade data (Binance aggTrades; Tardis trades for 2026-10-01) to the same 200 ms ticks as the book grids.
Bin k holds trades with time in [k, k+1) * 200 ms from the grid origin; the feature step shifts by one so a tick sees only earlier trades."""
import glob, io, zipfile, gzip, numpy as np, pandas as pd
from datetime import datetime, timezone
N=432000
def bins(k, qty, buy):
    k=np.asarray(k); m=(k>=0)&(k<N); k,qty,buy=k[m],np.asarray(qty)[m],np.asarray(buy)[m]
    b=np.bincount(k[buy],weights=qty[buy],minlength=N)[:N]; s=np.bincount(k[~buy],weights=qty[~buy],minlength=N)[:N]
    n=np.bincount(k,minlength=N)[:N]; return b,s,n
for f in sorted(glob.glob('../data/ext/aggTrades/*.zip')):
    day=f.split('aggTrades-')[1][:10]; t0=int(datetime.strptime(day,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000)
    with zipfile.ZipFile(f) as z: d=pd.read_csv(io.BytesIO(z.read(z.namelist()[0])),usecols=['quantity','transact_time','is_buyer_maker'])
    k=((d.transact_time.values-t0)//200); buy=(~d.is_buyer_maker.astype(str).str.lower().eq('true').values)
    b,s,n=bins(k,d.quantity.values,buy); np.savez_compressed(f'../data/ext/trades200_{day}.npz',buy=b,sell=s,n=n)
    print(day,len(d),'trades; buy share %.3f'%(b.sum()/(b.sum()+s.sum())))
# 2026-10-01: Tardis trades; origin = first book_ticker timestamp (what the server replays from)
origin=int(pd.read_csv('../data/raw/BTCUSDT_20261001_book_ticker.csv.gz',usecols=['timestamp'],nrows=1).timestamp.iloc[0])
d=pd.read_csv('../data/raw/BTCUSDT_20261001_trades.csv.gz',usecols=['timestamp','side','amount'])
b,s,n=bins((d.timestamp.values-origin)//200_000,d.amount.values,(d.side.values=='buy'))
np.savez_compressed('../data/ext/trades200_2026-10-01.npz',buy=b,sell=s,n=n); print('2026-10-01',len(d),'trades; buy share %.3f'%(b.sum()/(b.sum()+s.sum())))
