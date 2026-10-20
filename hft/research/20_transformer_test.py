"""Does a transformer over the last 30 s of ticks predict the next 5 s better than the current small net or gradient boosting?
Data: 11 days of BTC top of book (200 ms ticks) + trade flow. Folds: hold out one day, train on the other 10 (4 held-out days:
2023-07-15 quiet, 2023-12-15 normal, 2024-03-15 volatile, 2026-10-01 the replay day). Same label and metrics as 12/14/16_*.py.
The transformer sees a window of WIN ticks x per-tick features (book, queue changes, trade flow); each tick uses only earlier trades."""
import sys, glob, time, numpy as np, pandas as pd, torch, torch.nn as nn, warnings
warnings.filterwarnings('ignore')
H, WIN = 25, 150
DEV = torch.device('mps') if torch.backends.mps.is_available() else torch.device('cpu')
torch.manual_seed(0); np.random.seed(0)

def load():
    d = {}
    for f in sorted(glob.glob('../data/ext/grid200_*.npz')):
        z = np.load(f); d[f.split('grid200_')[1][:10]] = (z['bid'], z['ask'], z['bq'], z['aq'])
    g = np.load('../data/out/BTCUSDT_20261001_grid50ms.npz')
    d['2026-10-01'] = (g['bid'][::4] / 100., g['ask'][::4] / 100., g['bidsz'][::4] * 0.001, g['asksz'][::4] * 0.001)
    return d

def lag(a, k): return np.concatenate([np.full(k, a[0]), a[:-k]])

def tick_features(day, bid, ask, bq, aq):
    mid = (bid + ask) / 2; lr = np.r_[0, np.diff(mid) / mid[:-1]] * 1e4
    imb = (bq - aq) / (bq + aq); micro = (ask * bq + bid * aq) / (bq + aq)
    lbq, laq = np.log1p(bq * 1000), np.log1p(aq * 1000)
    z = np.load(f'../data/ext/trades200_{day}.npz')
    buy, sell, cnt = [np.concatenate([[0.], a[:-1]]) for a in (z['buy'], z['sell'], z['n'].astype(float))]     # tick i sees bins < i
    F = [imb, (micro / mid - 1) * 1e4, (ask - bid) / mid * 1e4, np.log1p((bq + aq) * 1000), lbq, laq, lr, lbq - lag(lbq, 1), laq - lag(laq, 1),
         (buy - sell) / (buy + sell + 1e-9), np.log1p((buy + sell) * 1000), np.log1p(cnt), imb - lag(imb, 5), (bid - lag(bid, 5)) / mid * 1e4]
    X = np.stack(F, 1).astype(np.float32)
    y = np.full(len(mid), np.nan, dtype=np.float32); y[:len(mid) - H] = (mid[H:] / mid[:len(mid) - H] - 1) * 1e4
    return X, y

class Net(nn.Module):
    def __init__(self, nf, d=64, layers=2, heads=4):
        super().__init__()
        self.inp = nn.Linear(nf, d); self.pos = nn.Parameter(torch.zeros(1, WIN, d))
        self.enc = nn.TransformerEncoder(nn.TransformerEncoderLayer(d, heads, 128, 0.1, batch_first=True, norm_first=True), layers)
        self.head = nn.Sequential(nn.LayerNorm(2 * d), nn.Linear(2 * d, 32), nn.ReLU(), nn.Linear(32, 1))
    def forward(self, x):
        h = self.enc(self.inp(x) + self.pos)
        return self.head(torch.cat([h[:, -1], h.mean(1)], 1)).squeeze(-1)

def windows(Xt, starts):
    """Xt: torch (N,F) on device; starts: tensor of window END indices -> (B, WIN, F)."""
    ar = torch.arange(-WIN + 1, 1, device=Xt.device)
    return Xt[(starts[:, None] + ar[None, :])]

def run_fold(test_day, days, D, epochs=2, bs=512):
    tr_days = [d for d in days if d != test_day]
    Xs, ys, ends = [], [], []
    off = 0
    for d in tr_days:
        X, y = D[d]; Xs.append(X); ys.append(y); ok = np.arange(WIN, len(y) - H, 8); ends.append(ok + off); off += len(y)
    Xall = np.concatenate(Xs); yall = np.concatenate(ys); ends = np.concatenate(ends)
    mu, sd = Xall.mean(0), Xall.std(0) + 1e-6
    norm = lambda X: np.clip((X - mu) / sd, -6, 6).astype(np.float32)
    Xt = torch.tensor(norm(Xall), device=DEV); yt = torch.tensor(np.clip(np.nan_to_num(yall), -15, 15), device=DEV)
    ends_t = torch.tensor(ends, device=DEV)
    net = Net(Xall.shape[1]).to(DEV); opt = torch.optim.AdamW(net.parameters(), 5e-4, weight_decay=1e-2); lf = nn.HuberLoss(delta=3.0)
    n = len(ends); t0 = time.time()
    for ep in range(epochs):
        net.train(); perm = torch.randperm(n, device=DEV); tot = 0.
        for i in range(0, n - bs, bs):
            b = ends_t[perm[i:i + bs]]; opt.zero_grad(); loss = lf(net(windows(Xt, b)), yt[b]); loss.backward()
            nn.utils.clip_grad_norm_(net.parameters(), 1.0); opt.step(); tot += loss.item()
        print(f'   {test_day} epoch {ep + 1}/{epochs} loss {tot / (n // bs):.3f} ({time.time() - t0:.0f}s)', flush=True)
    net.eval(); X, y = D[test_day]; Xte = torch.tensor(norm(X), device=DEV); idx = np.arange(WIN, len(y) - H); preds = []
    with torch.no_grad():
        for i in range(0, len(idx), 2048): preds.append(net(windows(Xte, torch.tensor(idx[i:i + 2048], device=DEV))).cpu().numpy())
    p = np.concatenate(preds); yy = y[idx]; c = np.abs(p)
    r = dict(day=test_day, ic=np.corrcoef(p, yy)[0, 1])
    for q in (0.9, 0.99, 0.999):
        t = c >= np.quantile(c, q); r[f'gross_q{q}'] = (np.sign(p[t]) * yy[t]).mean()
    return r

if __name__ == '__main__':
    raw = load(); D = {d: tick_features(d, *v) for d, v in raw.items()}; days = sorted(D)
    print('device', DEV, '| days', len(days), '| features', D[days[0]][0].shape[1], '| window', WIN, 'ticks', flush=True)
    folds = ['2023-07-15', '2023-12-15', '2024-03-15', '2026-10-01']
    rows = [run_fold(d, days, D) for d in folds]
    r = pd.DataFrame(rows); r.to_csv('results/transformer_test.csv', index=False); print(r.round(3).to_string(index=False))
    g = pd.read_csv('results/finetune_trades.csv'); g = g[g.day.isin(folds)]
    print('\nsame held-out days, mean:')
    print('transformer             IC %.3f  gross@top10%% %.2f  @1%% %.2f  @0.1%% %.2f bps' % (r.ic.mean(), r['gross_q0.9'].mean(), r['gross_q0.99'].mean(), r['gross_q0.999'].mean()))
    for m, x in g.groupby('model'):
        print('%-23s IC %.3f  gross@top10%% %.2f  @1%% %.2f  @0.1%% %.2f bps' % ('boosted trees ' + m, x.ic.mean(), x['gross_q0.9'].mean(), x['gross_q0.99'].mean(), x['gross_q0.999'].mean()))
    print('cost to beat per round trip: maker 4 bps, taker 10-11 bps')
