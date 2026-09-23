"""雲端版和桌機版 V3.1 篩出來的清單必須一模一樣。

## 為什麼有這個檔案

2026-09-23 同一天、同一份 Yahoo 資料，桌機版篩出 22 檔、雲端版 19 檔：

    只在桌機：7799 禾榮科、8299 群聯、7717 萊德光電-KY、3665 貿聯-KY
    只在雲端：2915 潤泰全

資料一個數字都沒有不一樣（收盤、成交量兩邊逐一對過）。差別是雲端版之前改了
兩條公式，而桌機版沒有：

    ③ 突破 20 日高點   桌機：前 20 日**最高收盤價**   雲端：前 20 日**最高價**
    ④ 量比的分母       桌機：20 日均量**含今天**      雲端：**不含今天**

每一條改動單獨看都有道理，合起來的結果是「兩支程式每天對不起來，而沒有人
知道為什麼」。所以規格是**桌機上那支程式**，這裡把它的判定原封不動抄進來
（`desktop_v31`，一個字都不要改），拿同一份 OHLCV 餵給兩邊逐檔比對。

以後要改定義，兩邊一起改，然後改這裡的那一段；只改一邊，這個檔案會紅。

## 兩種資料

* 2026-09-23 那十檔的真實 Yahoo 資料（`fixtures/yahoo_2026-09-23.csv.gz`），
  包含上面五檔「一邊進、一邊出」的。
* 亂數產生的幾千檔，而且**刻意貼著門檻產生**：收盤剛好等於／略高於前 20 日
  最高收盤、量比剛好落在 1.2 的正負一丁點、收盤剛好等於前 20 日最高價——邊界
  上才看得出兩份實作是不是同一件事。
* 快照取整本身（存到小數第幾位）也會把邊界翻過來，那一段單獨驗（`_toward`）。
"""

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


# ── 桌機版 V3.1：TW_Stock_Trend_Following_Trading_Filter_PC_V3.1.py 第 588–658 行 ──
#
# 逐字抄錄（只把 `return None` 改成回傳判定，好讓兩邊可以比）。**不要「順手」
# 修它**——它是規格，不是被測的程式。


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
    """桌機版的判定：回 ``(過了沒, 觸發訊號)``。"""
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


# ── 雲端版：快照 → passes()，也就是網頁上那一份 JS 讀的同一條路 ──────────────


def cloud(df, code='0000'):
    numbers, _ = indicator_snapshot(df)
    snap = {'code': code, 'name': code, 'industry': '',
            'six': None, 'rr': None, 'rr_free': False, **numbers}
    assert set(snap) == set(SNAPSHOT_COLUMNS)
    return passes(snap, DEFAULT_RULES)


# ── 2026-09-23 的真實資料 ────────────────────────────────────────────────


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
    # 使用者那天看到的差異：前四檔只在桌機、潤泰全只在雲端。改完之後兩邊都是：
    assert got['7799.TW'] and got['8299.TWO'] and got['7717.TWO'] and got['3665.TW'], got
    assert not got['2915.TW'], "潤泰全的量比（含今天）是 1.1921，不該過"
    assert got['1595.TWO'] and got['6526.TW'], "兩邊都有的那幾檔不見了"


# ── 亂數：貼著門檻產生 ──────────────────────────────────────────────────


def _series(rng, n=130):
    """一檔隨機的日 K。最後一天被刻意推到某一道門檻的邊上。"""
    drift = rng.normal(0.002, 0.004)
    ret = rng.normal(drift, rng.uniform(0.005, 0.03), n)
    close = 50 * np.exp(np.cumsum(ret))
    # 收盤價量化到 0.01（和真的報價一樣是格點上的數字——兩個收盤相等才會發生）
    close = np.round(close, 2)
    high = np.round(close * (1 + rng.uniform(0, 0.03, n)), 2)
    low = np.round(close * (1 - rng.uniform(0, 0.03, n)), 2)
    vol = np.round(rng.uniform(2e6, 6e6, n))

    kind = rng.integers(0, 4)
    if kind == 0:
        # 收盤剛好等於前 20 日最高收盤（`>` 不成立），或只高一檔
        top = close[-21:-1].max()
        close[-1] = top + rng.choice([0.0, 0.01, -0.01])
    elif kind == 1:
        # 量比剛好在 1.2 附近：解 V / ((S + V)/20) = r → V = r·S / (20 − r)
        s19 = vol[-20:-1].sum()
        r = 1.2 + rng.choice([0.0, 1e-9, -1e-9, 1e-4, -1e-4])
        vol[-1] = r * s19 / (20 - r)
    elif kind == 2:
        # 收盤剛好在最高價上（盤中最高就是收盤）——Donchian 用最高價的話這裡會翻
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
    # 沒有任何一檔通過的話，這個測試只是在比「兩邊都說不」。
    assert passed > 0, "亂數沒有產生任何通過的樣本，貼門檻的設計失效了"


# ── 快照取整：朝判定安全的方向 ─────────────────────────────────────────


@pytest.mark.parametrize('t', [0.12, 1.2, 10.0, 1000.0, 0.1, 2.5])
def test_進位之後和門檻比的答案不變(t):
    """`x ≤ t ⇔ 進位(x) ≤ t`、`x ≥ t ⇔ 捨去(x) ≥ t`，只要 t 在格點上。"""
    rng = np.random.default_rng(7)
    eps = [0.0, 1e-12, -1e-12, 1e-9, -1e-9, 4e-7, -4e-7, 6e-7, -6e-7, 1e-6, -1e-6]
    xs = [t + e for e in eps] + list(t + rng.normal(0, 1e-6, 500))
    for x in xs:
        assert (x <= t) == (_toward(x, 6, 'up') <= t), (x, _toward(x, 6, 'up'))
        assert (x > t) == (_toward(x, 6, 'up') > t), x
        assert (x >= t) == (_toward(x, 6, 'down') >= t), (x, _toward(x, 6, 'down'))
        assert (x < t) == (_toward(x, 6, 'down') < t), x


def test_四捨五入會翻的那個例子():
    """量比 1.1999996：四捨五入到六位是 1.2（→ 通過），桌機版是淘汰。"""
    assert round(1.1999996, 6) >= 1.2            # 舊做法：翻了
    assert not _toward(1.1999996, 6, 'down') >= 1.2
    assert _toward(0.12, 6, 'up') == 0.12, "剛好在格點上的值不能被推出去"
    # `math.ceil(x * 1e6) / 1e6` 那種寫法在這些格點上會把 x 推到下一格：
    # 0.000999 × 1e6 = 999.0000000000001 → 進位成 0.001。門檻剛好打 0.000999
    # 的時候，一個等於門檻的值被判到門檻外面。
    for g in (0.000999, 0.001998, 0.007881, 0.015651):
        assert _toward(g, 6, 'up') == g, g
        assert _toward(g, 6, 'down') == g, g
    assert _toward(1000.004, 2, 'up') > 1000
    assert _toward(50_000_000.4, 0, 'up') > 50_000_000
