"""Short-horizon (seconds) variant: 1s bars, 5s hold, volatility-gated, cost-aware thresholds chosen on validation only.
Same walk-forward folds / embargo / train-only normalisation as 02_train_eval.py."""
import numpy as np, pandas as pd, torch, torch.nn as nn, os, time, sys
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
torch.manual_seed(0); np.random.seed(0); torch.set_num_threads(8)
SEQ, H, LAT = 30, int(sys.argv[1]) if len(sys.argv)>1 else 5, 1
TAG=f'h{H}s'; os.makedirs('models',exist_ok=True); os.makedirs('results',exist_ok=True)
d=pd.read_csv('bars_1s.csv.gz',index_col=0).bfill()
mid=d.mid_close.values; N=len(d)
lr=np.r_[0,np.diff(np.log(mid))]*1e4
F=pd.DataFrame(index=d.index)
F['ret']=lr; F['range']=(d.mid_hi-d.mid_lo)/d.mid_close*1e4; F['tflow']=d.signed_vol/(d.vol+1e-9); F['svol']=d.signed_vol
F['lvol']=np.log1p(d.vol); F['lntr']=np.log1p(d.ntr); F['lnq']=np.log1p(d.nquote); F['imb']=d.imb
F['imb_last']=(d.bid_amt-d.ask_amt)/(d.bid_amt+d.ask_amt); F['micro']=(d.micro/d.mid_close-1)*1e4; F['spr']=d.spr_bps
F['basis']=(d.mark_price/d.index_price-1)*1e4
s=pd.Series(lr,index=d.index)
F['rv30']=s.rolling(30,min_periods=5).std(); F['mom3']=s.rolling(3).sum(); F['mom10']=s.rolling(10).sum(); F['mom30']=s.rolling(30).sum()
F['flow10']=F.svol.rolling(10).sum(); F['imb10']=F.imb.rolling(10).mean()
F=F.replace([np.inf,-np.inf],np.nan).fillna(0); X=F.values.astype(np.float32)
RV=F.rv30.values
y=np.full(N,np.nan,np.float32); y[:N-LAT-H]=np.log(mid[LAT+H:]/mid[LAT:N-H])*1e4
ids_all=np.arange(SEQ,N-LAT-H); emb=SEQ+H+LAT; hr=3600
folds=[dict(train=(0,10*hr),val=(10*hr,12*hr),test=(12*hr,16*hr)),dict(train=(0,14*hr),val=(14*hr,16*hr),test=(16*hr,20*hr)),dict(train=(0,18*hr),val=(18*hr,20*hr),test=(20*hr,N))]
sel=lambda lo,hi,e=0: ids_all[(ids_all>=lo)&(ids_all<hi-e)]
win=lambda Xn,ids: np.stack([Xn[i-SEQ+1:i+1] for i in ids])
class RNN(nn.Module):
    def __init__(s,nf,k): super().__init__(); s.r=(nn.GRU if k=='gru' else nn.LSTM)(nf,48,batch_first=True); s.o=nn.Sequential(nn.Dropout(.3),nn.Linear(48,1))
    def forward(s,x): return s.o(s.r(x)[0][:,-1]).squeeze(-1)
class Blk(nn.Module):
    def __init__(s,c,dl): super().__init__(); s.p=2*dl; s.a=nn.Conv1d(c,c,3,dilation=dl); s.b=nn.Conv1d(c,c,3,dilation=dl); s.dr=nn.Dropout(.2)
    def forward(s,x):
        f=nn.functional.pad; h=s.dr(torch.relu(s.a(f(x,(s.p,0))))); h=s.dr(torch.relu(s.b(f(h,(s.p,0))))); return torch.relu(x+h)
class TCN(nn.Module):
    def __init__(s,nf): super().__init__(); s.i=nn.Conv1d(nf,32,1); s.b=nn.Sequential(*[Blk(32,2**k) for k in range(4)]); s.o=nn.Sequential(nn.Dropout(.3),nn.Linear(32,1))
    def forward(s,x): return s.o(s.b(s.i(x.transpose(1,2)))[:,:,-1]).squeeze(-1)
def fit(m,Xt,yt,Xv,yv,ep=15):
    o=torch.optim.AdamW(m.parameters(),1e-3,weight_decay=1e-2); lf=nn.HuberLoss(); Xt,yt,Xv,yv=map(torch.tensor,(Xt,yt,Xv,yv)); best=(1e9,None,0)
    for e in range(ep):
        m.train(); p=torch.randperm(len(Xt))
        for i in range(0,len(Xt),512):
            j=p[i:i+512]; o.zero_grad(); lf(m(Xt[j]),yt[j]).backward(); nn.utils.clip_grad_norm_(m.parameters(),1.); o.step()
        m.eval()
        with torch.no_grad(): vl=lf(m(Xv),yv).item()
        if vl<best[0]: best=(vl,{k:v.clone() for k,v in m.state_dict().items()},e)
        elif e-best[2]>=4: break
    m.load_state_dict(best[1]); return best[2]
