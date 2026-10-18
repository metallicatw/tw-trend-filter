
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
    n = 20
    df = _frame([12] * n, [10] * n, [11] * n)
    atr = compute_atr(df, period=14)
    assert math.isclose(atr.iloc[-1], 2.0)
    assert atr.iloc[:13].isna().all()


def test_atr_把跳空算進去():
    df = _frame([12, 12, 22], [10, 10, 20], [11, 10, 21])
    tr = compute_atr(df, period=1)
    assert math.isclose(tr.iloc[-1], 12.0)


def test_布林帶用樣本標準差():
    close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    ma, up, dn, bw = compute_bollinger(close, period=5, k=2)
    sd = math.sqrt(2.5)
    assert math.isclose(ma.iloc[-1], 3.0)
    assert math.isclose(up.iloc[-1], 3.0 + 2 * sd)
    assert math.isclose(dn.iloc[-1], 3.0 - 2 * sd)
    assert math.isclose(bw.iloc[-1], (4 * sd) / 3.0)


def test_布林帶前面資料不足的部分是_nan():
    close = pd.Series([1.0] * 30)
    ma, up, dn, bw = compute_bollinger(close, period=20)
    assert ma.iloc[:19].isna().all()
    assert math.isclose(bw.iloc[-1], 0.0)


def test_產業別以_isin_當日抓到的為準():
    assert lookup_industry('2330', {'2330': '半導體業'}) == '半導體業'
    assert lookup_industry('2330', {'2330': '光電業'}) == '光電業'


def test_isin_沒給就退回靜態表():
    assert lookup_industry('2330', {}) == INDUSTRY_MAP['2330']


def test_兩邊都沒有就是其他():
    assert lookup_industry('9999', {}) == '其他'


def test_isin_給了垃圾值也退回靜態表():
    for junk in ('', 'nan', 'NaN', 'None', '-'):
        assert lookup_industry('2330', {'2330': junk}) == INDUSTRY_MAP['2330']
