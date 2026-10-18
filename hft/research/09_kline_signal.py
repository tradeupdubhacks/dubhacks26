"""Multi-year, multi-asset walk-forward test of 1h / 4h / 24h direction prediction on 1-minute klines.
Decision every 5 min at the close of a bar; entry at the NEXT bar's open (1 bar latency); label = return to bar m+H close.
Features use only bars <= m. Train on all years before the test year (minus an H embargo); pooled across assets."""
import numpy as np, pandas as pd, time, warnings; warnings.filterwarnings('ignore')
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier
SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','DOGEUSDT']; STEP=5
def cs(x): return np.concatenate([[0.],np.cumsum(x)])
def feats(d):
    c,o,h,l,v,tb=d['c'],d['o'],d['h'],d['l'],d['v'],d['tbv']; n=len(c)
    lr=np.r_[0,np.diff(np.log(c))]*1e4; C1=cs(lr); C2=cs(lr**2); V=cs(v); TB=cs(tb); RG=cs((h-l)/c*1e4)
    idx=np.arange(1500,n-1450,STEP)
    def win(C,k): return (C[idx+1]-C[idx+1-k])
    rv=lambda k: np.sqrt(np.maximum(win(C2,k)/k,1e-12))
    rv1440,rv60=rv(1440),rv(60); F=[]
    for k in (1,5,15,60,240,1440): F.append(win(C1,k)/(rv1440*np.sqrt(k)))
    F+= [np.log(rv60/rv1440), np.log(rv1440)]
    for k in (5,60,240):
        vv=win(V,k); F.append(np.where(vv>0,win(TB,k)/np.maximum(vv,1e-12)-0.5,0))
    F.append(np.log((win(V,60)+1e-9)/(win(V,1440)/24+1e-9)))
    F.append(win(RG,15)/15/ (rv1440)); F.append(win(RG,60)/60/rv1440)
    hr=((d['t'][idx]//3_600_000)%24)/24*2*np.pi; F+= [np.sin(hr),np.cos(hr)]
    X=np.stack(F,1).astype(np.float32)
    ts=d['t'][idx]; year=pd.to_datetime(ts,unit='ms').year.values
    return idx,X,year,lr
D={s:dict(np.load(f'klines_{s}.npz')) for s in SYMS}
rows=[]; t0=time.time()
for H in (60,240,1440):
    data={}
    for s in SYMS:
        d=D[s]; idx,X,yr,lr=feats(d); o=d['o']; c=d['c']
        ok=idx+H<len(c)-1; idx,X,yr=idx[ok],X[ok],yr[ok]
        y=np.log(c[idx+H]/o[idx+1])*1e4
        data[s]=(idx,X,yr,y)
    for Y in range(2022,2027):
        tr=[];te=[]
        for s,(idx,X,yr,y) in data.items():
            cut=np.flatnonzero(yr<Y); 
            if len(cut): cut=cut[:-max(1,H//STEP+1)]    # embargo
            tr.append((X[cut][::3],(y[cut][::3]>0)))   # train every 15 min
            m=yr==Y; te.append((s,X[m],y[m]))
        Xtr=np.concatenate([a for a,_ in tr]); ytr=np.concatenate([b for _,b in tr])
        if len(Xtr)<20000: continue
        clf=HistGradientBoostingClassifier(max_depth=3,max_iter=120,learning_rate=.05,l2_regularization=10).fit(Xtr,ytr)
        for s,Xt,yt in te:
            if len(Xt)<500: continue
            p=clf.predict_proba(Xt)[:,1]-.5; take=np.arange(0,len(yt),max(1,H//STEP))   # non-overlapping decisions
            conf=np.abs(p); top=conf>=np.quantile(conf,.8)
            d=np.sign(p)
            g_all=(d*yt)[take]; tt=take[top[take]]; g_top=(d*yt)[tt]
            rows.append(dict(H=H,year=Y,sym=s,n=len(yt),ic=spearmanr(p,yt)[0],dir_acc=(d==np.sign(yt)).mean(),
              gross_all=g_all.mean(),gross_top=g_top.mean() if len(g_top) else np.nan,n_top=len(tt),
              net_top_taker=(g_top-11).mean() if len(g_top) else np.nan,net_top_maker=(g_top-4).mean() if len(g_top) else np.nan,
              bh=yt[take].mean(),absmove=np.abs(yt).mean()))
        print('H',H,'year',Y,'trained',len(Xtr),'%.0fs'%(time.time()-t0),flush=True)
r=pd.DataFrame(rows); r.to_csv('results/kline_signal.csv',index=False)
pd.set_option('display.width',220)
print(r.groupby(['H','year'])[['ic','dir_acc','gross_all','gross_top','net_top_taker','net_top_maker','bh','absmove']].mean().round(3))
print(r.groupby('H')[['ic','dir_acc','gross_all','gross_top','net_top_taker','net_top_maker','absmove']].mean().round(3))
print((r.groupby('H').apply(lambda g:(g.ic>0).mean())).round(2).to_dict(),'share of asset-years with IC>0')
