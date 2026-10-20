"""Walk-forward training/evaluation of GRU, LSTM, TCN vs GBM/ridge and rule baselines on 5s BTCUSDT bars.
Label: forward 60s mid-price return (bps), entered one bar (5s) after the decision (latency). Costs applied per side."""
import numpy as np, pandas as pd, torch, torch.nn as nn, json, os, time
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
torch.manual_seed(0); np.random.seed(0); torch.set_num_threads(8)
BAR, SEQ, H, LAT = 5, 60, 12, 1          # bar seconds, lookback bars, holding bars, latency bars
os.makedirs('models',exist_ok=True); os.makedirs('results',exist_ok=True)

d = pd.read_csv('bars_1s.csv.gz', index_col=0)
d = d.bfill()
d['g'] = (np.arange(len(d))//BAR)
g = d.groupby('g')
b = pd.DataFrame({'mid':g.mid_close.last(),'hi':g.mid_hi.max(),'lo':g.mid_lo.min(),'vol':g.vol.sum(),'ntr':g.ntr.sum(),
    'sv':g.signed_vol.sum(),'imb':g.imb.mean(),'micro':g.micro.last(),'spr':g.spr_bps.mean(),'mark':g.mark_price.last(),
    'index':g.index_price.last(),'oi':g.open_interest.last(),'nq':g.nquote.sum()})
lr = np.log(b.mid).diff().fillna(0)*1e4
F = pd.DataFrame(index=b.index)
F['ret']=lr; F['range']=(b.hi-b.lo)/b.mid*1e4; F['tflow']=b.sv/(b.vol+1e-9); F['svol']=b.sv
F['lvol']=np.log1p(b.vol); F['lntr']=np.log1p(b.ntr); F['lnq']=np.log1p(b.nq); F['imb']=b.imb
F['micro']=(b.micro/b.mid-1)*1e4; F['spr']=b.spr; F['basis']=(b.mark/b.index-1)*1e4
F['doi']=b.oi.pct_change().fillna(0)*1e4
F['rv']=lr.rolling(60,min_periods=5).std().fillna(0); F['mom12']=lr.rolling(12).sum().fillna(0); F['mom60']=lr.rolling(60).sum().fillna(0)
F = F.replace([np.inf,-np.inf],np.nan).fillna(0)
X = F.values.astype(np.float32); N=len(X); print('nan in X:',np.isnan(X).sum())
mid = b.mid.values
# forward return entered at t+LAT, held H bars
y = np.full(N, np.nan, dtype=np.float32)
y[:N-LAT-H] = (np.log(mid[LAT+H:]/mid[LAT:N-H]))*1e4
idx_all = np.arange(SEQ, N-LAT-H)
bph = 3600//BAR
folds = [dict(train=(0,10*bph),val=(10*bph,12*bph),test=(12*bph,16*bph)),
         dict(train=(0,14*bph),val=(14*bph,16*bph),test=(16*bph,20*bph)),
         dict(train=(0,18*bph),val=(18*bph,20*bph),test=(20*bph,N))]
emb = SEQ+H+LAT   # embargo so no label/window overlaps across splits

def sel(lo,hi,e_lo=0,e_hi=0): 
    return idx_all[(idx_all>=lo+e_lo)&(idx_all<hi-e_hi)]
def windows(Xn, ids): return np.stack([Xn[i-SEQ+1:i+1] for i in ids])

class RNN(nn.Module):
    def __init__(s,nf,kind):
        super().__init__(); s.r=(nn.GRU if kind=='gru' else nn.LSTM)(nf,48,batch_first=True); s.o=nn.Sequential(nn.Dropout(.3),nn.Linear(48,1))
    def forward(s,x): return s.o(s.r(x)[0][:,-1]).squeeze(-1)
class Block(nn.Module):
    def __init__(s,c,dil):
        super().__init__(); s.pad=2*dil; s.c1=nn.Conv1d(c,c,3,dilation=dil); s.c2=nn.Conv1d(c,c,3,dilation=dil); s.a=nn.ReLU(); s.dr=nn.Dropout(.2)
    def forward(s,x):
        h=s.dr(s.a(s.c1(nn.functional.pad(x,(s.pad,0))))); h=s.dr(s.a(s.c2(nn.functional.pad(h,(s.pad,0))))); return s.a(x+h)
class TCN(nn.Module):
    def __init__(s,nf):
        super().__init__(); s.i=nn.Conv1d(nf,32,1); s.b=nn.Sequential(*[Block(32,2**k) for k in range(4)]); s.o=nn.Sequential(nn.Dropout(.3),nn.Linear(32,1))
    def forward(s,x): return s.o(s.b(s.i(x.transpose(1,2)))[:,:,-1]).squeeze(-1)

def fit(model, Xt, yt, Xv, yv, epochs=25):
    opt=torch.optim.AdamW(model.parameters(),1e-3,weight_decay=1e-2); lf=nn.HuberLoss()
    Xt,yt,Xv,yv=[torch.tensor(a) for a in (Xt,yt,Xv,yv)]; best=(1e9,None,0); hist=[]
    for ep in range(epochs):
        model.train(); perm=torch.randperm(len(Xt))
        for i in range(0,len(Xt),256):
            j=perm[i:i+256]; opt.zero_grad(); l=lf(model(Xt[j]),yt[j]); l.backward(); nn.utils.clip_grad_norm_(model.parameters(),1.); opt.step()
        model.eval()
        with torch.no_grad(): tl=lf(model(Xt),yt).item(); vl=lf(model(Xv),yv).item()
        hist.append((ep,tl,vl))
        if vl<best[0]: best=(vl,{k:v.clone() for k,v in model.state_dict().items()},ep)
        elif ep-best[2]>=5: break
    model.load_state_dict(best[1]); return hist,best[2]
def pred(model,Xw):
    model.eval()
    with torch.no_grad(): return model(torch.tensor(Xw)).numpy()

def trade(sig, ids, side_cost_bps):
    """Non-overlapping 60s decisions. sig: position in {-1,0,1} per sample id. Returns per-decision net returns (bps of capital)."""
    take = np.arange(0,len(ids),H)   # decide every H bars
    pos = sig[take]; gross = pos*y[ids[take]]
    prev = np.concatenate([[0],pos[:-1]])
    # costs: pay per side on position change (close old + open new); flat->flat free. If same direction held, no cost.
    turn = np.abs(pos-prev); cost = turn*side_cost_bps
    return gross-cost, gross, pos, ids[take]
COSTS = {'taker_5.5bps':5.5, 'maker_2bps_optimistic':2.0, 'zero_cost_gross':0.0}

def stats(net, ppy_trades):
    """net: per-decision returns in bps (every 60s). Returns metrics; annualisation by decisions/year."""
    r=net/1e4; 
    if r.std()==0: return dict(mean_bps=float(net.mean()),sharpe=0.0,sortino=0.0,tot_ret=float(r.sum()),mdd=0.0)
    ds=r[r<0].std() if (r<0).sum()>1 else np.nan
    eq=np.cumprod(1+r); mdd=float((eq/np.maximum.accumulate(eq)-1).min())
    return dict(mean_bps=float(net.mean()),sharpe=float(r.mean()/r.std()*np.sqrt(ppy_trades)),sortino=float(r.mean()/ds*np.sqrt(ppy_trades)) if ds==ds else 0.0,
                tot_ret=float(eq[-1]-1),mdd=mdd)
PPY = 365*24*60   # 60s decisions per year

rng=np.random.default_rng(0)
def block_boot(net,n=2000,blk=30):
    out=[]; m=len(net)
    for _ in range(n):
        st=rng.integers(0,m,m//blk+1); out.append(np.concatenate([net[s:s+blk] for s in st])[:m].mean())
    return np.percentile(out,[2.5,97.5]).tolist()

all_res={}; preds={n:[] for n in ['gru','lstm','tcn','ridge','gbm']}; test_ids=[]; hists={}
t0=time.time()
for k,f in enumerate(folds):
    tr=sel(*f['train'],0,emb); va=sel(*f['val'],0,emb); te=sel(*f['test'],0,0)
    mu=X[f['train'][0]:f['train'][1]].mean(0); sd=X[f['train'][0]:f['train'][1]].std(0)+1e-9
    Xn=np.clip((X-mu)/sd,-5,5).astype(np.float32)
    ysd=float(np.nanstd(y[tr])); 
    Wt,Wv,Wte=windows(Xn,tr),windows(Xn,va),windows(Xn,te)
    yt,yv=(y[tr]/ysd).astype(np.float32),(y[va]/ysd).astype(np.float32)
    for name in ['gru','lstm','tcn']:
        torch.manual_seed(k)
        m=RNN(X.shape[1],name) if name!='tcn' else TCN(X.shape[1])
        h,be=fit(m,Wt,yt,Wv,yv); torch.save(m.state_dict(),f'models/{name}_fold{k}.pt'); hists[f'{name}_fold{k}']=h
        preds[name].append(pred(m,Wte)*ysd); print(f'fold{k} {name} best_ep={be} val={min(a[2] for a in h):.4f} t={time.time()-t0:.0f}s',flush=True)
    # classical on flattened last-12-bar summary
    def flat(W): return np.concatenate([W[:,-1],W[:,-12:].mean(1),W[:,-60:].mean(1),W[:,-12:].std(1)],1)
    rg=Ridge(alpha=100).fit(flat(Wt),yt); preds['ridge'].append(rg.predict(flat(Wte))*ysd)
    gb=HistGradientBoostingRegressor(max_depth=3,max_iter=150,learning_rate=.05,l2_regularization=10).fit(flat(Wt),yt); preds['gbm'].append(gb.predict(flat(Wte))*ysd)
    # val |pred| thresholds are needed for signal rules -> store val preds
    for name in ['gru','lstm','tcn']:
        pass
    test_ids.append(te)
    # val predictions for threshold calibration
    for name in ['gru','lstm','tcn']:
        m=RNN(X.shape[1],name) if name!='tcn' else TCN(X.shape[1]); m.load_state_dict(torch.load(f'models/{name}_fold{k}.pt'))
        preds.setdefault(name+'_val',[]).append(pred(m,Wv)*ysd)
    preds.setdefault('ridge_val',[]).append(rg.predict(flat(Wv))*ysd); preds.setdefault('gbm_val',[]).append(gb.predict(flat(Wv))*ysd)
    preds.setdefault('yva',[]).append(y[va])

ids=np.concatenate(test_ids)
# --- build signals ---
sigs={}
for name in ['gru','lstm','tcn','ridge','gbm']:
    sg=[]
    for k in range(len(folds)):
        thr=np.quantile(np.abs(preds[name+'_val'][k]),0.80)     # trade top-20% most confident (calibrated on validation only)
        p=preds[name][k]; sg.append(np.where(np.abs(p)>=thr,np.sign(p),0.0))
    sigs[name]=np.concatenate(sg)
mom=F['mom12'].values[ids]; imb=F['imb'].values[ids]
sigs['momentum_12bar']=np.sign(mom); sigs['imbalance_rule']=np.where(np.abs(imb)>0.5,np.sign(imb),0.0)
sigs['buy_and_hold(always long)']=np.ones(len(ids)); sigs['random']=rng.choice([-1.,1.],len(ids))
sigs['mean_reversion_12bar']=-np.sign(mom)

rows=[]; curves={}
yt_all=y[ids]
for name,s in sigs.items():
    for cn,c in COSTS.items():
        net,gross,pos,tid=trade(s,ids,c)
        # buy&hold holds continuously -> costs only once
        if name.startswith('buy'): net=gross.copy(); net[0]-=c
        st=stats(net,PPY); lo,hi=block_boot(net)
        rows.append(dict(strategy=name,cost=cn,n_decisions=len(net),trades=int((np.abs(np.diff(np.concatenate([[0],pos])))>0).sum()),
            win_rate=float((net[pos!=0]>0).mean()) if (pos!=0).any() else np.nan,cost_paid_bps=float(((gross-net).sum())),
            mean_bps_ci_lo=lo,mean_bps_ci_hi=hi,**st))
        curves[(name,cn)]=net
res=pd.DataFrame(rows); res.to_csv('results/oos_backtest.csv',index=False)
pd.set_option('display.width',250); pd.set_option('display.max_columns',30)
print(res[res.cost=='taker_5.5bps'][['strategy','trades','win_rate','mean_bps','mean_bps_ci_lo','mean_bps_ci_hi','tot_ret','mdd','sharpe']].round(3).to_string())
print(res[res.cost=='zero_cost_gross'][['strategy','trades','win_rate','mean_bps','mean_bps_ci_lo','mean_bps_ci_hi','tot_ret','sharpe']].round(3).to_string())
# predictive skill
sk=[]
for name in ['gru','lstm','tcn','ridge','gbm']:
    p=np.concatenate(preds[name]); ok=~np.isnan(yt_all)
    ic=spearmanr(p[ok],yt_all[ok])[0]; da=float((np.sign(p[ok])==np.sign(yt_all[ok])).mean())
    sk.append(dict(model=name,spearman_ic=ic,directional_acc=da,n=int(ok.sum())))
sk=pd.DataFrame(sk); sk.to_csv('results/predictive_skill.csv',index=False); print(sk.round(4))
# save series for downstream
np.savez('results/test_series.npz',ids=ids,y=yt_all,mid=mid[ids],**{f'sig_{k}':v for k,v in sigs.items()},**{f'pred_{k}':np.concatenate(preds[k]) for k in ['gru','lstm','tcn','ridge','gbm']})
json.dump({k:[(a,b,c) for a,b,c in v] for k,v in hists.items()},open('results/training_history.json','w'))
# plots
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
fig,ax=plt.subplots(1,2,figsize=(13,4.5))
for name in ['gru','lstm','tcn','ridge','gbm','momentum_12bar','buy_and_hold(always long)']:
    ax[0].plot(np.cumsum(curves[(name,'zero_cost_gross')])/100,label=name,lw=1)
    ax[1].plot(np.cumsum(curves[(name,'taker_5.5bps')])/100,label=name,lw=1)
ax[0].set_title('Cumulative GROSS return (%), zero cost'); ax[1].set_title('Cumulative NET return (%), taker fees+slippage 5.5bps/side')
for a in ax: a.set_xlabel('60s decision # (12h OOS)'); a.axhline(0,c='k',lw=.5)
ax[1].legend(fontsize=7); plt.tight_layout(); plt.savefig('results/oos_equity.png',dpi=130)
fig,ax=plt.subplots(1,3,figsize=(13,3.5))
for a,n in zip(ax,['gru','lstm','tcn']):
    for k in range(3): h=np.array(hists[f'{n}_fold{k}']); a.plot(h[:,2],label=f'val f{k}'); a.plot(h[:,1],'--',label=f'train f{k}')
    a.set_title(n+' Huber loss'); a.set_xlabel('epoch')
ax[0].legend(fontsize=6); plt.tight_layout(); plt.savefig('results/training_curves.png',dpi=130)
