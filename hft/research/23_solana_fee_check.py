"""How much does the Solana network fee (about 0.1 cent per fill, FEE_MODEL=solana on origin/balls) cost the transformer policy?
Run from the repo root: .venv/bin/python hft/research/23_solana_fee_check.py"""
import sys
sys.path.insert(0, '.')
src = open('hft/research/22_transformer_policy_eval.py').read().split("def run(")[0]
exec(src)
import numpy as np
from model.backtest import StepEnv, LIMITS
def go(network, label, deposit=10_000, floor=0, ceiling=10**9):
    sizing.TAKER_BPS = sizing.MAKER_BPS = 0.0; backtest.FEES.update(taker=0.0, maker=0.0)
    P = SessionParams(take_profit_bps=0, stop_bps=25, max_hold_steps=25, cooldown_steps=5, threshold=entry, vol_gate=0.0, sizing_bps=1.0)
    env = StepEnv(tape, deposit, floor, ceiling, 0, network_fee_cents=network); ids, st = IdSource('t'), SessionState(); fills = 0
    for j in range(len(bid) - 2):
        o = env.observation(); d = decide_session(o, LIMITS, float(score[j]), list(sf[j]), P, st, ids); p0 = env.pos; f0 = env.fees; env.step(d)
        if env.halted: break
    print('%-46s price P&L %+8.2f $ | fees -%6.2f $ | stake change %+8.2f $%s' % (label, env.realized / 100, env.fees / 100, (env.stake() - deposit) / 100, ' [halted: %s at hour %.1f]' % (env.reason, (j + 1) * 0.2 / 3600) if env.halted else ''))
go(0.0, 'no fees at all')
go(0.1, 'Solana network fee only (0.1 cent per fill)')
go(0.1, 'Solana fee, page defaults (-20% / +5%)', floor=8_000, ceiling=10_500)
go(0.5, 'if SOL were 5x pricier (0.5 cent per fill)')
