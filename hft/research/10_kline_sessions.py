"""Daily goal sessions on 1-minute klines, 6 coins, 2022-2026, walk-forward 1h-direction model (see 09_kline_signal.py).
Session: deposit $100 pooled; start 00:00 UTC; runs <= 24 h; ends at ceiling, floor, or 24 h (open position closed at the last close).
Policy: on the 5-min grid, when flat and model confidence >= its median, take a bracket trade (taker entry at next open, maker take-profit
filled when the bar's high/low reaches it, taker stop; if both are touched in one bar the stop is assumed first).
Position is sized like model/sizing.py: smallest notional whose take-profit reaches the ceiling, capped so a stop-out cannot cross the floor,
and capped at the pool limit ($840 = 10 lots of 0.001 BTC at the sample price). Fees 5 bps taker / 2 bps maker, 0.5 bps slippage."""
import numpy as np, pandas as pd, time, warnings; warnings.filterwarnings('ignore')
from sklearn.ensemble import HistGradientBoostingClassifier
exec(open('09_kline_signal.py').read().split("D={s:dict")[0].split("SYMS=")[0])   # imports
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']; STEP=5; H=60; DEP=100.0; NMAX=840.0
def cs(x): return np.concatenate([[0.],np.cumsum(x)])
def feats(d):
    c,o,h,l,v,tb,t=d['c'],d['o'],d['h'],d['l'],d['v'],d['tbv'],d['t']; n=len(c)
    lr=np.r_[0,np.diff(np.log(c))]*1e4; C1=cs(lr); C2=cs(lr**2); V=cs(v); TB=cs(tb); RG=cs((h-l)/c*1e4)
    idx=np.flatnonzero(((t//60000)%STEP==0))
    idx=idx[(idx>=1500)&(idx<n-1500)]
    win=lambda C,k:(C[idx+1]-C[idx+1-k]); rv=lambda k:np.sqrt(np.maximum(win(C2,k)/k,1e-12))
    rv1440,rv60=rv(1440),rv(60); F=[win(C1,k)/(rv1440*np.sqrt(k)) for k in (1,5,15,60,240,1440)]
    F+=[np.log(rv60/rv1440),np.log(rv1440)]
    for k in (5,60,240):
        vv=win(V,k); F.append(np.where(vv>0,win(TB,k)/np.maximum(vv,1e-12)-0.5,0))
    F.append(np.log((win(V,60)+1e-9)/(win(V,1440)/24+1e-9))); F+=[win(RG,15)/15/rv1440,win(RG,60)/60/rv1440]
    hr=((t[idx]//3_600_000)%24)/24*2*np.pi; F+=[np.sin(hr),np.cos(hr)]
    return idx,np.stack(F,1).astype(np.float32),pd.to_datetime(t[idx],unit='ms').year.values
D={s:dict(np.load(f'klines_{s}.npz')) for s in SYMS}
def bracket_session(d,pred,idx,s0,goal,loss,tp,stop,rule,rng,thr):
    """returns (result, final_stake_$, trades)"""
    o,h,l,c=d['o'],d['h'],d['l'],d['c']; end=s0+1440; stake=DEP; floor=DEP*(1-loss); ceil=DEP*(1+goal); trades=0
    pos=np.searchsorted(idx,s0); 
    while pos<len(idx) and idx[pos]<end-2:
        m=idx[pos]; p=pred[pos]
        if abs(p)<thr: pos+=1; continue
        side=1 if (p>0 if rule=='model' else (rule=='long' or (rule=='random' and rng.random()<.5))) else -1
        gain=(tp-5-2-0.5)*1e-4; lossr=(stop+5+5+1.0)*1e-4
        notional=min(NMAX,(ceil-stake)/gain,(stake-floor-0.01)/lossr)
        if notional<=1.0 or gain<=0: return ('timeout',stake,trades)
        e=o[m+1]*(1+side*0.5e-4); trades+=1
        j0=m+1; seg=slice(j0,end)
        hh,ll=h[seg],l[seg]
        tpx=e*(1+side*tp*1e-4); spx=e*(1-side*stop*1e-4)
        tp_hit=(hh>=tpx) if side>0 else (ll<=tpx); st_hit=(ll<=spx) if side>0 else (hh>=spx)
        kt=np.argmax(tp_hit) if tp_hit.any() else 10**9; ks=np.argmax(st_hit) if st_hit.any() else 10**9
        if kt==10**9 and ks==10**9:
            x=c[end-1]*(1-side*0.5e-4); pnl=side*notional*(x/e-1)-notional*5e-4*2; stake+=pnl; return ('timeout',stake,trades)
        if ks<=kt:
            x=spx*(1-side*0.5e-4); stake+=side*notional*(x/e-1)-notional*(5e-4+5e-4); k=ks
        else:
            stake+=side*notional*(tpx/e-1)-notional*(5e-4+2e-4); k=kt
        if stake>=ceil-1e-9: return ('ceiling',stake,trades)
        if stake<=floor+1e-9: return ('floor',stake,trades)
        pos=np.searchsorted(idx,j0+k+1)
    return ('timeout',stake,trades)
rows=[]; t0=time.time(); rng=np.random.default_rng(0)
data={s:feats(D[s])+(np.log(D[s]['c'][feats(D[s])[0]+H]/D[s]['o'][feats(D[s])[0]+1])*1e4,) for s in SYMS}
CFG=[('goal+1%/floor-2%',.01,.02),('goal+5%/floor-5%',.05,.05)]
for Y in range(2022,2027):
    Xtr=[];ytr=[]
    for s,(idx,X,yr,y) in data.items():
        cut=np.flatnonzero(yr<Y); cut=cut[:-(H//STEP+2)]; Xtr.append(X[cut][::3]); ytr.append(y[cut][::3]>0)
    clf=HistGradientBoostingClassifier(max_depth=3,max_iter=120,learning_rate=.05,l2_regularization=10).fit(np.concatenate(Xtr),np.concatenate(ytr))
    for s,(idx,X,yr,y) in data.items():
        m=yr==Y
        if m.sum()<1000: continue
        pred=np.full(len(idx),0.0); pred[m]=clf.predict_proba(X[m])[:,1]-.5
        thr=np.quantile(np.abs(pred[m]),.5)
        starts=idx[m][(D[s]['t'][idx[m]]%86_400_000)==0]
        for name,g,f in CFG:
            for tp in (20,40,80):
                for rule in ('model','random','long'):
                    for s0 in starts:
                        res=bracket_session(D[s],pred,idx,s0,g,f,tp,tp,rule,rng,thr)
                        rows.append(dict(year=Y,sym=s,cfg=name,tp=tp,rule=rule,result=res[0],pnl_pct=(res[1]-DEP)/DEP*100,trades=res[2]))
    print('year',Y,'%.0fs'%(time.time()-t0),len(rows),flush=True)
r=pd.DataFrame(rows); r.to_csv('results/kline_sessions.csv',index=False)
pd.set_option('display.width',250)
g=r.groupby(['cfg','tp','rule']).apply(lambda x:pd.Series(dict(sessions=len(x),p_ceiling=(x.result=='ceiling').mean(),p_floor=(x.result=='floor').mean(),p_timeout=(x.result=='timeout').mean(),mean_pnl_pct=x.pnl_pct.mean(),trades=x.trades.mean())))
print(g.round(3))
