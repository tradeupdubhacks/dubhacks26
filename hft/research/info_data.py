"""Loaders for the 'information beyond price' data (hft/download_extra.py research). Every frame is indexed by the epoch
millisecond at which the value became KNOWN, so an as-of join on decision time cannot look ahead.

  metrics    5-min open interest, top-trader and global long/short ratios, taker buy/sell volume ratio.  Known at create_time + 5 min.
  funding    8-hourly funding rate.                                                                      Known at calc_time.
  premium    1-min premium-index klines (perp mark vs index basis).                                       Known at bar close.
  spot       1-min spot BTCUSDT klines.                                                                   Known at bar close.
  depth      bookDepth snapshots: cumulative bid/ask notional within 1..5 % of the mid.                   Known at its timestamp + 30 s.
"""
import glob, io, zipfile
import numpy as np
import pandas as pd

EXT = '../data/ext'


def _read_zip(path, **kw):
    with zipfile.ZipFile(path) as z:
        return pd.read_csv(io.BytesIO(z.read(z.namelist()[0])), **kw)


def _ms(series):
    return (pd.to_datetime(series, utc=True).astype('int64') // 1_000_000).values


def load_metrics(folder='metrics'):
    parts = [_read_zip(f) for f in sorted(glob.glob(f'{EXT}/{folder}/*.zip'))]
    d = pd.concat(parts, ignore_index=True)
    d['known'] = _ms(d['create_time']) + 5 * 60_000
    cols = ['sum_open_interest', 'sum_open_interest_value', 'count_toptrader_long_short_ratio',
            'sum_toptrader_long_short_ratio', 'count_long_short_ratio', 'sum_taker_long_short_vol_ratio']
    return d[['known'] + cols].apply(pd.to_numeric, errors='coerce').drop_duplicates('known').sort_values('known').set_index('known')


def load_funding():
    d = pd.concat([_read_zip(f) for f in sorted(glob.glob(f'{EXT}/funding/*.zip'))], ignore_index=True)
    d['known'] = d['calc_time'].astype('int64')
    return d[['known', 'last_funding_rate']].drop_duplicates('known').sort_values('known').set_index('known')


def _klines(folder, extra_cols=()):
    names = ['open_time', 'o', 'h', 'l', 'c', 'v', 'close_time', 'qv', 'n', 'tbv', 'tbqv', 'ig']
    out = []
    for f in sorted(glob.glob(f'{EXT}/{folder}/*.zip')):
        raw = _read_zip(f, header=None, names=names)
        if isinstance(raw.iloc[0, 0], str):          # header row in newer files
            raw = raw.iloc[1:]
        out.append(raw.apply(pd.to_numeric, errors='coerce'))
    d = pd.concat(out, ignore_index=True)
    t = d['open_time'].astype('int64')
    t = np.where(t > 1e14, t // 1000, t)
    d['known'] = t + 60_000                             # bar close
    return d.drop_duplicates('known').sort_values('known').set_index('known')


def load_premium():
    return _klines('premium')[['c']].rename(columns={'c': 'premium'})


def load_spot():
    d = _klines('spot')
    return d[['c', 'v', 'tbv', 'n']].rename(columns={'c': 'spot_c', 'v': 'spot_v', 'tbv': 'spot_tbv', 'n': 'spot_n'})


def load_depth():
    """One row per snapshot: log(bid/ask notional) within 1% and 5%, and log total depth within 1%."""
    rows = []
    for f in sorted(glob.glob(f'{EXT}/bookDepth/*.zip')):
        d = _read_zip(f)
        d['known'] = _ms(d['timestamp']) + 30_000
        p = d.pivot_table(index='known', columns='percentage', values='notional', aggfunc='last')
        need = [-5, -1, 1, 5]
        if not all(c in p.columns for c in need):
            continue
        r = pd.DataFrame(index=p.index)
        r['depth_imb1'] = np.log(p[-1].clip(lower=1) / p[1].clip(lower=1))
        r['depth_imb5'] = np.log(p[-5].clip(lower=1) / p[5].clip(lower=1))
        r['depth_tot1'] = np.log(p[-1].clip(lower=1) + p[1].clip(lower=1))
        rows.append(r)
    return pd.concat(rows).sort_index() if rows else pd.DataFrame()


def asof(frame, times, cols=None):
    """Values of `frame` (indexed by known-time) as of each decision time; NaN before the first row."""
    cols = list(cols or frame.columns)
    pos = np.searchsorted(frame.index.values, times, side='right') - 1
    vals = frame[cols].values
    out = np.full((len(times), len(cols)), np.nan)
    ok = pos >= 0
    out[ok] = vals[pos[ok]]
    return pd.DataFrame(out, columns=cols)
