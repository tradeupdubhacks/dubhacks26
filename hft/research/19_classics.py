"""Research stage E: a handful of classic, pre-specified ideas on BTC (and the other 5 coins as a robustness check).
Trials counted: T1 trend (24h, 7d, 30d lookbacks, long/short and long-only), T2 hour-of-day drift (selected on earlier years), T3 day-of-week drift.
All trades: enter at the next bar open, hold 24 h (or the stated hold), cost 4 bps maker / 11 bps taker per round trip."""
import numpy as np, pandas as pd, warnings; warnings.filterwarnings('ignore')
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']
def daily(s):
    d=np.load(f'klines_{s}.npz'); t,o,c=d['t'],d['o'],d['c']
    day0=np.flatnonzero((t%86_400_000)==0); day0=day0[(day0+1441<len(c))&(day0>=60*1440)]
    ent=o[day0+1]; ext=c[day0+1440]; fwd=np.log(ext/ent)*1e4
    past={k:np.log(c[day0]/c[day0-1440*k])*1e4 for k in (1,7,30)}
    ma200=np.array([c[i-200*1440:i:1440].mean() for i in day0]) if False else None
    yr=pd.to_datetime(t[day0],unit='ms').year.values; dow=pd.to_datetime(t[day0],unit='ms').dayofweek.values
    return pd.DataFrame(dict(year=yr,dow=dow,fwd=fwd,**{f'p{k}':v for k,v in past.items()}))
R=[]
for s in SYMS:
    D=daily(s)
    for k in (1,7,30):
        for name,pos in (('long/short',np.sign(D[f'p{k}'])),('long-only',(D[f'p{k}']>0).astype(float))):
            g=pos*D.fwd; tr=np.abs(pos)>0
            D[f'x{k}{name}']=g-4*tr
            for y,gy in D.assign(g=g,tr=tr).groupby('year'):
                if y>=2021: R.append(dict(sym=s,trial=f'trend {k}d {name}',year=y,days=len(gy),gross=gy.g.mean(),net_maker=(gy.g-4*gy.tr).mean(),net_taker=(gy.g-11*gy.tr).mean(),bh=gy.fwd.mean()))
r=pd.DataFrame(R); pd.set_option('display.width',200)
b=r[r.sym=='BTCUSDT']
print('BTC, mean net bps per DAY after maker round trip (4 bps), by year; bh = buy and hold (24 h):')
print(b.pivot(index='trial',columns='year',values='net_maker').round(1).join(b.groupby('trial').net_maker.mean().rename('avg').round(1)).to_string())
print('\nBTC buy&hold per day by year:',b.groupby('year').bh.mean().round(1).to_dict())
print('\nAll 6 coins pooled (mean of coin means), net maker, by trial:'); print(r.groupby(['trial','year']).net_maker.mean().unstack().round(1).to_string())
# hour-of-day drift: select the best 6 UTC hours on years < Y, hold 1 h each, test on Y
d=np.load('klines_BTCUSDT.npz'); t,o,c=d['t'],d['o'],d['c']; hrs=((t//3_600_000)%24); idx=np.flatnonzero((t%3_600_000)==0); idx=idx[idx+61<len(c)]
h1=np.log(c[idx+59]/o[idx+1])*1e4; hh=hrs[idx]; yy=pd.to_datetime(t[idx],unit='ms').year.values
print('\nhour-of-day drift (BTC, 1-hour holds). Walk-forward: choose 3 best and 3 worst UTC hours on earlier years, long the best / short the worst on test year')
for Y in range(2022,2027):
    m=pd.Series(h1[yy<Y]).groupby(hh[yy<Y]).mean(); best=m.nlargest(3).index; worst=m.nsmallest(3).index
    te=yy==Y; g=np.where(np.isin(hh[te],best),h1[te],np.where(np.isin(hh[te],worst),-h1[te],np.nan)); g=g[~np.isnan(g)]
    print(Y,'trades',len(g),'gross %.2f bps  net maker %.2f  t-stat of net %.1f'%(g.mean(),g.mean()-4,(g.mean()-4)/(g.std()/np.sqrt(len(g)))))
# day-of-week: long the 2 best weekdays (earlier years), 24h holds
print('\nday-of-week drift (BTC, 24 h): long the 2 best / short the 2 worst weekdays chosen on earlier years')
D=daily('BTCUSDT')
for Y in range(2022,2027):
    m=D[D.year<Y].groupby('dow').fwd.mean(); best=m.nlargest(2).index; worst=m.nsmallest(2).index; te=D[D.year==Y]
    g=np.where(te.dow.isin(best),te.fwd,np.where(te.dow.isin(worst),-te.fwd,np.nan)); g=g[~np.isnan(g)]
    print(Y,'trades',len(g),'gross %.1f bps  net maker %.1f  t-stat %.1f'%(g.mean(),g.mean()-4,(g.mean()-4)/(g.std()/np.sqrt(len(g)))))
