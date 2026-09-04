"""指標與產業對照的單元測試。不碰網路。

這裡測的不是「程式跑不跑得動」——那由 CI 的煙霧測試負責。這裡測的是那些
**算錯了也不會當掉**的東西：一個少了一格的 rolling 窗口、一個把母體標準差
當成樣本標準差的布林帶。它們不會讓 workflow 變紅，只會讓每天的名單默默地
變成另一份名單。
"""

import math

import pandas as pd

from tw_trend_filter.pipeline import (
    INDUSTRY_MAP,
    compute_atr,
    compute_bollinger,
    lookup_industry,
)


def _frame(highs, lows, closes):
    return pd.DataFrame({'High': highs, 'Low': lows, 'Close': closes})


def test_atr_是前_n_根真實波幅的平均():
    # 每一根的 High-Low 都是 2，而且沒有跳空，所以真實波幅恆為 2，
    # 不管窗口多長，ATR 都該是 2。
    n = 20
    df = _frame([12] * n, [10] * n, [11] * n)
    atr = compute_atr(df, period=14)
    assert math.isclose(atr.iloc[-1], 2.0)
    # 前 13 根資料不足，必須是 NaN 而不是被填成 0——用 0 當停損距離
    # 會算出「停損 = 現價」。
    assert atr.iloc[:13].isna().all()


def test_atr_把跳空算進去():
    # 收在 10，隔天整根跳到 20~22：真實波幅是 |22-10| = 12，不是當根的 2。
    df = _frame([12, 12, 22], [10, 10, 20], [11, 10, 21])
    tr = compute_atr(df, period=1)
    assert math.isclose(tr.iloc[-1], 12.0)


def test_布林帶用樣本標準差():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    ma, up, dn, bw = compute_bollinger(close, period=5, k=2)
    # pandas 的 .std() 預設 ddof=1（樣本），這五個數的樣本標準差是 sqrt(2.5)。
    # 若誤用母體標準差（ddof=0）會得到 sqrt(2)，帶寬窄 11%——足以讓
    # 「布林壓縮 ≤ 12%」那一關的名單整個換一批。
    sd = math.sqrt(2.5)
    assert math.isclose(ma.iloc[-1], 3.0)
    assert math.isclose(up.iloc[-1], 3.0 + 2 * sd)
    assert math.isclose(dn.iloc[-1], 3.0 - 2 * sd)
    assert math.isclose(bw.iloc[-1], (4 * sd) / 3.0)


def test_布林帶前面資料不足的部分是_nan():
    close = pd.Series([1.0] * 30)
    ma, up, dn, bw = compute_bollinger(close, period=20)
    assert ma.iloc[:19].isna().all()
    # 完全不動的價格，標準差 0，上下軌貼在中軌上，頻寬 0。
    assert math.isclose(bw.iloc[-1], 0.0)


def test_產業別以_isin_當日抓到的為準():
    # ISIN 每天抓，靜態表是三個月前寫死的。同一檔兩邊不一致時，
    # 該信今天抓到的那一份。
    assert lookup_industry('2330', {'2330': '半導體業'}) == '半導體業'
    assert lookup_industry('2330', {'2330': '光電業'}) == '光電業'


def test_isin_沒給就退回靜態表():
    assert lookup_industry('2330', {}) == INDUSTRY_MAP['2330']


def test_兩邊都沒有就是其他():
    # 不是空字串，也不是 None——報告上那一格要印得出東西。
    assert lookup_industry('9999', {}) == '其他'


def test_isin_給了垃圾值也退回靜態表():
    for junk in ('', 'nan', 'NaN', 'None', '-'):
        assert lookup_industry('2330', {'2330': junk}) == INDUSTRY_MAP['2330']
