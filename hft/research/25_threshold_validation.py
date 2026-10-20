"""Pick the transformer's entry confidence WITHOUT looking at the replay day: sweep it on the validation day (2023-12-15, never trained on),
then check the same values on the replay day. The validation day only has 200 ms data, so each tick is repeated 4x to feed StepEnv."""
import sys
sys.path.insert(0, '.')
import numpy as np, torch
from model import backtest, sizing
from model.backtest import StepEnv, Tape, LIMITS
from model.features_session import session_features
from model.policy import IdSource
from model.session_policy import SessionParams, SessionState, decide_session
from model.train_transformer import predict, VALIDATION_DAY
from model.transformer_signal import ARTIFACT, TickNet, grid_features
blob = torch.load(ARTIFACT, map_location='cpu', weights_only=False)
net = TickNet(len(blob['feature_names'])); net.load_state_dict(blob['state_dict']); net.eval(); scale = blob['scale']
mean, std = np.asarray(blob['mean'], np.float32), np.asarray(blob['std'], np.float32)
sizing.TAKER_BPS = sizing.MAKER_BPS = 0.0; backtest.FEES.update(taker=0.0, maker=0.0)
def day_from_200ms(z):
    t = Tape.__new__(Tape); rep = lambda a: np.repeat(a, 4)
    t.bid, t.ask = np.round(rep(z['bid']) * 100).astype(np.int64), np.round(rep(z['ask']) * 100).astype(np.int64)
    t.bidsz, t.asksz = np.round(rep(z['bq']) / 0.001).astype(np.int64), np.round(rep(z['aq']) / 0.001).astype(np.int64)
    t.tpx = np.zeros(len(t.bid), np.int64); t.tsz = np.zeros(len(t.bid), np.int64); t.tside = np.zeros(len(t.bid), np.int8)
    t.n200 = len(t.bid) // 4; t.mid50 = (t.bid + t.ask + 1) // 2; t.mid200 = t.mid50[::4]; return t
def scores_for(tape):
    bid, ask = tape.bid[::4] / 100.0, tape.ask[::4] / 100.0; bq, aq = tape.bidsz[::4] * 0.001, tape.asksz[::4] * 0.001
    pred, ends = predict(net, grid_features(bid, ask, bq, aq), mean, std, torch.device('cpu'))
    s = np.full(len(bid), 0.5); s[ends] = 0.5 + 0.5 * np.clip(pred / scale, -1, 1); return s
def sweep(tape, name, thresholds):
    sc = scores_for(tape); out = {}
    feats = np.stack([session_features(tape.observation(j)) or [0.0] * 11 for j in range(0, tape.n200, 1)]) if False else None
    for th in thresholds:
        P = SessionParams(take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=th, vol_gate=0.0, sizing_bps=1.0)
        env = StepEnv(tape, 2000, 1600, 10**9, 600, network_fee_cents=0.1); ids, st = IdSource('t'), SessionState(); opened = 0; first = None; ones = [0.0] * 11
        for j in range(600, tape.n200 - 2):
            o = env.observation(); d = decide_session(o, LIMITS, float(sc[j]), ones, P, st, ids); was = env.pos; env.step(d)
            if was == 0 and env.pos != 0:
                opened += 1; first = first if first is not None else (j - 600) * 0.2
            if env.halted: break
        hours = (tape.n200 - 602 - 0) * 0.2 / 3600; out[th] = (first, opened / hours, (env.stake() - 2000) / 100)
    return out
ths = (0.72, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10)
val = day_from_200ms(np.load(f'hft/data/ext/grid200_{VALIDATION_DAY}.npz')); v = sweep(val, VALIDATION_DAY, ths)
rep = Tape(); r = sweep(rep, '2026-10-01', ths)
print('entry confidence | validation day 2023-12-15 (not trained on)      | replay day 2026-10-01 (not trained on)')
print('                 | first fill   trades/h   full-day P&L ($20 stake) | first fill   trades/h   full-day P&L')
for th in ths:
    f = lambda x: '%7.1f s' % x if x is not None else '      -  '
    print('%-16s | %s  %8.0f  %+12.2f $        | %s  %8.0f  %+10.2f $' % ('%.2f' % th + (' (now)' if th == 0.72 else ''), f(v[th][0]), v[th][1], v[th][2], f(r[th][0]), r[th][1], r[th][2]))
