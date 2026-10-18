"""Does trade-by-trade flow add to the 5 s signal? Leave-one-day-out, book-only vs book+trades."""
"""Can a better model/feature set raise the 5 s edge enough to matter? Leave-one-day-out over 11 days of BTC top of book.
A: current 9-feature net (baseline from 12_book_multiday.py).  B: gradient boosting on ~30 richer features (book history, queue changes,
multi-scale returns and vol).  Metric: rank correlation with the next 5 s move and the GROSS bps per trade at top confidence levels."""
import sys, glob, numpy as np, pandas as pd, warnings; warnings.filterwarnings('ignore')
from sklearn.ensemble import HistGradientBoostingRegressor
H=25
def load():
    d={}
    for f in sorted(glob.glob('../data/ext/grid200_*.npz')):
        z=np.load(f); d[f.split('grid200_')[1][:10]]=(z['bid'],z['ask'],z['bq'],z['aq'])
    g=np.load('../data/out/BTCUSDT_20261001_grid50ms.npz')
    d['2026-10-01']=(g['bid'][::4]/100.,g['ask'][::4]/100.,g['bidsz'][::4]*0.001,g['asksz'][::4]*0.001); return d
def lag(a,k): return np.concatenate([np.full(k,a[0]),a[:-k]])
def feats(bid,ask,bq,aq):
    mid=(bid+ask)/2; n=len(mid); lr=np.r_[0,np.diff(mid)/mid[:-1]]*1e4; cs=np.r_[0,np.cumsum(lr)]; c2=np.r_[0,np.cumsum(lr**2)]; i=np.arange(n)
    imb=(bq-aq)/(bq+aq); micro=(ask*bq+bid*aq)/(bq+aq); sp=(ask-bid)/mid*1e4
    F={'imb':imb,'micro':(micro/mid-1)*1e4,'spread':sp,'depth':np.log1p((bq+aq)*1000),'logbq':np.log1p(bq*1000),'logaq':np.log1p(aq*1000)}
    for k in (1,3,5,15,31,75,150): F[f'ret{k}']=cs[i+1]-cs[np.maximum(i+1-k,0)]
    for k in (31,150): lo=np.maximum(i+1-k,0); F[f'vol{k}']=np.sqrt((c2[i+1]-c2[lo])/np.maximum(i+1-lo,1))
    for k in (1,3,5,15): F[f'dimb{k}']=imb-lag(imb,k)
    for k in (1,5,15): F[f'dbq{k}']=np.log1p(bq*1000)-lag(np.log1p(bq*1000),k); F[f'daq{k}']=np.log1p(aq*1000)-lag(np.log1p(aq*1000),k)
    for k in (5,31): F[f'imbmean{k}']=pd.Series(imb).rolling(k,min_periods=1).mean().values
    F['bid_up5']=((bid-lag(bid,5))/mid)*1e4; F['ask_dn5']=((ask-lag(ask,5))/mid)*1e4
    F['mid_vs_micro_x_imb']=F['micro']*imb
    X=np.stack(list(F.values()),1).astype(np.float32); y=np.full(n,np.nan); y[:n-H]=(mid[H:]/mid[:n-H]-1)*1e4
    return X,y

def tfeats(day):
    z=np.load(f'../data/ext/trades200_{day}.npz'); buy,sell,cnt=[np.concatenate([[0.],a[:-1]]) for a in (z['buy'],z['sell'],z['n'].astype(float))]  # tick i sees bins < i
    cb,cs_,cc=[np.r_[0,np.cumsum(a)] for a in (buy,sell,cnt)]; i=np.arange(len(buy)); F=[]
    for k in (1,3,5,15,31,75,150):
        lo=np.maximum(i+1-k,0); b=cb[i+1]-cb[lo]; s=cs_[i+1]-cs_[lo]; c=cc[i+1]-cc[lo]
        F+=[(b-s)/(b+s+1e-9), np.log1p((b+s)*1000), np.log1p(c)]
    F+=[np.log1p(np.abs(buy-sell)*1000)*np.sign(buy-sell)]
    return np.stack(F,1).astype(np.float32)
raw=load(); D={}
for k,v in raw.items():
    X,y=feats(*v); D[k]=(X,np.concatenate([X,tfeats(k)],1),y)
names=sorted(D); print(len(names),'days; book features',D[names[0]][0].shape[1],'| + trade features',D[names[0]][1].shape[1])
rows=[]
for d in names:
    res={}
    for tag,idx in (('book',0),('book+trades',1)):
        Xtr=np.concatenate([D[x][idx][150:-H:4] for x in names if x!=d]); ytr=np.concatenate([D[x][2][150:-H:4] for x in names if x!=d])
        m=HistGradientBoostingRegressor(max_depth=5,max_iter=200,learning_rate=.05,l2_regularization=5,min_samples_leaf=200).fit(Xtr,np.clip(ytr,-15,15))
        X=D[d][idx]; y=D[d][2]; sl=slice(150,len(y)-H); p=m.predict(X[sl]); yy=y[sl]; c=np.abs(p)
        r=dict(day=d,model=tag,ic=np.corrcoef(p,yy)[0,1])
        for q in (0.9,0.99,0.999):
            t=c>=np.quantile(c,q); r[f'gross_q{q}']=(np.sign(p[t])*yy[t]).mean()
        rows.append(r)
    print(d,' '.join('%s ic %.3f g10 %.2f g1 %.2f'%(r['model'][:5],r['ic'],r['gross_q0.9'],r['gross_q0.99']) for r in rows[-2:]),flush=True)
r=pd.DataFrame(rows); r.to_csv('results/finetune_trades.csv',index=False)
print('\nmean over 11 days, leave-one-day-out:')
print(r.groupby('model')[['ic','gross_q0.9','gross_q0.99','gross_q0.999']].mean().round(3).to_string())
print('days where trades improve IC:',int((r[r.model=='book+trades'].ic.values>r[r.model=='book'].ic.values).sum()),'of',len(names))
print('cost to beat per round trip: maker 4.0 bps, taker 10-11 bps')
