"""Research stage D: does information beyond price (funding, basis, open interest, positioning, spot, depth, other coins)
predict BTC returns at 1 h / 4 h / 24 h well enough to beat trading costs?

Protocol (fixed before looking at results):
  * decisions every 5 min at a bar close T; entry at the next bar open; exit H bars later; label in bps.
  * every feature is as-of T (info_data.asof on the time the value became known).
  * walk-forward by year: train on all earlier years (minus an H embargo), test on the next year. 2026 (Jan-Sep) is the last test year.
  * the model trades the top 10 % most confident predictions (|predicted z|), direction = sign; non-overlapping trades.
  * cost per round trip: maker 4 bps, taker 11 bps (framework fees 2/5 bps per side + slippage).
  * feature groups are cumulative: G0 price/flow, G1 +funding/basis, G2 +open interest & positioning, G3 +spot, G4 +other coins, G5 +book depth.
"""
import sys, time, warnings
import numpy as np, pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
import info_data as ID
warnings.filterwarnings('ignore')
STEP = 5
OTHERS = ['ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT']


def cs(x): return np.concatenate([[0.], np.cumsum(x)])


def build():
    d = dict(np.load('klines_BTCUSDT.npz')); c, o, h, l, v, tb, t = (d[k] for k in ('c', 'o', 'h', 'l', 'v', 'tbv', 't'))
    n = len(c)
    lr = np.r_[0, np.diff(np.log(c))] * 1e4; C1, C2, V, TB, RG = cs(lr), cs(lr ** 2), cs(v), cs(tb), cs((h - l) / c * 1e4)
    idx = np.flatnonzero((t // 60000) % STEP == 0); idx = idx[(idx >= 1500) & (idx < n - 1501)]
    T = t[idx] + 60_000
    win = lambda C, k: C[idx + 1] - C[idx + 1 - k]
    rv = lambda k: np.sqrt(np.maximum(win(C2, k) / k, 1e-9))
    rv1440 = rv(1440)
    F = {}
    for k in (1, 5, 15, 60, 240, 1440): F[f'g0_ret{k}'] = win(C1, k) / (rv1440 * np.sqrt(k))
    F['g0_rv_ratio'] = np.log(rv(60) / rv1440); F['g0_logrv'] = np.log(rv1440)
    for k in (5, 60, 240):
        vv = win(V, k); F[f'g0_flow{k}'] = np.where(vv > 0, win(TB, k) / np.maximum(vv, 1e-12) - .5, 0)
    F['g0_volratio'] = np.log((win(V, 60) + 1e-9) / (win(V, 1440) / 24 + 1e-9))
    F['g0_range15'] = win(RG, 15) / 15 / rv1440; F['g0_range60'] = win(RG, 60) / 60 / rv1440
    hr = ((t[idx] // 3_600_000) % 24) / 24 * 2 * np.pi; F['g0_sin'] = np.sin(hr); F['g0_cos'] = np.cos(hr)
    dow = ((t[idx] // 86_400_000 + 4) % 7) / 7 * 2 * np.pi; F['g0_dsin'] = np.sin(dow); F['g0_dcos'] = np.cos(dow)

    # ---- G1: funding and perp premium (basis)
    fund = ID.load_funding(); f = ID.asof(fund, T)['last_funding_rate'].values
    F['g1_funding'] = f * 1e4; F['g1_funding_abs'] = np.abs(f) * 1e4
    prem = ID.load_premium(); pser = prem['premium']
    pm60 = pser.rolling(60, min_periods=10).mean(); pd60 = pser - pser.shift(60); pd240 = pser - pser.shift(240)
    pf = pd.DataFrame({'p': pser, 'pm60': pm60, 'pd60': pd60, 'pd240': pd240}, index=prem.index)
    pa = ID.asof(pf, T)
    for k in pf.columns: F[f'g1_prem_{k}'] = pa[k].values * 1e4

    # ---- G2: open interest and positioning
    m = ID.load_metrics(); mm = m.copy()
    oi = mm['sum_open_interest_value']
    mm['oi_chg1h'] = np.log(oi / oi.shift(12)); mm['oi_chg4h'] = np.log(oi / oi.shift(48)); mm['oi_chg24h'] = np.log(oi / oi.shift(288))
    mm['tt_lsr'] = np.log(mm['sum_toptrader_long_short_ratio']); mm['gl_lsr'] = np.log(mm['count_long_short_ratio']); mm['tt_cnt'] = np.log(mm['count_toptrader_long_short_ratio'])
    mm['taker_lsr'] = np.log(mm['sum_taker_long_short_vol_ratio']); mm['taker_lsr_m12'] = mm['taker_lsr'].rolling(12, min_periods=3).mean()
    mm['tt_lsr_d12'] = mm['tt_lsr'] - mm['tt_lsr'].shift(12); mm['gl_lsr_d12'] = mm['gl_lsr'] - mm['gl_lsr'].shift(12)
    mm['tt_minus_gl'] = mm['tt_lsr'] - mm['gl_lsr']
    ma = ID.asof(mm, T, ['oi_chg1h', 'oi_chg4h', 'oi_chg24h', 'tt_lsr', 'gl_lsr', 'tt_cnt', 'taker_lsr', 'taker_lsr_m12', 'tt_lsr_d12', 'gl_lsr_d12', 'tt_minus_gl'])
    for k in ma.columns: F[f'g2_{k}'] = ma[k].values
    # price move vs OI move (divergence)
    F['g2_oi_vs_px4h'] = F['g2_oi_chg4h'] - win(C1, 240) / 1e4

    # ---- G3: spot
    sp = ID.load_spot(); sc = sp['spot_c']
    perp_c = pd.Series(c, index=t + 60_000)
    j = sp.join(perp_c.rename('perp_c'), how='inner')
    basis = np.log(j['spot_c'] / j['perp_c']) * 1e4
    sf = pd.DataFrame({'sbasis': basis, 'sbasis_d60': basis - basis.shift(60), 'sbasis_m240': basis.rolling(240, min_periods=30).mean()}, index=j.index)
    sflow = (sp['spot_tbv'].rolling(60, min_periods=10).sum() / sp['spot_v'].rolling(60, min_periods=10).sum().clip(lower=1e-9)) - .5
    sf['spot_flow60'] = sflow.reindex(sf.index); sf['spot_share'] = np.log((sp['spot_v'].rolling(60, min_periods=10).sum() / (pd.Series(v, index=t + 60_000).rolling(60, min_periods=10).sum().reindex(sp.index)).clip(lower=1e-9)).clip(lower=1e-9)).reindex(sf.index)
    sa = ID.asof(sf, T)
    for k in sf.columns: F[f'g3_{k}'] = sa[k].values

    # ---- G4: the other coins (market factor and relative strength), vol-normalised
    rets = {}
    for s in OTHERS:
        od = dict(np.load(f'klines_{s}.npz')); oc, ot = od['c'], od['t']
        pos = (T - 60_000 - ot[0]) // 60_000; ok = (pos >= 1500) & (pos < len(oc))
        olr = np.r_[0, np.diff(np.log(oc))] * 1e4; oC1, oC2 = cs(olr), cs(olr ** 2)
        for k in (15, 60, 240):
            r = np.full(len(T), np.nan); p = pos[ok]
            sig = np.sqrt(np.maximum((oC2[p + 1] - oC2[p + 1 - 1440]) / 1440, 1e-9)); r[ok] = (oC1[p + 1] - oC1[p + 1 - k]) / (sig * np.sqrt(k))
            rets[(s, k)] = r
    for k in (15, 60, 240):
        M = np.nanmean(np.stack([rets[(s, k)] for s in OTHERS]), 0)
        F[f'g4_mkt{k}'] = M; F[f'g4_btc_rel{k}'] = F[f'g0_ret{k}'] - M
        F[f'g4_eth{k}'] = rets[('ETHUSDT', k)]

    # ---- G5: order-book depth (2023+)
    try:
        dp = ID.load_depth()
        if len(dp):
            dp = dp.copy(); dp['depth_imb1_d1h'] = dp['depth_imb1'] - dp['depth_imb1'].rolling(120, min_periods=20).mean()
            da = ID.asof(dp, T)
            for k in dp.columns: F[f'g5_{k}'] = da[k].values
    except Exception as e:
        print('depth skipped:', e)

    X = pd.DataFrame(F)
    yr = pd.to_datetime(t[idx], unit='ms').year.values
    ys = {}
    for H in (60, 240, 1440):
        ys[H] = np.log(c[idx + H] / o[idx + 1]) * 1e4
    return X, yr, ys, rv1440, T


def groups(cols):
    order = ['g0', 'g1', 'g2', 'g3', 'g4', 'g5']
    return {g: [c for c in cols if c.split('_')[0] in order[:order.index(g) + 1]] for g in order}


def main():
    t0 = time.time(); X, yr, ys, rv1440, T = build(); print('features', X.shape, 'built in %.0fs' % (time.time() - t0), flush=True)
    X.assign(year=yr, T=T).to_pickle('results/info_features.pkl')
    G = groups(list(X.columns)); rows = []
    for H in (60, 240, 1440):
        sd = rv1440 * np.sqrt(H); z = np.clip(ys[H] / sd, -5, 5)
        for g, cols in G.items():
            first = 2024 if g == 'g5' else 2022
            for Y in range(first, 2027):
                tr = np.flatnonzero((yr < Y) & np.isfinite(X[cols].values).any(1)); tr = tr[:-(H // STEP + 2)] if Y > yr.min() else tr
                if g == 'g5': tr = tr[yr[tr] >= 2023]
                te = np.flatnonzero(yr == Y)
                m = HistGradientBoostingRegressor(max_depth=4, max_iter=150, learning_rate=.05, l2_regularization=10, min_samples_leaf=300).fit(X.iloc[tr[::3]][cols], z[tr[::3]])
                p = m.predict(X.iloc[te][cols]); y = ys[H][te]
                take = np.arange(0, len(te), max(1, H // STEP)); thr = np.quantile(np.abs(p), .9)
                tt = take[np.abs(p[take]) >= thr]; gross = np.sign(p[tt]) * y[tt]
                bh = y[take].mean()
                rows.append(dict(H=H, group=g, year=Y, ic=spearmanr(p, y)[0], n=len(tt), gross=gross.mean(), net_maker=gross.mean() - 4, net_taker=gross.mean() - 11,
                                 hit=(gross > 0).mean(), buyhold=bh, edge_vs_random=gross.mean() - (np.sign(np.random.default_rng(0).standard_normal(len(tt))) * y[tt]).mean()))
        print('H', H, 'done %.0fs' % (time.time() - t0), flush=True)
    r = pd.DataFrame(rows); r.to_csv('results/info_ablation.csv', index=False)
    pd.set_option('display.width', 220)
    for H in (60, 240, 1440):
        x = r[r.H == H]
        print(f'\n=== horizon {H} min: net bps per trade after MAKER round trip (4 bps), top-10% confidence; rows = feature group, cols = test year')
        print(x.pivot(index='group', columns='year', values='net_maker').round(1).to_string())
        print('IC:'); print(x.pivot(index='group', columns='year', values='ic').round(3).to_string())


if __name__ == '__main__':
    main()
