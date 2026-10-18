"""Load Binance USDS-M 1-minute klines (data/ext/klines) into one compact npz per symbol on a gap-free minute grid."""
import glob, io, zipfile, numpy as np, pandas as pd
COLS=['t','o','h','l','c','v','ct','qv','n','tbv','tbqv','ig']
for sym in ['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']:
    parts=[]
    for f in sorted(glob.glob(f'../data/ext/klines/{sym}/*.zip')):
        with zipfile.ZipFile(f) as z:
            raw=z.read(z.namelist()[0])
        head=raw[:20]; skip=1 if head.startswith(b'open_time') else 0
        d=pd.read_csv(io.BytesIO(raw),header=None,skiprows=skip,names=COLS,usecols=['t','o','h','l','c','v','n','tbv','qv'])
        parts.append(d)
    d=pd.concat(parts).drop_duplicates('t').sort_values('t')
    t=d.t.values.astype(np.int64)
    if t[0]>1e14: t=t//1000                      # some archives use microseconds
    d['t']=t; step=60_000
    grid=np.arange(t[0],t[-1]+step,step); miss=len(grid)-len(d)
    d=d.set_index('t').reindex(grid)
    d['c']=d.c.ffill(); 
    for k in ['o','h','l']: d[k]=d[k].fillna(d.c)
    for k in ['v','n','tbv','qv']: d[k]=d[k].fillna(0)
    np.savez_compressed(f'klines_{sym}.npz',t=grid,**{k:d[k].values.astype(np.float64) for k in ['o','h','l','c','v','n','tbv','qv']})
    print(sym,pd.to_datetime(grid[[0,-1]],unit='ms').strftime('%Y-%m-%d').tolist(),len(grid),'bars; gap bars filled:',miss,'; px %.2f->%.2f'%(d.c.iloc[0],d.c.iloc[-1]))
