"""The transformer's short-hold policy on the day the server replays (2026-10-01), which the model never trained on.
Runs model.session_policy.decide_session step by step through model.backtest.StepEnv (the replica of the server's fills,
checked against the real server in 13_server_vs_replica.py), for the whole day, under three fee settings.
Run from the repo root:  .venv/bin/python hft/research/22_transformer_policy_eval.py"""
import sys, time
sys.path.insert(0, '.')
import numpy as np
import torch
from model import backtest, sizing
from model.backtest import StepEnv, Tape, LIMITS
from model.features_session import session_features
from model.policy import IdSource
from model.session_policy import SessionParams, SessionState, decide_session
from model.train_transformer import HORIZON, predict
from model.transformer_signal import ARTIFACT, TickNet, WIN, grid_features

blob = torch.load(ARTIFACT, map_location='cpu', weights_only=False)
net = TickNet(len(blob['feature_names'])); net.load_state_dict(blob['state_dict']); net.eval()
mean, std = np.asarray(blob['mean'], np.float32), np.asarray(blob['std'], np.float32)
scale, entry = blob['scale'], blob['entry_threshold']
tape = Tape()
bid, ask = tape.bid[::4] / 100.0, tape.ask[::4] / 100.0
bq, aq = tape.bidsz[::4] * 0.001, tape.asksz[::4] * 0.001
feats = grid_features(bid, ask, bq, aq)
pred, ends = predict(net, feats, mean, std, torch.device('cpu'))
score = np.full(len(bid), 0.5); score[ends] = 0.5 + 0.5 * np.clip(pred / scale, -1, 1)
mid = (bid + ask) / 2; y = np.full(len(mid), np.nan); y[:-HORIZON] = (mid[HORIZON:] / mid[:-HORIZON] - 1) * 1e4
ok = ~np.isnan(y[ends]); conf = np.abs(pred) / scale
print('replay day, model never saw it: rank corr %.3f, gross edge at top 10%% of calls %.2f bps, share of ticks above entry confidence %.1f%%' % (
    np.corrcoef(pred[ok], y[ends][ok])[0, 1], (np.sign(pred) * y[ends])[ok & (conf >= np.quantile(conf, .9))].mean(), 100 * (np.minimum(conf, 1) >= entry).mean()))
sf = np.load('hft/data/out/session_feats.npy')    # session_features per tick of the same day (vol gate is 0, only needs a vector)


def run(maker, taker, deposit=10_000, floor=0, ceiling=10**9, label=''):
    sizing.TAKER_BPS, sizing.MAKER_BPS = taker, maker; backtest.FEES.update(taker=taker, maker=maker)
    P = SessionParams(take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=entry, vol_gate=0.0, sizing_bps=taker + maker + 1.0)
    env = StepEnv(tape, deposit, floor, ceiling, 0); ids, st = IdSource('t'), SessionState(); trades = 0; t0 = time.time()
    for j in range(len(bid) - 2):
        o = env.observation(); d = decide_session(o, LIMITS, float(score[j]), list(sf[j]), P, st, ids)
        was = env.pos; env.step(d); trades += (was == 0 and env.pos != 0)
        if env.halted: break
    hours = (j + 1) * 0.2 / 3600
    print('%-34s trades %5d (%.0f/h) | price P&L %+8.2f $ | fees -%7.2f $ | stake change %+8.2f $ on a $%d deposit%s  (%.0fs)' % (
        label, trades, trades / hours, env.realized / 100, env.fees / 100, (env.stake() - deposit) / 100, deposit // 100, ' [halted: %s]' % env.reason if env.halted else '', time.time() - t0))


run(0, 0, label='fees OFF (platform default)')
run(2, 5, label='fees ON  (2 bps maker / 5 bps taker)')
run(0, 0, deposit=10_000, floor=8_000, ceiling=10_500, label='fees OFF, page defaults (-20%/+5%)')
