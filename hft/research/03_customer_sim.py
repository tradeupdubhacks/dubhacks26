"""First-passage + customer-journey simulation.
EMPIRICAL part: what the OOS 12h backtest actually produced.
SCENARIO part: Monte Carlo of daily wealth paths under stated (NOT estimated-from-data) edge assumptions. One day of data
cannot estimate 7-90 day return distributions; these are sensitivity analyses, not forecasts."""
import numpy as np, pandas as pd, matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
rng=np.random.default_rng(1)
res=pd.read_csv('results/oos_backtest.csv'); tk=res[res.cost=='taker_5.5bps']
D=np.load('results/test_series.npz'); y=D['y']; ok=~np.isnan(y)

# ---- empirical: peak/trough of OOS net equity for each strategy (12h) ----
emp=[]
for _,r in tk.iterrows(): pass
bars=pd.read_csv('bars_1s.csv.gz',index_col=0)
r1=np.log(bars.mid_close).diff().dropna().values
day_vol_ann=r1.std()*np.sqrt(365*86400)
print('realised BTC vol on the sample day, annualised: %.1f%% (single day -> very noisy)'%(day_vol_ann*100))
print('BTC high-low range of the day: %.2f%%'%((bars.mid_hi.max()/bars.mid_lo.min()-1)*100))
# net per-decision stats for ML strategies -> extrapolated daily drift/vol (scenario "measured")
import subprocess
def sim(mu_d,sig_d,T,R,stop,n=20000,df=4):
    """daily arithmetic returns, Student-t(df) scaled to sig_d. Trade until W>=1+R (success), W<=1-stop (stop-out), or day T."""
    z=rng.standard_t(df,(n,T))/np.sqrt(df/(df-2)); r=mu_d+sig_d*z
    W=np.cumprod(1+np.maximum(r,-0.99),1); hit=W>=1+R; stp=W<=1-stop
    th=np.where(hit.any(1),hit.argmax(1),T+1); ts=np.where(stp.any(1),stp.argmax(1),T+1)
    succ=th<ts; stopped=(ts<th)
    t_end=np.where(succ,th,np.where(stopped,ts,T-1)); Wend=W[np.arange(n),t_end]
    # tie-break: stop-out first if same day
    return succ,stopped,th,Wend,W
SC={}
# measured: use per-decision net mean/std of the best-by-mean ML model at taker costs & the upper CI bound
best=tk[tk.strategy.isin(['gru','lstm','tcn','ridge','gbm'])].sort_values('mean_bps',ascending=False).iloc[0]
print('best ML (taker):',best.strategy,best.mean_bps,'CI',best.mean_bps_ci_lo,best.mean_bps_ci_hi)
dec_per_day=1440
SC['ML measured net (best model, mean)']=(best.mean_bps/1e4*dec_per_day, 0.25/np.sqrt(365))
SC['ML measured net (best model, CI upper)']=(best.mean_bps_ci_hi/1e4*dec_per_day, 0.25/np.sqrt(365))
for s in [0,0.5,1,2,3,5]:
    SC[f'Hypothetical vol-targeted strat, net Sharpe {s}']=(s*0.25/365, 0.25/np.sqrt(365))   # 25% ann. vol
for v in [0.45,0.8]:
    SC[f'BTC spot long, zero drift, vol {int(v*100)}%']=(0.0, v/np.sqrt(365))
SC[f'BTC spot long, zero drift, vol {day_vol_ann*100:.0f}% (this day)']=(0.0,day_vol_ann/np.sqrt(365))
Ts=[7,30,60,90]; Rs=[.10,.15,.20]; STOP=0.15   # capital-preservation stop at -15%
rows=[]
for name,(mu,sg) in SC.items():
    for R in Rs:
        for T in Ts:
            s,st,th,We,W=sim(mu,sg,T,R,STOP)
            ps=s.mean(); se=np.sqrt(ps*(1-ps)/len(s))
            rows.append(dict(scenario=name,R=R,T=T,p_reach=ps,p_reach_ci=1.96*se,p_stopout=st.mean(),p_end_below_deposit=(We<1).mean(),
                median_days_to_hit=float(np.median(th[s])) if s.any() else np.nan,
                p_end_above_target_only=(W[:,-1]>=1+R).mean()))
