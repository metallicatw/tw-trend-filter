
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tw_trend_filter.pipeline import (
    DEFAULT_RULES,
    SNAPSHOT_COLUMNS,
    _toward,
    indicator_snapshot,
    passes,
)

FIXTURE = Path(__file__).parent / 'fixtures' / 'yahoo_2026-09-23.csv.gz'




def _desktop_atr(df, period=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _desktop_bollinger(close, period=20, k=2):
    ma = close.rolling(period).mean()
    std = close.rolling(period).std()
    up = ma + k*std
    dn = ma - k*std
    bw = (up - dn) / ma
    return ma, up, dn, bw


def desktop_v31(df):
    close, volume = df['Close'], df['Volume']

    price = float(close.iloc[-1])
    vol20 = float(volume.rolling(20).mean().iloc[-1])
    amt20 = float((close*volume).rolling(20).mean().iloc[-1])
    if price <= 10 or vol20 <= 1000 or amt20 <= 50_000_000:
        return False, []

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    if price <= float(ma60.iloc[-1]) or float(ma20.iloc[-1]) <= float(ma60.iloc[-1]):
        return False, []

    boll_ma, boll_up, boll_dn, bw = _desktop_bollinger(close)
    lookback = min(10, len(ma20)-1)
    golden_cross = any(
        float(ma20.iloc[-i]) > float(ma60.iloc[-i]) and
        float(ma20.iloc[-i-1]) <= float(ma60.iloc[-i-1])
        for i in range(1, lookback+1)
    )
    squeeze = bool((bw.iloc[-lookback:] <= 0.12).any())
    if not (golden_cross or squeeze):
        return False, []

    donchian = close.rolling(20).max().shift(1)
    brk_boll = price > float(boll_up.iloc[-1])
    brk_donchian = (not pd.isna(donchian.iloc[-1])) and (price > float(donchian.iloc[-1]))
    if not (brk_boll or brk_donchian):
        return False, []

    vol_ratio = float(volume.iloc[-1]) / vol20
    if vol_ratio < 1.2:
        return False, []

    trigger_parts = []
    if golden_cross:
        trigger_parts.append('黃金交叉')
    if squeeze:
        trigger_parts.append('布林壓縮')
    if brk_boll:
        trigger_parts.append('突破布林上軌')
    if brk_donchian:
        trigger_parts.append('突破20日高點')
    return True, trigger_parts




def cloud(df, code='0000'):
    numbers, _ = indicator_snapshot(df)
    snap = {'code': code, 'name': code, 'industry': '',
            'six': None, 'rr': None, 'rr_free': False, **numbers}
    assert set(snap) == set(SNAPSHOT_COLUMNS)
    return passes(snap, DEFAULT_RULES)




def _real():
    raw = pd.read_csv(FIXTURE, parse_dates=['date'])
    for t, g in raw.groupby('ticker', sort=True):
        df = g.set_index('date')[['Open', 'High', 'Low', 'Close', 'Volume']]
        yield t, df.astype(float)


def test_2026_09_23_那十檔兩邊一模一樣():
    got = {}
    for t, df in _real():
        d, c = desktop_v31(df), cloud(df, t.split('.')[0])
        assert d == c, f"{t}：桌機 {d}，雲端 {c}"
        got[t] = d[0]
    assert got['7799.TW'] and got['8299.TWO'] and got['7717.TWO'] and got['3665.TW'], got
    assert not got['2915.TW'], "潤泰全的量比（含今天）是 1.1921，不該過"
    assert got['1595.TWO'] and got['6526.TW'], "兩邊都有的那幾檔不見了"




def _series(rng, n=130):
    drift = rng.normal(0.002, 0.004)
    ret = rng.normal(drift, rng.uniform(0.005, 0.03), n)
    close = 50 * np.exp(np.cumsum(ret))
    close = np.round(close, 2)
    high = np.round(close * (1 + rng.uniform(0, 0.03, n)), 2)
    low = np.round(close * (1 - rng.uniform(0, 0.03, n)), 2)
    vol = np.round(rng.uniform(2e6, 6e6, n))

    kind = rng.integers(0, 4)
    if kind == 0:
        top = close[-21:-1].max()
        close[-1] = top + rng.choice([0.0, 0.01, -0.01])
    elif kind == 1:
        s19 = vol[-20:-1].sum()
        r = 1.2 + rng.choice([0.0, 1e-9, -1e-9, 1e-4, -1e-4])
        vol[-1] = r * s19 / (20 - r)
    elif kind == 2:
        close[-1] = high[-21:-1].max()
    high = np.maximum(high, close)
    low = np.minimum(low, close)
    idx = pd.bdate_range('2026-01-02', periods=n)
    return pd.DataFrame({'Open': close, 'High': high, 'Low': low,
                         'Close': close, 'Volume': vol}, index=idx)


@pytest.mark.parametrize('seed', range(8))
def test_亂數貼著門檻兩邊一模一樣(seed):
    rng = np.random.default_rng(20260923 + seed)
    passed = 0
    for i in range(400):
        df = _series(rng)
        d, c = desktop_v31(df), cloud(df)
        assert d == c, f"seed {seed} 第 {i} 檔：桌機 {d}，雲端 {c}"
        passed += d[0]
    assert passed > 0, "亂數沒有產生任何通過的樣本，貼門檻的設計失效了"




@pytest.mark.parametrize('t', [0.12, 1.2, 10.0, 1000.0, 0.1, 2.5])
def test_進位之後和門檻比的答案不變(t):
    rng = np.random.default_rng(7)
    eps = [0.0, 1e-12, -1e-12, 1e-9, -1e-9, 4e-7, -4e-7, 6e-7, -6e-7, 1e-6, -1e-6]
    xs = [t + e for e in eps] + list(t + rng.normal(0, 1e-6, 500))
    for x in xs:
        assert (x <= t) == (_toward(x, 6, 'up') <= t), (x, _toward(x, 6, 'up'))
        assert (x > t) == (_toward(x, 6, 'up') > t), x
        assert (x >= t) == (_toward(x, 6, 'down') >= t), (x, _toward(x, 6, 'down'))
        assert (x < t) == (_toward(x, 6, 'down') < t), x


def test_四捨五入會翻的那個例子():
    assert round(1.1999996, 6) >= 1.2
    assert not _toward(1.1999996, 6, 'down') >= 1.2
    assert _toward(0.12, 6, 'up') == 0.12, "剛好在格點上的值不能被推出去"
    for g in (0.000999, 0.001998, 0.007881, 0.015651):
        assert _toward(g, 6, 'up') == g, g
        assert _toward(g, 6, 'down') == g, g
    assert _toward(1000.004, 2, 'up') > 1000
    assert _toward(50_000_000.4, 0, 'up') > 50_000_000
