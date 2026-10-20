"""Does the 5-second top-of-book signal hold on other days? 10 Binance days (2023-06..2024-03) + the 2026-10-01 replay day.
Features = the session model's book features without the last-trade ones (Binance bookTicker has no trades): 9 inputs.
(A) transfer: train on 2026-10-01, test each other day.  (B) leave-one-day-out over all 11 days."""
import sys, glob, numpy as np, pandas as pd, torch
sys.path.insert(0,'../..')
from model.train_session import fit, score
H=25; dev=torch.device('cpu')
def load():
    days={}
    for f in sorted(glob.glob('../data/ext/grid200_*.npz')):
        z=np.load(f); days[f.split('grid200_')[1][:10]]=(z['bid'],z['ask'],z['bq'],z['aq'])
    g=np.load('../data/out/BTCUSDT_20261001_grid50ms.npz')
    days['2026-10-01']=(g['bid'][::4]/100.,g['ask'][::4]/100.,g['bidsz'][::4]*0.001,g['asksz'][::4]*0.001)
    return days
def feats(bid,ask,bq,aq):
    mid=(bid+ask)/2; n=len(mid); lr=np.r_[0,np.diff(mid)/mid[:-1]]*1e4
    F=[(bq-aq)/(bq+aq)]; micro=(ask*bq+bid*aq)/(bq+aq); F.append((micro/mid-1)*1e4); F.append((ask-bid)/mid*1e4)
    cs=np.r_[0,np.cumsum(lr)]; c2=np.r_[0,np.cumsum(lr**2)]; i=np.arange(n)
    for k in (1,5,15,31): F.append(cs[i+1]-cs[np.maximum(i+1-k,0)])
    lo=np.maximum(i+1-31,0); F.append(np.sqrt((c2[i+1]-c2[lo])/np.maximum(i+1-lo,1)))
    F.append(np.log1p((bq+aq)*1000))
    X=np.stack(F,1).astype(np.float32); y=np.full(n,np.nan); y[:n-H]=(mid[H:]/mid[:n-H]-1)*1e4
    return X,y,mid
D={d:feats(*v) for d,v in load().items()}; names=sorted(D); print('days',names)
def train(train_days):
    X=np.concatenate([D[d][0][32:-H] for d in train_days]); y=np.concatenate([D[d][1][32:-H] for d in train_days])
    k=y!=0; m,s=X[k].mean(0),X[k].std(0)+1e-9
    net=fit(np.clip((X[k]-m)/s,-5,5).astype(np.float32),(y[k]>0).astype(np.float32),dev,epochs=6); return net,m,s
def evaluate(net,m,s,day):
    X,y,mid=D[day]; p=score(net,np.clip((X-m)/s,-5,5).astype(np.float32),dev)-.5; sl=slice(32,len(y)-H); p,yy=p[sl],y[sl]
    c=np.abs(p); out=dict(day=day,ic=np.corrcoef(p,yy)[0,1],absmove=np.abs(yy).mean(),zero_share=(yy==0).mean())
    for q in (0.0,0.9,0.99):
        t=c>=np.quantile(c,q); g=(np.sign(p)[t]*yy[t]); out[f'gross_q{q}']=g.mean(); out[f'win_q{q}']=(g>0).mean()
    # non-overlapping 5 s trades, top 10% confidence, round-trip costs
    take=np.arange(0,len(yy),H); t=take[c[take]>=np.quantile(c,.9)]; g=np.sign(p)[t]*yy[t]
    out['net_taker']=(g-11).mean(); out['net_maker']=(g-4).mean(); out['n_trades']=len(t); return out
rows=[]
net,m,s=train(['2026-10-01'])
for d in names:
    if d!='2026-10-01': rows.append(dict(mode='A_transfer_from_2026-10-01',**evaluate(net,m,s,d)))
for d in names:
    net,m,s=train([x for x in names if x!=d]); rows.append(dict(mode='B_leave_one_day_out',**evaluate(net,m,s,d)))
r=pd.DataFrame(rows); r.to_csv('results/book_multiday.csv',index=False); pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
print(r.round(3).to_string())
print(r.groupby('mode')[['ic','absmove','gross_q0.0','gross_q0.9','gross_q0.99','net_taker','net_maker']].mean().round(3))
print(r.groupby('mode').apply(lambda g:(g.ic>0).mean()).to_dict(),'share of days with IC>0')
