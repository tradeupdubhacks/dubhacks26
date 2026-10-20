"""Replication test: does the BTC result (price + positioning data at a 4 h horizon) hold on other coins?
Per coin: features G0 (own price/volume/flow) and G0+G2 (+ own open interest, top-trader and global long/short ratios, taker ratio);
H = 240 min; walk-forward by year on that coin's own history; top 10 % |prediction| trades, non-overlapping; same protocol as 18_info_features.py.
Nothing is tuned per coin."""
import sys, glob, warnings, numpy as np, pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
import info_data as ID
warnings.filterwarnings('ignore')
STEP, H = 5, 240
def cs(x): return np.concatenate([[0.], np.cumsum(x)])
def build(sym, mfolder):
    d = dict(np.load(f'klines_{sym}.npz')); c, o, h, l, v, tb, t = (d[k] for k in ('c', 'o', 'h', 'l', 'v', 'tbv', 't')); n = len(c)
    lr = np.r_[0, np.diff(np.log(c))] * 1e4; C1, C2, V, TB, RG = cs(lr), cs(lr ** 2), cs(v), cs(tb), cs((h - l) / c * 1e4)
    idx = np.flatnonzero((t // 60000) % STEP == 0); idx = idx[(idx >= 1500) & (idx < n - 1501)]; T = t[idx] + 60_000
    win = lambda C, k: C[idx + 1] - C[idx + 1 - k]; rv = lambda k: np.sqrt(np.maximum(win(C2, k) / k, 1e-9)); rv1440 = rv(1440); F = {}
    for k in (1, 5, 15, 60, 240, 1440): F[f'g0_ret{k}'] = win(C1, k) / (rv1440 * np.sqrt(k))
    F['g0_rv_ratio'] = np.log(rv(60) / rv1440); F['g0_logrv'] = np.log(rv1440)
    for k in (5, 60, 240):
        vv = win(V, k); F[f'g0_flow{k}'] = np.where(vv > 0, win(TB, k) / np.maximum(vv, 1e-12) - .5, 0)
    F['g0_volratio'] = np.log((win(V, 60) + 1e-9) / (win(V, 1440) / 24 + 1e-9)); F['g0_range15'] = win(RG, 15) / 15 / rv1440; F['g0_range60'] = win(RG, 60) / 60 / rv1440
    hr = ((t[idx] // 3_600_000) % 24) / 24 * 2 * np.pi; F['g0_sin'] = np.sin(hr); F['g0_cos'] = np.cos(hr)
    m = ID.load_metrics(mfolder); mm = m.copy(); oi = mm['sum_open_interest_value']
    mm['oi_chg1h'] = np.log(oi / oi.shift(12)); mm['oi_chg4h'] = np.log(oi / oi.shift(48)); mm['oi_chg24h'] = np.log(oi / oi.shift(288))
    mm['tt_lsr'] = np.log(mm['sum_toptrader_long_short_ratio']); mm['gl_lsr'] = np.log(mm['count_long_short_ratio']); mm['tt_cnt'] = np.log(mm['count_toptrader_long_short_ratio'])
    mm['taker_lsr'] = np.log(mm['sum_taker_long_short_vol_ratio']); mm['taker_lsr_m12'] = mm['taker_lsr'].rolling(12, min_periods=3).mean()
    mm['tt_lsr_d12'] = mm['tt_lsr'] - mm['tt_lsr'].shift(12); mm['gl_lsr_d12'] = mm['gl_lsr'] - mm['gl_lsr'].shift(12); mm['tt_minus_gl'] = mm['tt_lsr'] - mm['gl_lsr']
    ma = ID.asof(mm, T, ['oi_chg1h', 'oi_chg4h', 'oi_chg24h', 'tt_lsr', 'gl_lsr', 'tt_cnt', 'taker_lsr', 'taker_lsr_m12', 'tt_lsr_d12', 'gl_lsr_d12', 'tt_minus_gl'])
    for k in ma.columns: F[f'g2_{k}'] = ma[k].values
    F['g2_oi_vs_px4h'] = F['g2_oi_chg4h'] - win(C1, 240) / 1e4
    X = pd.DataFrame(F); yr = pd.to_datetime(t[idx], unit='ms').year.values; y = np.log(c[idx + H] / o[idx + 1]) * 1e4
    first_metric = T[np.isfinite(X['g2_oi_chg24h'].values)][0]
    return X, yr, y, rv1440, first_metric
rows = []
coins = {'BTCUSDT': 'metrics'}
for s in ['ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT']:
    if glob.glob(f'../data/ext/metrics_coins/{s}/*.zip'): coins[s] = f'metrics_coins/{s}'
print('coins with positioning data:', list(coins), flush=True)
for s, folder in coins.items():
    X, yr, y, rv1440, fm = build(s, folder); z = np.clip(y / (rv1440 * np.sqrt(H)), -5, 5)
    ok = np.isfinite(X['g2_oi_chg24h'].values); years = sorted(set(yr[ok]))
    for grp, cols in (('price only', [c for c in X.columns if c.startswith('g0')]), ('price + positioning', list(X.columns))):
        for Y in years[1:]:
            tr = np.flatnonzero((yr < Y) & ok); tr = tr[:-(H // STEP + 2)]; te = np.flatnonzero((yr == Y) & ok)
            if len(tr) < 20000 or len(te) < 5000: continue
            m = HistGradientBoostingRegressor(max_depth=4, max_iter=150, learning_rate=.05, l2_regularization=10, min_samples_leaf=300).fit(X.iloc[tr[::3]][cols], z[tr[::3]])
            p = m.predict(X.iloc[te][cols]); take = np.arange(0, len(te), H // STEP); tt = take[np.abs(p[take]) >= np.quantile(np.abs(p), .9)]
            g = np.sign(p[tt]) * y[te][tt]; rnd = np.sign(np.random.default_rng(1).standard_normal(len(tt))) * y[te][tt]
            rows.append(dict(coin=s, features=grp, year=Y, n=len(tt), gross=g.mean(), se=g.std() / np.sqrt(len(g)), hit=(g > 0).mean(), vs_random=g.mean() - rnd.mean(), ic=spearmanr(p, y[te])[0]))
    print(s, 'done', flush=True)
r = pd.DataFrame(rows); r.to_csv('results/other_coins_positioning.csv', index=False); pd.set_option('display.width', 220)
print('\nGROSS bps per 4h trade (top 10% confidence); cost to beat: maker 4, taker 11')
print(r.pivot_table(index=['coin', 'features'], columns='year', values='gross').round(1).to_string())
w = lambda g: pd.Series(dict(trades=g.n.sum(), gross=(g.gross * g.n).sum() / g.n.sum(), se=np.sqrt(((g.se * g.n) ** 2).sum()) / g.n.sum(), vs_random=(g.vs_random * g.n).sum() / g.n.sum()))
print('\npooled over all test years:'); s = r.groupby(['coin', 'features']).apply(w); s['t'] = s.gross / s.se; print(s.round(2).to_string())
print('\nAll non-BTC coins together:'); o = r[r.coin != 'BTCUSDT'].groupby('features').apply(w); o['t'] = o.gross / o.se; print(o.round(2).to_string())
