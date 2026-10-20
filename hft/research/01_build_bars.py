"""Aggregate raw tick data (trades, book ticker, derivative ticker) into 1-second bars."""
import pandas as pd, numpy as np, sys
RAW = '../data/raw/BTCUSDT_20261001_'
def sec(ts): return (ts // 1_000_000).astype('int64')

# trades
t = pd.read_csv(RAW+'trades.csv.gz', usecols=['timestamp','side','price','amount'])
print('trades', len(t))
t['s'] = sec(t.timestamp); t['sgn'] = np.where(t.side=='buy',1.0,-1.0)
t['q'] = t.amount*t.sgn; t['notional']=t.price*t.amount
g = t.groupby('s')
tb = pd.DataFrame({'vol':g.amount.sum(),'ntr':g.amount.size(),'signed_vol':g.q.sum(),
                   'last_px':g.price.last(),'vwap':g.notional.sum()/g.amount.sum()})

# book ticker in chunks
parts=[]
n=0
for c in pd.read_csv(RAW+'book_ticker.csv.gz', usecols=['timestamp','ask_amount','ask_price','bid_price','bid_amount'], chunksize=5_000_000):
    n+=len(c); c['s']=sec(c.timestamp)
    mid=(c.ask_price+c.bid_price)/2; c['mid']=mid; c['spr']=(c.ask_price-c.bid_price)/mid*1e4
    c['micro']=(c.ask_price*c.bid_amount+c.bid_price*c.ask_amount)/(c.bid_amount+c.ask_amount)
    c['imb']=(c.bid_amount-c.ask_amount)/(c.bid_amount+c.ask_amount)
    gg=c.groupby('s')
    parts.append(pd.DataFrame({'mid_open':gg.mid.first(),'mid_close':gg.mid.last(),'mid_hi':gg.mid.max(),'mid_lo':gg.mid.min(),
        'spr_bps':gg.spr.mean(),'micro':gg.micro.last(),'imb':gg.imb.mean(),'bid':gg.bid_price.last(),'ask':gg.ask_price.last(),
        'nquote':gg.mid.size(),'bid_amt':gg.bid_amount.last(),'ask_amt':gg.ask_amount.last()}))
    print('book rows',n,flush=True)
b = pd.concat(parts)
b = b.groupby(level=0).agg({'mid_open':'first','mid_close':'last','mid_hi':'max','mid_lo':'min','spr_bps':'mean','micro':'last','imb':'mean',
    'bid':'last','ask':'last','nquote':'sum','bid_amt':'last','ask_amt':'last'})

d = pd.read_csv(RAW+'derivative_ticker.csv.gz', usecols=['timestamp','funding_rate','open_interest','mark_price','index_price'])
d['s']=sec(d.timestamp); d=d.groupby('s').last().drop(columns='timestamp')

idx = pd.RangeIndex(b.index.min(), b.index.max()+1)
df = b.reindex(idx).join(tb.reindex(idx)).join(d.reindex(idx))
for c in ['mid_close','bid','ask','micro','bid_amt','ask_amt','funding_rate','open_interest','mark_price','index_price','last_px','vwap','spr_bps','imb']:
    df[c]=df[c].ffill()
for c in ['vol','ntr','signed_vol','nquote']: df[c]=df[c].fillna(0)
df.index.name='epoch_s'
df.to_parquet('bars_1s.parquet') if False else df.to_csv('bars_1s.csv.gz')
print(df.shape, pd.to_datetime(df.index[[0,-1]],unit='s'))
print(df[['mid_close','spr_bps','vol','ntr']].describe())
