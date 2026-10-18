"""Run the session policy on the REAL model-API server (training mode, BTC replay), record every command,
then replay the same commands in model.backtest.StepEnv and compare tape, fills and equity.
Needs the server on :3000 (npm start). Rails are emulated locally because training mode has no stake."""
import sys, json, numpy as np
sys.path.insert(0,'.')
from model.client import ModelClient
from model.features_session import session_features
from model.live import load_session_model
from model.policy import Decision, IdSource, InstrumentLimits
from model.session_policy import SessionParams, SessionState, decide_session
from model.backtest import Tape, StepEnv, LIMITS
STEPS=int(sys.argv[1]) if len(sys.argv)>1 else 9000; DEP,FLOOR,CEIL=10000,9800,10100
c=ModelClient(timeout=120); score_of,_=load_session_model()
TP=float(sys.argv[2]) if len(sys.argv)>2 else 25.0
params=SessionParams(take_profit_bps=TP,stop_bps=TP,max_hold_steps=3000,cooldown_steps=25,threshold=0.0,vol_gate=0.0)
lim=InstrumentLimits("BTC-USDT",10,10,10)
obs=c.reset({"instrument":"BTC-USDT","episodeSteps":10**6})["observation"]
ids,state=IdSource("srv"),SessionState(); log=[]; srv=[]; ended=None; fills=0; reasons={}
for k in range(STEPS):
    stake=DEP+obs["equityCents"]-100000
    view={**obs,"stakeCents":stake,"floorCents":FLOOR,"ceilingCents":CEIL}
    f=session_features(view); sc=score_of(f) if f else 0.5
    d=decide_session(view,lim,sc,f,params,state,ids)
    act={"k":k,"flatten":d.flatten,"cancel_all":d.cancel_all,"orders":d.orders,"cancels":d.cancels}
    log.append(act); reasons[d.reason]=reasons.get(d.reason,0)+1
    if d.flatten: c.request("POST","/v1/flatten",{})
    elif d.cancel_all: c.request("POST","/v1/orders/cancel-all",{})
    r=c.step({"dtMs":200,"orders":d.orders,"cancels":d.cancels}); obs=r["observation"]
    fills+=sum(1 for e in r["events"] if e.get("type")=="fill")
    srv.append((obs["ts"],obs["mid"],obs["bids"][0]["price"],obs["asks"][0]["price"],obs["bids"][0]["size"],obs["asks"][0]["size"],obs["equityCents"],obs["positionLots"]))
    s2=DEP+obs["equityCents"]-100000
    if s2<=FLOOR or s2>=CEIL: ended=('ceiling' if s2>=CEIL else 'floor',k); break
srv=np.array(srv,dtype=np.int64); n=len(srv)
print("policy decisions:",{k:v for k,v in reasons.items() if k not in ("no_signal","cooldown","hold","entry_pending")})
print(f"server run: {n} steps, {fills} fills, trades entered {sum(1 for a in log if a['orders'] and a['orders'][0]['type']=='ioc')}, ended={ended}, final equity {srv[-1,6]-100000:+d} cents, position {srv[-1,7]}")
# replica replay of the same commands
T=Tape(); env=StepEnv(T,DEP,FLOOR,CEIL,0); rep=[]
for a in log[:n]:
    env.step(Decision(orders=a["orders"],cancels=a["cancels"],cancel_all=a["cancel_all"],flatten=a["flatten"]))
    i=env.i; rep.append((i*50,int(T.mid50[i]),int(T.bid[i]),int(T.ask[i]),int(T.bidsz[i]),int(T.asksz[i]),100000+env.stake()-DEP,env.pos))
rep=np.array(rep,dtype=np.int64)
print("ts equal:",(srv[:,0]==rep[:,0]).mean().round(4))
for j,nm in [(1,'mid'),(2,'bid px'),(3,'ask px'),(4,'bid size'),(5,'ask size')]:
    print(f"tape {nm:9s} exact-match share: {(srv[:,j]==rep[:,j]).mean():.4f}")
de=srv[:,6]-rep[:,6]; print("equity diff (server - replica), cents: max |diff| %d, final %+d, mean %.2f"%(np.abs(de).max(),de[-1],de.mean()))
print("position equal share:",(srv[:,7]==rep[:,7]).mean().round(4))