def pr(m,W):
    m.eval()
    with torch.no_grad(): return np.concatenate([m(torch.tensor(W[i:i+8192])).numpy() for i in range(0,len(W),8192)])
def pnl(pos,yy,c):
    prev=np.r_[0,pos[:-1]]; return pos*yy-np.abs(pos-prev)*c
def pick(pv,yv,gate_v,c):
    """choose |pred| quantile threshold on VALIDATION maximizing net PnL (decisions every H s, gated). Returns thr or None."""
    best=(0.0,None)
    for q in [.5,.7,.8,.9,.95,.98]:
        thr=np.quantile(np.abs(pv[gate_v]),q) if gate_v.any() else np.inf
        pos=np.where(gate_v&(np.abs(pv)>=thr),np.sign(pv),0.)[::H]; v=pnl(pos,yv[::H],c).sum()
        if v>best[0]: best=(v,thr)
    return best[1]
COSTS={'taker_5.5':5.5,'maker_2.0':2.0,'rebate_free_0.5':0.5,'zero':0.0}
names=['gru','lstm','tcn','gbm']; P={n:[] for n in names}; PV={n:[] for n in names}; YV=[]; GV=[]; TE=[]; GT=[]
t0=time.time()
for k,f in enumerate(folds):
    tr=sel(*f['train'],emb); va=sel(*f['val'],emb); te=sel(*f['test'])
    a,b=f['train']; mu,sd=X[a:b].mean(0),X[a:b].std(0)+1e-9; Xn=np.clip((X-mu)/sd,-5,5).astype(np.float32)
    ysd=float(np.nanstd(y[tr])); Wt,Wv,We=win(Xn,tr),win(Xn,va),win(Xn,te); yt,yv=(y[tr]/ysd).astype(np.float32),(y[va]/ysd).astype(np.float32)
    rv_thr=np.quantile(RV[tr],0.80)       # "volatile" = top 20% of trailing 30s vol, threshold from TRAIN only
    GV.append(RV[va]>=rv_thr); GT.append(RV[te]>=rv_thr); YV.append(y[va]); TE.append(te)
    for n in ['gru','lstm','tcn']:
        torch.manual_seed(k); m=RNN(X.shape[1],n) if n!='tcn' else TCN(X.shape[1]); be=fit(m,Wt,yt,Wv,yv)
        torch.save(m.state_dict(),f'models/{n}_{TAG}_fold{k}.pt'); P[n].append(pr(m,We)*ysd); PV[n].append(pr(m,Wv)*ysd); print(f'fold{k} {n} ep={be} {time.time()-t0:.0f}s',flush=True)
    fl=lambda W: np.concatenate([W[:,-1],W[:,-5:].mean(1),W[:,-30:].mean(1)],1)
    g=HistGradientBoostingRegressor(max_depth=3,max_iter=150,learning_rate=.05,l2_regularization=10).fit(fl(Wt),yt); P['gbm'].append(g.predict(fl(We))*ysd); PV['gbm'].append(g.predict(fl(Wv))*ysd)
ids=np.concatenate(TE); yy=y[ids]; gate=np.concatenate(GT)
rows=[]
for n in names:
    p=np.concatenate(P[n]); ok=~np.isnan(yy); ic_all=spearmanr(p[ok],yy[ok])[0]; ic_g=spearmanr(p[ok&gate],yy[ok&gate])[0]
    for cn,c in COSTS.items():
        # thresholds chosen per fold on validation at THIS cost; None -> no trading
        sig=[]; ntr=0
        for k in range(3):
            thr=pick(PV[n][k],YV[k],GV[k],c); pk=P[n][k]; gk=GT[k]
            sig.append(np.zeros(len(pk)) if thr is None else np.where(gk&(np.abs(pk)>=thr),np.sign(pk),0.))
        pos=np.concatenate(sig)[::H]; net=pnl(pos,yy[::H],c)
        gross=pos*yy[::H]; tr_=int((np.abs(np.diff(np.r_[0,pos]))>0).sum()); hold=pos!=0
        rows.append(dict(model=n,cost_bps_per_side=cn,IC_all=ic_all,IC_volatile=ic_g,trades=int(hold.sum()),gross_bps_per_trade=float(gross[hold].mean()) if hold.any() else np.nan,
            net_bps_per_trade=float(net[hold].mean()) if hold.any() else np.nan,total_net_ret_pct=float(net.sum()/100),win_rate=float((net[hold]>0).mean()) if hold.any() else np.nan))
r=pd.DataFrame(rows); r.to_csv(f'results/short_horizon_{TAG}.csv',index=False)
pd.set_option('display.width',250); print(r.round(4).to_string())
print('avg |y| over %ds in volatile periods (bps): %.2f ; all: %.2f'%(H,np.nanmean(np.abs(yy[gate])),np.nanmean(np.abs(yy))))
