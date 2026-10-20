"""Rebuild the model-API BTC replay (src/tape.ts) on a 50 ms grid.
Market time 0 = first book_ticker timestamp. Book = last quote at or before the grid time (cents / lots),
lastTrade = last trade at or before the grid time. Only top of book exists in this tape, as in the server."""
import numpy as np, pandas as pd
RAW='../data/raw/BTCUSDT_20261001_'; B=50_000   # 50 ms in microseconds
parts=[]; origin=None
for c in pd.read_csv(RAW+'book_ticker.csv.gz',usecols=['timestamp','ask_amount','ask_price','bid_price','bid_amount'],chunksize=5_000_000):
    if origin is None: origin=int(c.timestamp.iloc[0])
    c['k']=(c.timestamp-origin)//B
    parts.append(c.groupby('k').last())
bk=pd.concat(parts).groupby(level=0).last()
n=int(86_400_000//50)
bk=bk[(bk.index>=0)&(bk.index<n)].reindex(np.arange(n)).ffill().bfill()
t=pd.read_csv(RAW+'trades.csv.gz',usecols=['timestamp','side','price','amount']); t['k']=(t.timestamp-origin)//B
t=t[t.k>=0].groupby('k').last().reindex(np.arange(n)).ffill()
out=dict(bid=np.round(bk.bid_price.values*100).astype(np.int64),ask=np.round(bk.ask_price.values*100).astype(np.int64),
  bidsz=np.round(bk.bid_amount.values/0.001).astype(np.int64),asksz=np.round(bk.ask_amount.values/0.001).astype(np.int64),
  tpx=np.round(t.price.fillna(0).values*100).astype(np.int64),tsz=np.round(t.amount.fillna(0).values/0.001).astype(np.int64),
  tside=(t.side.fillna('buy')=='buy').values.astype(np.int8))
np.savez_compressed('tape_50ms.npz',**out); print(n,{k:v[:3] for k,v in out.items()}, 'crossed:',(out['ask']<=out['bid']).sum())
