"""Research stage C: extreme-move events (momentum or reversal) on 1-minute klines, 6 coins, 2020-2026.
Decision every 5 min at a bar close; entry at the NEXT bar's open (1 bar latency); exit at the close H bars later; non-overlapping per coin.
Candidates (120): signal window {15,60,240} min x |z| threshold {1.5,2,2.5,3,4} x {momentum, reversal} x hold {15,60,240,1440} min.
Walk-forward: for test year Y pick the candidate with the best t-stat of NET-OF-MAKER-FEE edge on years < Y (>=200 pooled events), report it on Y."""
import numpy as np, pandas as pd, warnings; warnings.filterwarnings('ignore')
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']; WIN=(15,60,240); ZS=(1.5,2,2.5,3,4); HOLDS=(15,60,240,1440)
def cs(x): return np.concatenate([[0.],np.cumsum(x)])
ev=[]   # rows: sym, year, win, z, H, side(+1 momentum/-1 reversal), gross_bps
for s in SYMS:
    d=np.load(f'klines_{s}.npz'); c,o,t=d['c'],d['o'],d['t']; n=len(c)
    lr=np.r_[0,np.diff(np.log(c))]; C2=cs(lr**2); C1=cs(lr)
    idx=np.flatnonzero(((t//60000)%5==0)); idx=idx[(idx>=1500)&(idx<n-1500-1)]
    sig=np.sqrt(np.maximum((C2[idx+1]-C2[idx+1-1440])/1440,1e-14)); yr=pd.to_datetime(t[idx],unit='ms').year.values
    for w in WIN:
        z=(C1[idx+1]-C1[idx+1-w])/(sig*np.sqrt(w))
        for zt in ZS:
            hit=np.flatnonzero(np.abs(z)>=zt)
            for H in HOLDS:
                last=-10**9; sel=[]
                for h in hit:                    # greedy non-overlap
                    if idx[h]-last>=H: sel.append(h); last=idx[h]
                sel=np.array(sel,dtype=int)
                if len(sel)==0: continue
                m=idx[sel]; fwd=np.log(c[m+H]/o[m+1])*1e4; mom=np.sign(z[sel])*fwd
                ev.append(pd.DataFrame(dict(sym=s,year=yr[sel],win=w,z=zt,H=H,mom=mom)))
    print(s,'done',flush=True)
E=pd.concat(ev); E.to_pickle('results/events_raw.pkl')
g=E.groupby(['win','z','H','year']).agg(n=('mom','size'),mom=('mom','mean')).reset_index()
rows=[]
for Y in range(2022,2027):
    best=None
    for (w,zt,H),gg in E[E.year<Y].groupby(['win','z','H']):
        if len(gg)<200: continue
        for side,name in ((1,'momentum'),(-1,'reversal')):
            x=side*gg.mom.values-4.0; tstat=x.mean()/(x.std()/np.sqrt(len(x))+1e-9)
            if best is None or tstat>best[0]: best=(tstat,w,zt,H,side,name,x.mean(),len(x))
    tstat,w,zt,H,side,name,trmean,ntr=best
    te=E[(E.year==Y)&(E.win==w)&(E.z==zt)&(E.H==H)]; gross=side*te.mom.values
    btc=te[te.sym=='BTCUSDT']; gb=side*btc.mom.values
    rows.append(dict(test_year=Y,rule=f'{name} win{w}m |z|>={zt} hold{H}m',train_net_maker=round(trmean,2),train_events=ntr,test_events=len(te),
        test_gross=round(gross.mean(),2),test_net_maker=round(gross.mean()-4,2),test_net_taker=round(gross.mean()-11,2),btc_events=len(btc),btc_gross=round(gb.mean(),2) if len(gb) else np.nan))
r=pd.DataFrame(rows); pd.set_option('display.width',250); pd.set_option('display.max_colwidth',40); print(r.to_string(index=False)); r.to_csv('results/events_walkforward.csv',index=False)
# descriptive: the cleanest single claims, pooled over all years (NOT used for selection)
print('\nPooled across years, 6 coins: mean gross bps after a |z|>=3 move on the 60-min window (+ = momentum):')
print(g[(g.win==60)&(g.z==3)].groupby('H').apply(lambda x:pd.Series(dict(events=int(x.n.sum()),momentum_gross=round((x.mom*x.n).sum()/x.n.sum(),2)))).to_string())
