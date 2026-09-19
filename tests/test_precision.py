"""判定的精度：和桌機版逐位相同。

## 這在修什麼

快照存的是**拿去和門檻比大小**的數字，所以存進去的精度就是判定的精度。
原本一律 `round(…, 2)`，於是同一檔股票在桌機版和這裡會得到不同的答案——
而且兩個方向都會錯：

    量比原值 1.1951    桌機版 1.1951 < 1.2 → 淘汰
                       這裡   round(…,2)=1.2 → 1.2 < 1.2 為假 → 通過

    20日均量 1000.4    桌機版 1000.4 <= 1000 為假 → 通過
                       這裡   round(…)=1000 → 1000 <= 1000 → 淘汰

不是「寬一點」也不是「嚴一點」，是**對不起來**。而使用者拿兩邊的檔數相比的
時候，看到的就是一個沒有解釋的差異。

## 為什麼分兩種處理

* **和門檻比**的（close / vol20 / amt20 / bw / vol_ratio）→ 多存幾位。門檻
  來自畫面上的數字框，最細到小數第二位；存到第六位，要翻轉得有人打到第七位。
* **兩個算出來的數字互比**的（close vs ma60、ma20 vs ma60、close vs 布林上軌
  ／Donchian）→ 多存幾位**也不保險**，兩邊可以任意接近。這幾個判定沒有任何
  門檻在調，所以在 Python 這邊用完整的 float64 算完，存成布林值。

全部存全精度也是一個選項，量過：1,900 檔 gzip 之後快照從 146 KB 變成 494 KB。
那是使用者手機上真的要下載的東西，而這一段的目的是讓判定正確，不是把 float64
的每一位都送到瀏覽器。
"""

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
    """一檔剛好通過預設門檻的快照；kw 蓋掉想動的那幾格。"""
    close = kw.pop('close', 100.0)
    s = {
        'code': '2330', 'name': '台積電', 'industry': '半導體',
        'close': close, 'vol20': 5000.0, 'amt20': 5e8,
        'ma20': close * 0.95, 'ma60': close * 0.90,
        'cross_ago': 3, 'bw': [0.30] * 19 + [0.05],
        'boll_up': close * 0.98, 'donchian': close * 0.99,
        'vol_ratio': 1.5, 'atr14': 2.0, 'chg': 1.0, 'chg_pct': 1.0,
        'trend_ok': True, 'brk_boll': True, 'brk_don': True,
    }
    s.update(kw)
    assert set(s) == set(SNAPSHOT_COLUMNS)
    return s


# ── 和門檻比的那幾格：多存幾位就對了 ────────────────────────────────────


def test_量比_1_1951_要和桌機版一樣被淘汰():
    """桌機版：`1.1951 < 1.2` → 淘汰。

    存兩位的時候 round(1.1951, 2) 是 1.2，而 `1.2 < 1.2` 為假 → 這裡會放它過。
    """
    kept = round(1.1951, SNAPSHOT_PRECISION['vol_ratio'])
    ok, _ = passes(_snap(vol_ratio=kept), DEFAULT_RULES)
    assert not ok, (
        f"量比 1.1951 存成 {kept} 之後被判成通過了，桌機版是淘汰。"
        "SNAPSHOT_PRECISION['vol_ratio'] 是不是又被調回兩位？"
    )


def test_20日均量_1000_4_要和桌機版一樣通過():
    """桌機版：`1000.4 <= 1000` 為假 → 通過。存成整數就變成 1000 → 淘汰。"""
    kept = round(1000.4, SNAPSHOT_PRECISION['vol20'])
    ok, _ = passes(_snap(vol20=kept), DEFAULT_RULES)
    assert ok, (
        f"20日均量 1000.4 存成 {kept} 之後被判成淘汰了，桌機版是通過。"
    )


def test_壓縮門檻上下一點點都要判對():
    """頻寬 0.120001 不算壓縮、0.119999 算。存四位的話兩個都會變成 0.12。

    這兩個數字踩在**快照真正存得下的**最後一位（第六位）上，不是更細——寫
    0.1200001 的話 `round(…, 6)` 會把它變回 0.12，測到的就不是門檻，而是我
    自己的算術。（第一版就是那樣寫的，然後紅了。）
    """
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
    """新增一格卻忘了決定精度，就是下一個「和桌機版對不起來」。

    只管數字欄：code/name/industry 是字串，cross_ago 是整數，
    trend_ok/brk_* 是布林值，chg/chg_pct 判定用不到。
    """
    judged = {'close', 'vol20', 'amt20', 'ma20', 'ma60',
              'boll_up', 'donchian', 'vol_ratio', 'atr14'}
    missing = judged - set(SNAPSHOT_PRECISION)
    assert not missing, f"這幾格沒有寫在 SNAPSHOT_PRECISION 裡：{sorted(missing)}"


# ── 兩個算出來的數字互比：存布林值 ──────────────────────────────────────


def test_判定不看_ma60_只看_trend_ok():
    """快照裡的 ma20/ma60 現在只給畫面看。

    這一條讓兩者互相矛盾：float 說「站上季線」，布林說沒有。判定必須聽布林的。
    順著寫（兩者一致）的話，讀錯也會得到同一個答案，等於沒驗到。
    """
    s = _snap(ma20=95.0, ma60=90.0, trend_ok=False)   # float 說過
    ok, _ = passes(s, DEFAULT_RULES)
    assert not ok, "判定還在自己拿 ma60 比一次，沒有讀 trend_ok"


def test_判定不看布林上軌只看_brk_boll():
    s = _snap(boll_up=101.0, donchian=101.0, brk_boll=True, brk_don=False)
    ok, trig = passes(s, DEFAULT_RULES)
    assert ok, "判定還在自己拿 boll_up 比一次，沒有讀 brk_boll"
    assert '突破布林上軌' in trig and '突破20日高點' not in trig


@pytest.mark.parametrize('gap', [1e-3, 1e-6, 1e-9])
def test_收盤只比季線高一點點也要算站上(gap):
    """這是**多存幾位也救不了**的那一種，所以才要存布林值。

    收盤 100 + gap、季線 100：gap 小於快照的顯示精度時，兩個 float 會被存成
    同一個值，`close <= ma60` 於是成立。真正的答案由完整的 float64 決定。
    """
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


# ── 端對端：真的跑一趟 screen，快照裡要有那三個布林值 ──────────────────


def test_真的跑一趟的快照帶著那三個布林值(tmp_path):
    """helper 造出來的快照對了不算數，`screen_stock` 真的寫出來的才算。"""
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
        r = pl.run(str(tmp_path), make_excel=False, workers=1,
                   open_when_done=False, plotly_cdn=False)
    finally:
        pl.load_tw_stock_universe, pl.yf.download = orig_u, orig_d

    snaps = r['snapshots']
    assert snaps, '一份快照都沒有'
    s = snaps[0]
    for key in ('trend_ok', 'brk_boll', 'brk_don'):
        assert key in s, f'快照裡沒有 {key}'
        assert isinstance(s[key], bool), f'{key} 不是布林值：{s[key]!r}'
    # 存進去的位數要照表走（浮點數才有意義）。
    for key, nd in SNAPSHOT_PRECISION.items():
        v = s.get(key)
        if not isinstance(v, float):
            continue
        assert round(v, nd) == v, f'{key}={v} 沒有照 {nd} 位存'