sc=pd.DataFrame(rows); sc.to_csv('results/first_passage_scenarios.csv',index=False)
pd.set_option('display.width',250); pd.set_option('display.max_rows',500)
print(sc[sc.R==.10].pivot(index='scenario',columns='T',values='p_reach').round(3))
print(sc[sc.R==.20].pivot(index='scenario',columns='T',values='p_reach').round(3))

# ---- customer journeys: advance-financing vs deferred purchase ----
journeys=[(600,500),(1100,1000),(1150,1000),(1200,1000),(1000,900)]
mu,sg=SC['Hypothetical vol-targeted strat, net Sharpe 1'] 
cj=[]
for scn in ['ML measured net (best model, CI upper)','Hypothetical vol-targeted strat, net Sharpe 0','Hypothetical vol-targeted strat, net Sharpe 1','Hypothetical vol-targeted strat, net Sharpe 2','BTC spot long, zero drift, vol 45%']:
    mu,sg=SC[scn]
    for P,C0 in journeys:
        R=(P-C0)/C0
        for T in [30,90]:
            s,st,th,We,W=sim(mu,sg,T,R,STOP)
            # advance model: platform pays P-C0 at t0; at end customer account We*C0 is applied; platform shortfall = P - We*C0 when not reached
            short=np.where(s,0.0,np.maximum(P-We*C0,0)); loss_cust=np.where(s,0.0,np.maximum(C0-We*C0,0))
            cj.append(dict(scenario=scn,price=P,deposit=C0,R=R,T_days=T,p_buy_funded_by_trading=s.mean(),p_stopout=st.mean(),
                deferred_model_E_customer_loss_usd=loss_cust.mean(),deferred_p_customer_loses_capital=(We<1).mean() if True else 0,
                advance_model_p_platform_shortfall=(short>0).mean(),advance_E_platform_shortfall_usd=short.mean(),
                advance_p95_shortfall_usd=np.percentile(short,95),advance_shortfall_as_pct_of_advance=short.mean()/(P-C0)))
cj=pd.DataFrame(cj); cj.to_csv('results/customer_journeys.csv',index=False)
print(cj[cj.T_days==30][['scenario','price','deposit','R','p_buy_funded_by_trading','p_stopout','advance_E_platform_shortfall_usd','advance_shortfall_as_pct_of_advance','deferred_model_E_customer_loss_usd']].round(3).to_string())

# ---- charts ----
fig,ax=plt.subplots(1,3,figsize=(14,4),sharey=True)
show=['ML measured net (best model, CI upper)','Hypothetical vol-targeted strat, net Sharpe 0','Hypothetical vol-targeted strat, net Sharpe 1','Hypothetical vol-targeted strat, net Sharpe 2','Hypothetical vol-targeted strat, net Sharpe 3','BTC spot long, zero drift, vol 45%']
for a,R in zip(ax,Rs):
    for n in show:
        q=sc[(sc.scenario==n)&(sc.R==R)]; a.plot(q['T'],q.p_reach,marker='o',label=n.replace('Hypothetical vol-targeted strat, ','hyp. '))
    a.set_title(f'P(first reach +{int(R*100)}% by day T), −15% stop'); a.set_xlabel('horizon T (days)'); a.set_xticks(Ts)
ax[0].set_ylabel('probability'); ax[2].legend(fontsize=6); plt.tight_layout(); plt.savefig('results/first_passage.png',dpi=130)

print("\n== 30-day, R=10%: reach / stop-out / end-below-deposit ==")
print(sc[(sc.R==.10)&(sc['T']==30)][['scenario','p_reach','p_reach_ci','p_stopout','p_end_below_deposit','median_days_to_hit']].round(3).to_string())
print("\n== 90-day, R=20% ==")
print(sc[(sc.R==.20)&(sc['T']==90)][['scenario','p_reach','p_reach_ci','p_stopout','p_end_below_deposit','median_days_to_hit']].round(3).to_string())
