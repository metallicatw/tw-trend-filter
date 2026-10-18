
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tw_trend_filter.pipeline import (
    DEFAULT_RULES,
    SNAPSHOT_COLUMNS,
    SNAPSHOT_PRECISION,
    passes,
)


def _snap(**kw):
    close = kw.pop('close', 100.0)
    s = {
        'code': '2330', 'name': '台積電', 'industry': '半導體',
        'close': close, 'vol20': 5000.0, 'amt20': 5e8,
        'ma20': close * 0.95, 'ma60': close * 0.90,
        'cross_ago': 3, 'bw': [0.30] * 19 + [0.05],
        'boll_up': close * 0.98, 'donchian': close * 0.99,
        'vol_ratio': 1.5, 'atr14': 2.0, 'chg': 1.0, 'chg_pct': 1.0,
        'trend_ok': True, 'brk_boll': True, 'brk_don': True,
        'six': None, 'rr': None, 'rr_free': False,
    }
    s.update(kw)
    assert set(s) == set(SNAPSHOT_COLUMNS)
    return s




def test_量比_1_1951_要和桌機版一樣被淘汰():
    kept = round(1.1951, SNAPSHOT_PRECISION['vol_ratio'])
    ok, _ = passes(_snap(vol_ratio=kept), DEFAULT_RULES)
    assert not ok, (
        f"量比 1.1951 存成 {kept} 之後被判成通過了，桌機版是淘汰。"
        "SNAPSHOT_PRECISION['vol_ratio'] 是不是又被調回兩位？"
    )


def test_20日均量_1000_4_要和桌機版一樣通過():
    kept = round(1000.4, SNAPSHOT_PRECISION['vol20'])
    ok, _ = passes(_snap(vol20=kept), DEFAULT_RULES)
    assert ok, (
        f"20日均量 1000.4 存成 {kept} 之後被判成淘汰了，桌機版是通過。"
    )


def test_壓縮門檻上下一點點都要判對():
    nd = 6
    just_over = round(0.120001, nd)
    just_under = round(0.119999, nd)
    ok_over, _ = passes(
        _snap(cross_ago=0, bw=[0.30] * 19 + [just_over]), DEFAULT_RULES)
    ok_under, _ = passes(
        _snap(cross_ago=0, bw=[0.30] * 19 + [just_under]), DEFAULT_RULES)
    assert not ok_over, f"{just_over} 被當成壓縮了（門檻 0.12，要 <=）"
    assert ok_under, f"{just_under} 沒被當成壓縮（門檻 0.12，要 <=）"


def test_每一個快照欄位都要決定存幾位():
    judged = {'close', 'vol20', 'amt20', 'ma20', 'ma60',
              'boll_up', 'donchian', 'vol_ratio', 'atr14'}
    missing = judged - set(SNAPSHOT_PRECISION)
    assert not missing, f"這幾格沒有寫在 SNAPSHOT_PRECISION 裡：{sorted(missing)}"




def test_判定不看_ma60_只看_trend_ok():
    s = _snap(ma20=95.0, ma60=90.0, trend_ok=False)
    ok, _ = passes(s, DEFAULT_RULES)
    assert not ok, "判定還在自己拿 ma60 比一次，沒有讀 trend_ok"


def test_判定不看布林上軌只看_brk_boll():
    s = _snap(boll_up=101.0, donchian=101.0, brk_boll=True, brk_don=False)
    ok, trig = passes(s, DEFAULT_RULES)
    assert ok, "判定還在自己拿 boll_up 比一次，沒有讀 brk_boll"
    assert '突破布林上軌' in trig and '突破20日高點' not in trig


@pytest.mark.parametrize('gap', [1e-3, 1e-6, 1e-9])
def test_收盤只比季線高一點點也要算站上(gap):
    close = 100.0 + gap
    nd = SNAPSHOT_PRECISION['ma60']
    s = _snap(close=round(close, SNAPSHOT_PRECISION['close']),
              ma20=round(100.0 + gap, nd), ma60=round(100.0, nd),
              boll_up=round(99.0, nd), donchian=round(99.0, nd),
              trend_ok=close > 100.0 and (100.0 + gap) > 100.0)
    ok, _ = passes(s, DEFAULT_RULES)
    assert ok, (
        f"收盤比季線高 {gap} 卻被判成沒站上。"
        "這正是布林值要解決的：兩個 float 存到同一位就分不出來了。"
    )




def test_真的跑一趟的快照帶著那三個布林值(tmp_path):
    import tw_trend_filter.pipeline as pl

    days = 150
    idx = pd.bdate_range('2025-01-01', periods=days)
    closes = [100.0 + i * 0.3 for i in range(days - 1)] + [180.0]
    df = pd.DataFrame({
        'Open': closes, 'High': [c * 1.01 for c in closes],
        'Low': [c * 0.99 for c in closes], 'Close': closes,
        'Volume': [600_000] * (days - 1) + [2_000_000],
    }, index=idx)

    orig_u, orig_d = pl.load_tw_stock_universe, pl.yf.download
    try:
        pl.load_tw_stock_universe = lambda *a, **k: (
            ['1111.TW'], {'1111': '測試股'}, {'1111': '測試業'})
        pl.yf.download = lambda *a, **k: df.copy()
        r = pl.run(str(tmp_path), workers=1,
                   open_when_done=False, plotly_cdn=False)
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_u, orig_d

    snaps = r['snapshots']
    assert snaps, '一份快照都沒有'
    s = snaps[0]
    for key in ('trend_ok', 'brk_boll', 'brk_don'):
        assert key in s, f'快照裡沒有 {key}'
        assert isinstance(s[key], bool), f'{key} 不是布林值：{s[key]!r}'
    for key, nd in SNAPSHOT_PRECISION.items():
        v = s.get(key)
        if not isinstance(v, float):
            continue
        assert round(v, nd) == v, f'{key}={v} 沒有照 {nd} 位存'
