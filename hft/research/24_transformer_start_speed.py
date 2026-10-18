"""How soon does the transformer first trade, and what does that cost? Replays a session exactly as the live server starts it
(session clock 0 = tape 120 s, history primed) for several entry-confidence thresholds. Run from the repo root."""
import sys, time
sys.path.insert(0, '.')
import numpy as np, torch
from model import backtest, sizing
from model.backtest import StepEnv, Tape, LIMITS
from model.features_session import session_features
from model.policy import IdSource
from model.session_policy import SessionParams, SessionState, decide_session
from model.train_transformer import predict
from model.transformer_signal import ARTIFACT, TickNet, grid_features
blob = torch.load(ARTIFACT, map_location='cpu', weights_only=False)
net = TickNet(len(blob['feature_names'])); net.load_state_dict(blob['state_dict']); net.eval()
scale = blob['scale']; tape = Tape()
bid, ask = tape.bid[::4] / 100.0, tape.ask[::4] / 100.0; bq, aq = tape.bidsz[::4] * 0.001, tape.asksz[::4] * 0.001
pred, ends = predict(net, grid_features(bid, ask, bq, aq), np.asarray(blob['mean'], np.float32), np.asarray(blob['std'], np.float32), torch.device('cpu'))
score = np.full(len(bid), 0.5); score[ends] = 0.5 + 0.5 * np.clip(pred / scale, -1, 1)
sf = np.load('hft/data/out/session_feats.npy'); START = 600                      # the live session starts at tape tick 600 (120 s)
sizing.TAKER_BPS = sizing.MAKER_BPS = 0.0; backtest.FEES.update(taker=0.0, maker=0.0)
def run(threshold, cap_ticks=2, deposit=2000, floor=1600, ceiling=10**9, minutes=None, network=0.1):
    P = SessionParams(take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=threshold, vol_gate=0.0, sizing_bps=1.0, entry_slip_ticks=cap_ticks)
    env = StepEnv(tape, deposit, floor, ceiling, START, network_fee_cents=network); ids, st = IdSource('t'), SessionState()
    first_entry = first_fill = None; entries = fills_open = 0; stop = len(bid) - 2 if minutes is None else START + int(minutes * 300)
    for j in range(START, stop):
        o = env.observation(); d = decide_session(o, LIMITS, float(score[j]), list(sf[j]), P, st, ids); was = env.pos
        if d.reason == 'entry':
            entries += 1; first_entry = first_entry if first_entry is not None else (j - START) * 0.2
        env.step(d)
        if was == 0 and env.pos != 0:
            fills_open += 1; first_fill = first_fill if first_fill is not None else (j - START) * 0.2
        if env.halted: break
    hours = (stop - START) * 0.2 / 3600
    return dict(first_entry=first_entry, first_fill=first_fill, entries=entries, opened=fills_open, per_hour=fills_open / hours, pnl=(env.stake() - deposit) / 100)
print('session starts at tape 120 s, history primed, Solana fee 0.1c/fill, $20 deposit, floor $16 (cap 2 ticks unless noted)\n')
print('%-22s %-14s %-14s %-9s %-10s %-12s' % ('entry confidence', 'first entry', 'first fill', 'trades/h', 'full-day', 'first 10 min'))
for th in (0.72, 0.60, 0.50, 0.40, 0.30, 0.20, 0.10):
    full = run(th); short = run(th, minutes=10)
    print('%-22s %-14s %-14s %-9.0f %+8.2f $   %+8.2f $' % ('%.2f%s' % (th, ' (current)' if th == 0.72 else ''), '%.1f s' % full['first_entry'] if full['first_entry'] is not None else '-', '%.1f s' % full['first_fill'] if full['first_fill'] is not None else '-', full['per_hour'], full['pnl'], short['pnl']))
print('\nwider order price cap at the current threshold (so a fast first move still fills):')
for cap in (2, 5, 10, 20):
    r = run(0.72, cap_ticks=cap); print('  cap %2d ticks ($%.1f): first fill %s, full-day %+.2f $, trades/h %.0f' % (cap, cap * 0.1, '%.1f s' % r['first_fill'] if r['first_fill'] is not None else '-', r['pnl'], r['per_hour']))
