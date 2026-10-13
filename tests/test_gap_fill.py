"""Yahoo 整個市場少一根日線時，用官方逐日行情補（2026-10-13）。

實際發生過：Yahoo 的台股日線全市場少了 2026-10-12（週一，正常交易日）與 2025-08-01。
少一根的後果不只是圖上缺一天——隔天那一根的漲跌變成「對兩天前」，20 日均量、量比、
布林、20 日高點的窗口全部錯位一天。久元（6261）10/13 實際 -0.1，報告上變成 +7.50。
"""
import gzip
from pathlib import Path

import pandas as pd

from tw_trend_filter.pipeline import fill_gap_rows, load_official_day, market_gaps


def _frame(days, close):
    idx = pd.to_datetime(days)
    return pd.DataFrame({'Open': close, 'High': close, 'Low': close, 'Close': close,
                         'Volume': [1000.0] * len(days)}, index=idx)


def test_只有週一到週五卻沒有那一根的日子才算缺():
    a = _frame(['2026-10-07', '2026-10-08', '2026-10-13'], [1.0, 1.0, 1.0])
    b = _frame(['2026-10-07', '2026-10-08', '2026-10-13'], [2.0, 2.0, 2.0])
    gaps = market_gaps([a, b])
    # 10/09（週五）、10/12（週一）是候選；週末不算。問官方才知道哪一天真的有交易
    assert [f'{d:%m-%d}' for d in gaps] == ['10-09', '10-12']


def test_官方行情讀得出來_假日或斷線回空(tmp_path):
    csv = ('date,code,market,close,open,high,low,change,volume,value\n'
           '2026-10-12,6261,上櫃,84,79.5,84,79.5,7.6,3127814,258637310\n')
    f = tmp_path / '2026-10-12.csv.gz'
    f.write_bytes(gzip.compress(csv.encode('utf-8')))
    url = Path(tmp_path).as_uri() + '/{date}.csv.gz'
    rows = load_official_day(pd.Timestamp('2026-10-12'), url)
    assert rows['6261'][:5] == (79.5, 84.0, 79.5, 84.0, 3127814.0)
    assert abs(rows['6261'][5] - 76.4) < 1e-9          # 前一天原始收盤 = 收 − 漲跌
    assert load_official_day(pd.Timestamp('2026-10-09'), url) == {}   # 沒有檔＝假日


def test_補進去的那一根換算成還原價_量不換算():
    # Yahoo 的還原價比原始價低 10%（之後有除息），補進去的那一根要用同一個比例
    df = _frame(['2026-10-07', '2026-10-08', '2026-10-13'], [70.0, 68.76, 75.51])
    gaps = {pd.Timestamp('2026-10-12'): {'6261': (79.5, 84.0, 79.5, 84.0, 3127814.0, 76.4)}}
    out = fill_gap_rows(df, '6261', gaps)
    assert list(out.index.strftime('%m-%d')) == ['10-07', '10-08', '10-12', '10-13']
    k = 68.76 / 76.4
    assert abs(out.loc['2026-10-12', 'Close'] - 84.0 * k) < 1e-9
    assert out.loc['2026-10-12', 'Volume'] == 3127814.0
    # 那天沒有這一檔的官方資料（停牌）就不補；範圍外的日子也不補
    assert len(fill_gap_rows(df, '9999', gaps)) == 3
    old = {pd.Timestamp('2026-01-05'): {'6261': (1, 1, 1, 1, 1, 1)}}
    assert len(fill_gap_rows(df, '6261', old)) == 3
