"""Max-1-day horizon: what does a +10/15/20% target require? (a) this day's actual path as a hindsight bound, (b) intraday MC with leverage."""
import numpy as np, pandas as pd
d=pd.read_csv('bars_1s.csv.gz',index_col=0); m=d.mid_close.values
print('Day range: low %.0f high %.0f -> %.2f%%; open->close %.2f%%'%(m.min(),m.max(),(m.max()/m.min()-1)*100,(m[-1]/m[0]-1)*100))
# hindsight bounds, NO costs: best single long-only trade and best-case max drawdown path
lo=np.minimum.accumulate(m); best_long=(m/lo-1).max(); hi=np.maximum.accumulate(m[::-1])[::-1]; best_short=(hi/m-1).max()
print('Perfect-hindsight best single long trade: %.2f%%, best long-then-nothing; leverage needed for +10/15/20%%: %s'%(best_long*100,[round(r/best_long,1) for r in (.10,.15,.20)]))
# 1-min perfect-foresight (sum of |1-min returns|): unrealistic ceiling, before costs
m1=m[::60]; r1=np.diff(np.log(m1)); print('Perfect 1-min foresight, zero cost, gross sum|r|: %.0f%%  (cost at 11bps/round trip x 1439 = %.0f%%)'%(np.abs(r1).sum()*100,1439*.0011*100))
# MC: zero-edge directional trader (random direction each 15 min) or edge sweep, leverage L, liquidation at 1/L adverse move, stop at -15% equity
rng=np.random.default_rng(2); n=20000; steps=96; rows=[]
for vol in [0.31,0.45,0.80]:
    sd=vol/np.sqrt(365*steps)
    for L in [1,3,5,10,20]:
        for edge_sr in [0,2]:           # annualised Sharpe of unlevered strategy
            mu=edge_sr*vol/365/steps*1.0
            r=mu+sd*rng.standard_normal((n,steps)); eq=np.cumprod(1+np.maximum(L*r-0.0*L,-1),1)   # no cost in this bound
            dead=eq<=0.15+0.0   # equity <=15% remaining
            for R in [.10,.15,.20]:
                hit=eq>=1+R; stop=eq<=0.85
                th=np.where(hit.any(1),hit.argmax(1),999); ts=np.where(stop.any(1),stop.argmax(1),999)
                rows.append(dict(vol=vol,lev=L,sharpe_unlev=edge_sr,R=R,p_reach_1d=(th<ts).mean(),p_stop_first=(ts<th).mean(),p_end_below_deposit=(eq[:,-1]<1).mean()))
r=pd.DataFrame(rows); r.to_csv('results/one_day_leverage.csv',index=False)
pd.set_option('display.width',200); q=r[(r.vol==.45)&(r.sharpe_unlev==0)]
print(q.pivot(index='lev',columns='R',values='p_reach_1d').round(3)); print(q.pivot(index='lev',columns='R',values='p_stop_first').round(3))
print(r[(r.vol==.45)&(r.sharpe_unlev==2)].pivot(index='lev',columns='R',values='p_reach_1d').round(3))
